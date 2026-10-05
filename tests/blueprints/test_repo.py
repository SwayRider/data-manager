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
    assert str(app_config.package_root) in html and "No packages" in html and "New package" in html


def test_plan_preview_lists_files_and_problems(client, cfg):
    ok = client.post("/repo/plan", data={"config": cfg.id, "classes": ["valhalla"]}).get_data(as_text=True)
    assert "valhalla" in ok and "3" in ok and "Create package" in ok
    bad = client.post("/repo/plan", data={"config": cfg.id, "classes": ["pelias"], "force": "1"}).get_data(as_text=True)
    assert "no approved pelias-index-snapshot" in bad and "disabled" in bad


def test_create_enqueues_a_package_run(client, cfg):
    response = client.post("/repo/create", data={"config": cfg.id, "classes": ["valhalla"], "labels": "for=q4\ncandidate",
                                                  "note": "hello", "force": "1"})
    assert response.status_code == 303
    run = SessionLocal().query(BuildRun).one()
    assert response.headers["Location"].endswith(f"/build/runs/{run.id}")
    assert run.stage_key == "package" and run.rq_job_id == f"job-{run.id}"
    assert run.params_json["labels"] == {"for": "q4", "candidate": ""} and run.params_json["classes"] == ["valhalla"]
    refused = client.post("/repo/create", data={"config": cfg.id, "classes": ["pelias"], "force": "1"})
    assert refused.status_code == 422 and SessionLocal().query(BuildRun).count() == 1


def test_list_detail_labels_and_filters(client, package):
    html = client.get("/repo/").get_data(as_text=True)
    assert package.tag in html and "candidate" in html and "dev-mini" in html
    assert package.tag not in client.get("/repo/?label=nomatch").get_data(as_text=True)
    assert package.tag in client.get("/repo/?class=valhalla").get_data(as_text=True)
    detail = client.get(f"/repo/{package.tag}").get_data(as_text=True)
    assert "valhalla_tiles.tar" in detail and "tool.valhalla" in detail and "after a clean" in detail
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


def test_cleanup_box_after_verify(client, package):
    assert packages.verify_package(SessionLocal(), package.tag) == []
    html = client.get(f"/repo/{package.tag}").get_data(as_text=True)
    assert "Per-country PBFs" in html and "Clean up selected" in html
    assert 'value="country_pbf" checked' in html and 'value="planet" checked' not in html  # Settings defaults
    preview = client.post(f"/repo/{package.tag}/cleanup/preview", data={"category": ["valhalla"]}).get_data(as_text=True)
    assert 'value="valhalla" checked' in preview and 'value="country_pbf" checked' not in preview
    done = client.post(f"/repo/{package.tag}/cleanup/apply", data={"category": ["country_pbf"]}).get_data(as_text=True)
    assert "Freed" in done and not (Path(app_config.DATA_ROOT) / "library/assets/country-pbf/5/be.pbf").exists()
    pbf = SessionLocal().query(Asset).filter_by(asset_type="country-pbf").one()
    assert asset_service.is_purged(pbf)
    SessionLocal().add(BuildRun(stage_key="pelias", status="running"))
    SessionLocal().commit()
    blocked = client.post(f"/repo/{package.tag}/cleanup/apply", data={"category": ["valhalla"]}).get_data(as_text=True)
    assert "queued or running" in blocked
