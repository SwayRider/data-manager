from pathlib import Path

from types import SimpleNamespace

import pytest

from datamanager.blueprints.build import routes as build_routes
from datamanager.config import config as app_config
from datamanager.db import SessionLocal
from datamanager.models import Asset, BuildRun, ConfigProfile, Package, Region
from datamanager.services import assets as asset_service
from datamanager.services import packages


def _asset(session, config_id, asset_type, name, rel, content):
    file = Path(app_config.DATA_ROOT) / rel
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_bytes(content)
    session.add(Asset(asset_type=asset_type, name=name, config_profile_id=config_id, path=rel,
                      content_hash=asset_service.sha256_of(file), size_bytes=len(content), status="approved"))


@pytest.fixture()
def cfg(app, tmp_path, monkeypatch):
    monkeypatch.setattr(app_config, "PACKAGE_ROOT", str(tmp_path / "repo"))
    monkeypatch.setattr(build_routes, "enqueue_run", lambda run_id: f"job-{run_id}")
    session = SessionLocal()
    profile = ConfigProfile(name="dev-mini")
    session.add(profile)
    session.flush()
    session.add(Region(config_profile_id=profile.id, name="benelux", color="#ff0000"))
    for t, n, f in [("valhalla-tiles", "benelux", "tiles.tar"), ("valhalla-admin", "benelux", "admin.sqlite"),
                    ("valhalla-timezones", "benelux", "tz.sqlite")]:
        _asset(session, profile.id, t, n, f"library/assets/valhalla/9/benelux/{f}", t.encode())
    _asset(session, None, "country-pbf", "belgium", "library/assets/country-pbf/5/be.pbf", b"be")
    session.commit()
    return SimpleNamespace(id=profile.id)  # the app's session is removed after each request


@pytest.fixture()
def package(cfg):
    created = packages.create_package(SessionLocal(), cfg.id, ["valhalla"], labels={"candidate": ""})
    return SimpleNamespace(tag=created.tag, path=created.path)


def test_index_shows_repository_and_empty_state(client, cfg):
    html = client.get("/repo/").get_data(as_text=True)
    assert str(app_config.package_root) in html and "No packages" in html and "Packages are made, verified and cleaned up" in html




def test_list_detail_labels_and_filters(client, package):
    html = client.get("/repo/").get_data(as_text=True)
    assert package.tag in html and "candidate" in html and "dev-mini" in html
    assert package.tag not in client.get("/repo/?label=nomatch").get_data(as_text=True)
    assert package.tag in client.get("/repo/?class=valhalla").get_data(as_text=True)
    detail = client.get(f"/repo/{package.tag}").get_data(as_text=True)
    assert "valhalla_tiles.tar" in detail and "tool.valhalla" in detail and "Cleanup stage on Build" in detail and "unverified" in detail
    client.post(f"/repo/{package.tag}/labels", data={"labels": "live=dev-mini", "note": "n1", "protected": "1"})
    again = client.get(f"/repo/{package.tag}").get_data(as_text=True)
    assert "live=dev-mini" in again and "protected" in again
    assert client.post(f"/repo/{package.tag}/delete").status_code == 422
    assert client.get("/repo/r-nope").status_code == 404


def test_verify_enqueues_and_delete_removes(client, package):
    response = client.post(f"/repo/{package.tag}/verify")
    run = SessionLocal().query(BuildRun).one()
    assert response.status_code == 303 and run.stage_key == "package-verify" and run.params_json == {"tag": package.tag}
    assert client.post(f"/repo/{package.tag}/delete").status_code == 303
    assert SessionLocal().query(Package).count() == 0 and not Path(package.path).exists()





def test_created_package_carries_date_and_config_tags(package):
    import datetime

    tags = {l.key: l.value for l in SessionLocal().query(Package).one().labels if l.origin == "auto"}
    assert tags["config"] == "dev-mini" and tags["date"] == datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d")
    assert "created" not in tags



def test_progress_updates_are_throttled(cfg, monkeypatch):
    import datamanager.services.packages as pk

    monkeypatch.setattr(pk, "CHUNK", 4)
    calls = []
    pk.create_package(SessionLocal(), cfg.id, ["valhalla"], progress=lambda d, t, m: calls.append((d, t)))
    assert calls and calls[-1][0] == calls[-1][1]  # the final update always arrives
    assert len(calls) <= 4  # three tiny files, many 4-byte chunks, but at most one update per second


def test_unverified_badge_until_verified_and_reserved_labels(client, package):
    assert "unverified" in client.get("/repo/").get_data(as_text=True)
    assert packages.verify_package(SessionLocal(), package.tag) == []
    page = client.get("/repo/").get_data(as_text=True)
    assert 'title="verified' in page and ">unverified<" not in page
    edited = client.post(f"/repo/{package.tag}/labels", data={"labels": "config=other"})
    assert edited.status_code == 422 and "Reserved tag" in edited.get_data(as_text=True)
    keep = {(l.key, l.value, l.origin) for l in SessionLocal().query(Package).one().labels}
    assert ("config", "dev-mini", "auto") in keep and ("candidate", "", "user") in keep
    assert client.post(f"/repo/{package.tag}/labels", data={"labels": "for=q4\ncandidate"}).status_code == 303


def test_recent_runs_list_package_verify_and_cleanup_runs(client, package):
    from datamanager.services import runs

    session = SessionLocal()
    for stage, params in (("package-verify", {"tag": package.tag}), ("cleanup", {"tag": package.tag, "categories": ["country_pbf"]})):
        runs.create_run(session, stage, None, params=params)
    html = client.get("/repo/").get_data(as_text=True)
    assert "Recent runs" in html and "verify" in html and "cleanup" in html and package.tag in html


def test_progress_is_per_file_with_the_package_position_in_the_message(cfg, monkeypatch):
    import datamanager.services.packages as pk

    monkeypatch.setattr(pk, "CHUNK", 4)
    calls = []
    package = pk.create_package(SessionLocal(), cfg.id, ["valhalla"], progress=lambda d, t, m: calls.append((d, t, m)))
    sizes = sorted(i.size_bytes for i in package.items)
    finals = [c for c in calls if c[0] == c[1]]
    assert sorted(c[1] for c in finals) == sizes  # the bar total is the file's own size, not the package's
    assert all(c[0] <= c[1] for c in calls)
    assert any("file 1/3" in c[2] for c in calls) and all("in total" in c[2] for c in calls)
    verify_calls = []
    pk.verify_package(SessionLocal(), package.tag, progress=lambda d, t, m: verify_calls.append((d, t, m)))
    assert sorted(c[1] for c in verify_calls if c[0] == c[1]) == sizes and "file 3/3" in verify_calls[-1][2]
