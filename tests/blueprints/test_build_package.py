import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from datamanager.blueprints.build import routes as build_routes
from datamanager.config import config as app_config
from datamanager.db import SessionLocal
from datamanager.jobs import tasks
from datamanager.models import Asset, BuildRun, ConfigProfile, Package, Region
from datamanager.services import assets as asset_service
from datamanager.services import packages, runs


def _asset(session, config_id, asset_type, name, rel, content, status="approved"):
    file = Path(app_config.DATA_ROOT) / rel
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_bytes(content)
    session.add(Asset(asset_type=asset_type, name=name, config_profile_id=config_id, path=rel, status=status,
                      content_hash=asset_service.sha256_of(file), size_bytes=len(content)))


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
    _asset(session, None, "country-pbf", "belgium", "library/assets/country-pbf/5/be.pbf", b"belgium")
    session.commit()
    return SimpleNamespace(id=profile.id)


def _start(client, cfg, stage, data):
    return client.post(f"/build/run/{stage}?config={cfg.id}", data=data)


def test_build_shows_the_three_stages_with_modals(client, cfg):
    html = client.get(f"/build/?config={cfg.id}").get_data(as_text=True)
    for key in ("package", "package-verify", "cleanup"):
        assert f'id="modal-{key}"' in html and f"openStageModal('{key}')" in html
    assert "date=" in html and "config=dev-mini" in html and "cannot be changed" in html
    assert "No package of this configuration yet" in html and "No verified package yet" in html  # blocked reasons


def test_package_plan_partial_and_reserved_labels(client, cfg):
    ok = client.post(f"/build/package/plan?config={cfg.id}", data={"classes": ["valhalla"]}).get_data(as_text=True)
    assert "Start package" in ok and ">3<" in ok
    bad = client.post(f"/build/package/plan?config={cfg.id}", data={"classes": ["pelias"], "force": "1"}).get_data(as_text=True)
    assert "no approved pelias-index-snapshot" in bad and "disabled" in bad
    reserved = client.post(f"/build/package/plan?config={cfg.id}", data={"classes": ["valhalla"], "labels": "date=x"}).get_data(as_text=True)
    assert "Reserved tag" in reserved


def test_start_package_creates_a_run_with_params(client, cfg):
    response = _start(client, cfg, "package", {"classes": ["valhalla"], "labels": "for=q4\ncandidate", "note": "hi", "verify": "1", "force": "1"})
    assert response.status_code == 302
    run = SessionLocal().query(BuildRun).one()
    assert run.stage_key == "package" and run.rq_job_id == f"job-{run.id}" and run.config_profile_id == cfg.id
    assert run.params_json == {"classes": ["valhalla"], "labels": {"for": "q4", "candidate": ""}, "note": "hi", "force": True,
                               "verify": True, "created_by": "ui"}
    assert _start(client, cfg, "package", {"classes": ["pelias"], "force": "1"}).status_code == 422
    assert _start(client, cfg, "package", {"classes": ["valhalla"], "labels": "config=x", "force": "1"}).status_code == 422
    assert SessionLocal().query(BuildRun).count() == 1


def test_package_run_with_verify_makes_a_verified_package_and_updates_status(client, cfg):
    _start(client, cfg, "package", {"classes": ["valhalla"], "verify": "1", "force": "1"})
    run = SessionLocal().query(BuildRun).one()
    assert tasks.run_stage(run.id)["status"] == "approved"
    package = SessionLocal().query(Package).one()
    assert package.verified_at is not None and package.build_run_id == run.id
    page = client.get(f"/build/runs/{run.id}").get_data(as_text=True)
    assert package.tag in page and "verified" in page and "Verify" in page and "<progress" in page
    index = client.get(f"/build/?config={cfg.id}").get_data(as_text=True)
    assert f"{package.tag} verified" in index  # the Verify package card
    assert "Cleaned up" not in index and f"Choose what to remove after {package.tag}" in index


def test_status_package_outdated_when_a_newer_asset_is_approved(cfg):
    from datamanager.stages import status as stage_status

    session = SessionLocal()
    packages.create_package(session, cfg.id, ["valhalla"])
    assert stage_status._package(session, cfg.id).state == "ok"
    _asset(session, cfg.id, "valhalla-admin", "benelux", "library/assets/valhalla/10/benelux/admin.sqlite", b"newer")
    session.commit()
    state = stage_status._package(session, cfg.id)
    assert state.state == "outdated" and "valhalla-admin benelux" in state.detail and "not verified" in state.detail


def test_verify_stage_start_and_results(client, cfg):
    package = packages.create_package(SessionLocal(), cfg.id, ["valhalla"])
    tag = package.tag
    assert _start(client, cfg, "package-verify", {"tag": "r-nope"}).status_code == 422
    assert _start(client, cfg, "package-verify", {"tag": tag}).status_code == 302
    run = SessionLocal().query(BuildRun).one()
    assert run.params_json == {"tag": tag}
    assert tasks.run_stage(run.id)["status"] == "approved"
    assert SessionLocal().query(Package).one().verified_at is not None
    assert "match their SHA-256" in client.get(f"/build/runs/{run.id}").get_data(as_text=True)
    (Path(package.path) / "valhalla/benelux/admin.sqlite").write_bytes(b"corrupt")
    again = runs.create_run(SessionLocal(), "package-verify", cfg.id, params={"tag": tag})
    assert tasks.run_stage(again.id)["status"] == "failed"
    failed = SessionLocal().get(BuildRun, again.id)
    assert "admin.sqlite" in failed.error_message and SessionLocal().query(Package).one().verified_at is None
    page = client.get(f"/build/runs/{again.id}").get_data(as_text=True)
    assert "admin.sqlite" in page and "problem" in page


def test_cleanup_modal_plan_and_run(client, cfg):
    session = SessionLocal()
    package = packages.create_package(session, cfg.id, ["valhalla"])
    tag = package.tag
    unverified = client.post(f"/build/cleanup/plan?config={cfg.id}", data={"tag": tag}).get_data(as_text=True)
    assert "not verified yet" in unverified
    assert _start(client, cfg, "cleanup", {"tag": tag, "category": ["country_pbf"]}).status_code == 422
    packages.verify_package(session, tag)
    first = client.post(f"/build/cleanup/plan?config={cfg.id}", data={"tag": tag}).get_data(as_text=True)
    assert 'value="country_pbf" checked' in first and 'value="planet" checked' not in first  # Settings defaults
    again = client.post(f"/build/cleanup/plan?config={cfg.id}", data={"tag": tag, "touched": "1", "category": ["valhalla"]}).get_data(as_text=True)
    assert 'value="valhalla" checked' in again and 'value="country_pbf" checked' not in again
    assert _start(client, cfg, "cleanup", {"tag": tag}).status_code == 422  # nothing ticked
    assert _start(client, cfg, "cleanup", {"tag": tag, "category": ["country_pbf", "valhalla"]}).status_code == 302
    run = SessionLocal().query(BuildRun).filter_by(stage_key="cleanup").one()
    assert run.params_json == {"tag": tag, "categories": ["country_pbf", "valhalla"]}
    assert tasks.run_stage(run.id)["status"] == "approved"
    assert not (Path(app_config.DATA_ROOT) / "library/assets/country-pbf/5/be.pbf").exists()
    assert not (Path(app_config.DATA_ROOT) / "library/assets/valhalla/9/benelux/tiles.tar").exists()
    page = client.get(f"/build/runs/{run.id}").get_data(as_text=True)
    assert "freed" in page and "Per-country PBFs" in page and "Valhalla outputs" in page
    assert f"Cleaned up after {tag}" in client.get(f"/build/?config={cfg.id}").get_data(as_text=True)


def test_cleanup_refuses_while_another_run_is_active(client, cfg):
    session = SessionLocal()
    tag = packages.create_package(session, cfg.id, ["valhalla"]).tag
    packages.verify_package(session, tag)
    session.add(BuildRun(stage_key="pelias", status="running"))
    session.commit()
    response = _start(client, cfg, "cleanup", {"tag": tag, "category": ["country_pbf"]})
    assert response.status_code == 422 and "queued or running" in response.get_data(as_text=True)
