import datetime
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from datamanager.config import config as app_config
from datamanager.db import SessionLocal
from datamanager.errors import PackageError
from datamanager.jobs import tasks
from datamanager.models import Asset, ConfigProfile, DownloadRecord, Package, Region
from datamanager.services import assets as asset_service
from datamanager.services import packages, runs
from datamanager.stages import status as stage_status


def _asset(session, config_id, asset_type, name, rel, content):
    file = Path(app_config.DATA_ROOT) / rel
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_bytes(content)
    session.add(Asset(asset_type=asset_type, name=name, config_profile_id=config_id, path=rel,
                      content_hash=asset_service.sha256_of(file), size_bytes=len(content), status="approved"))


@pytest.fixture()
def cfg(app, tmp_path, monkeypatch):
    monkeypatch.setattr(app_config, "PACKAGE_ROOT", str(tmp_path / "repo"))
    session = SessionLocal()
    profile = ConfigProfile(name="dev-mini")
    session.add(profile)
    session.flush()
    session.add(Region(config_profile_id=profile.id, name="benelux", color="#ff0000"))
    for t, n, f in [("valhalla-tiles", "benelux", "tiles.tar"), ("valhalla-admin", "benelux", "admin.sqlite"),
                    ("valhalla-timezones", "benelux", "tz.sqlite")]:
        _asset(session, profile.id, t, n, f"library/assets/valhalla/9/benelux/{f}", f"{t}".encode())
    session.commit()
    return profile


def _run(profile_id, **params):
    session = SessionLocal()
    run = runs.create_run(session, "package", profile_id, params=params)
    result = tasks.run_stage(run.id)
    session.refresh(run)
    return run, result


def test_stage_packages_and_approves_without_review(cfg):
    run, result = _run(cfg.id, classes=["valhalla"], labels={"baseline": ""}, note="n", force=True)
    assert result["status"] == "approved" and run.status == "approved" and run.reviewed_at is not None
    summary = run.report_json["summary"]
    assert summary["files"] == 3 and summary["classes"]["valhalla"]["files"] == 3
    package = SessionLocal().query(Package).one()
    assert package.tag == summary["tag"] and package.build_run_id == run.id and package.note == "n"
    doc = json.loads((Path(package.path) / "package.json").read_text())
    assert set(doc["tool_versions"]) == {"valhalla", "elasticsearch", "pelias_ref", "pelias_plan_version"}
    assert any(l.key == "tool.valhalla" and l.origin == "auto" for l in package.labels)


def test_byte_progress_is_recorded_per_class_step(cfg):
    run, _ = _run(cfg.id, classes=["valhalla"], force=True)
    names = [s.name for s in run.steps]
    assert names[0] == "Plan" and "Copy valhalla" in names
    copy = next(s for s in run.steps if s.name == "Copy valhalla")
    assert copy.progress_current == copy.progress_total and copy.progress_total > 0


def test_status_gate_refuses_unsettled_stages_unless_forced(cfg, monkeypatch):
    states = {"valhalla": stage_status.StageStatus("outdated", "Needs a run: benelux.")}
    monkeypatch.setattr(stage_status, "compute", lambda *a, **k: states)
    session = SessionLocal()
    with pytest.raises(PackageError, match="stage valhalla is outdated"):
        packages.create_package(session, cfg.id, ["valhalla"], check_status=True)
    assert packages.create_package(session, cfg.id, ["valhalla"], check_status=False).status == "complete"


def test_failed_run_when_inputs_missing(cfg):
    SessionLocal().query(Asset).filter_by(asset_type="valhalla-admin").delete()
    SessionLocal().commit()
    session = SessionLocal()
    run = runs.create_run(session, "package", cfg.id, params={"classes": ["valhalla"], "force": True})
    with pytest.raises(PackageError):
        tasks.run_stage(run.id)
    session.refresh(run)
    assert run.status == "failed" and "valhalla-admin" in run.error_message


def test_cli_inline_create_list_verify_prune(cfg, app):
    runner = CliRunner()
    out = runner.invoke(app.cli, ["package-create", "--config", "dev-mini", "--classes", "valhalla", "--force",
                                  "--inline", "--label", "for=q4", "--label", "candidate"])
    assert out.exit_code == 0, out.output
    tag = SessionLocal().query(Package).one().tag
    assert tag in out.output
    listing = runner.invoke(app.cli, ["package-list"]).output
    assert tag in listing and "for=q4" in listing and "candidate" in listing
    assert runner.invoke(app.cli, ["package-verify", tag]).exit_code == 0
    (Path(app_config.package_root) / tag / "valhalla/benelux/admin.sqlite").write_bytes(b"corrupt")
    bad = runner.invoke(app.cli, ["package-verify", tag])
    assert bad.exit_code != 0 and "admin.sqlite" in bad.output
    assert "nothing" in runner.invoke(app.cli, ["packages-prune", "--keep", "3"]).output
    assert runner.invoke(app.cli, ["package-create", "--config", "nope", "--inline"]).exit_code != 0
