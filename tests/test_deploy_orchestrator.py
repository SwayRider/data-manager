import datetime
import json

import pytest
from click.testing import CliRunner

from datamanager.db import SessionLocal
from datamanager.deploy import docker, orchestrator, s3_tiles
from datamanager.errors import DeployError
from datamanager.models import BuildRun, Deployment, Package, PackageItem, PackageLabel
from tests.test_deploy_driver import make_package
from tests.test_deploy_tiles import FakeS3

CLASSES = ("geodata", "valhalla", "pelias", "tiles")


@pytest.fixture()
def env(tmp_path, app, monkeypatch):
    db_session = SessionLocal()
    s3 = FakeS3()
    monkeypatch.setattr(s3_tiles, "make_client", lambda block: s3)
    monkeypatch.setattr(s3_tiles, "PART_SIZE", 64)
    monkeypatch.setattr(s3_tiles, "SINGLE_PUT_MAX", 100)
    state = {"Status": "running", "Running": True}

    def fake_docker(args, timeout=300, check=True):
        from types import SimpleNamespace
        return SimpleNamespace(returncode=0, stdout=json.dumps(state), stderr="")

    monkeypatch.setattr(docker, "run", fake_docker)
    monkeypatch.setattr(docker, "sleep", lambda s: None)
    monkeypatch.setattr("datamanager.deploy.activators.requests.request", _fake_es)
    config = {"host": None, "activation_order": list(CLASSES), "classes": {
        "geodata": {"root": str(tmp_path / "geodata"), "activate": {"type": "compose-restart", "services": {"all": "rs"}}},
        "valhalla": {"root": str(tmp_path / "valhalla"), "activate": {"type": "compose-restart", "services": {"benelux": "v"}}},
        "pelias": {"root": str(tmp_path / "pelias"), "es_snapshots": str(tmp_path / "es"),
                   "activate": {"type": "pelias-restore", "restart": {"region": ["pip-{region}"]}}},
        "tiles": {"transport": "s3", "endpoint": "http://x", "bucket": "b",
                  "credentials": {"access_key_env": "A", "secret_key_env": "B"},
                  "activate": {"type": "tilesservice-env", "env_file": str(tmp_path / "t.env"), "compose_file": "c.yml"}}}}
    orchestrator.save_config(db_session, "dev-mini", config, "test")
    return type("Env", (), {"tmp": tmp_path, "s3": s3, "docker": state, "session": db_session})


_INDICES: set = set()


def _fake_es(method, url, json=None, timeout=None):
    from types import SimpleNamespace
    path = url.split("39200", 1)[-1] if "39200" in url else url.split("localhost", 1)[-1]
    status, body = 200, {}
    if method == "HEAD":
        status = 200 if path.lstrip("/") in _INDICES else 404
    elif method == "POST" and "_restore" in path:
        _INDICES.add(path.split("/")[3])
        body = {"snapshot": {"shards": {"failed": 0}}}
    elif method == "GET" and path.endswith("_count"):
        body = {"count": 5}
    return SimpleNamespace(status_code=status, ok=status < 400, content=b"x" if body else b"", json=lambda: body, text="")


def add_package(env, tag, version="1", verified=True) -> Package:
    view = make_package(env.tmp, tag, version)
    package = Package(tag=tag, status="complete", path=str(view.path), size_bytes=sum(p.size for p in view.parts),
                      verified_at=datetime.datetime(2026, 10, 8) if verified else None)
    for p in view.parts:
        package.items.append(PackageItem(class_=p.class_, region=p.region, path=p.path, kind=p.kind, size_bytes=p.size, sha256=p.sha256))
    env.session.add(package)
    env.session.commit()
    return package


def test_save_config_validates_and_replaces(env):
    with pytest.raises(DeployError, match="host"):
        orchestrator.save_config(env.session, "x", {"host": {"ssh": "a"}, "classes": {"geodata": {"root": "/g"}}})
    with pytest.raises(DeployError, match="key"):
        orchestrator.save_config(env.session, "bad key!", {"classes": {"geodata": {"root": "/g"}}})
    row = orchestrator.save_config(env.session, "dev-mini", {"classes": {"geodata": {"root": "/other"}}}, "changed")
    assert row.config_json["classes"] == {"geodata": {"root": "/other"}} and row.description == "changed"


def test_resolve_package_by_tag_label_or_newest(env):
    one, two = add_package(env, "r-1"), add_package(env, "r-2", verified=False)
    env.session.add(PackageLabel(package_id=one.id, key="v1.0.1", value="", origin="user"))
    env.session.commit()
    assert orchestrator.resolve_package(env.session, "r-2").tag == "r-2"
    assert orchestrator.resolve_package(env.session, "v1.0.1").tag == "r-1"
    assert orchestrator.resolve_package(env.session, None).tag == "r-1"  # the newest verified one
    with pytest.raises(DeployError, match="No package"):
        orchestrator.resolve_package(env.session, "nope")


def test_unverified_packages_are_refused_unless_allowed(env):
    add_package(env, "r-1", verified=False)
    with pytest.raises(DeployError, match="not been verified"):
        orchestrator.run(env.session, "dev-mini", "r-1", ["geodata"])
    assert orchestrator.plan(env.session, "dev-mini", "r-1", ["geodata"])["problems"]
    assert orchestrator.run(env.session, "dev-mini", "r-1", ["geodata"], allow_unverified=True).status == "succeeded"


def test_plan_orders_classes_and_changes_nothing(env):
    add_package(env, "r-1")
    the_plan = orchestrator.plan(env.session, "dev-mini", "r-1", ["tiles", "geodata"])
    assert [e["class"] for e in the_plan["classes"]] == ["geodata", "tiles"]  # configured order, not the order asked
    assert not the_plan["problems"] and the_plan["bytes_to_copy"] > 0
    assert env.session.query(Deployment).count() == 0 and not (env.tmp / "geodata/current").exists()


def test_run_deploys_all_classes_in_order_and_marks_the_live_package(env):
    package = add_package(env, "r-1")
    steps = []
    deployment = orchestrator.run(env.session, "dev-mini", "r-1", step=steps.append)
    assert deployment.status == "succeeded" and deployment.classes_json == list(CLASSES)
    assert [s for s in steps if s.startswith("Deploy ")] == [f"Deploy {c}" for c in CLASSES]
    assert set(deployment.detail_json["classes"]) == set(CLASSES) and deployment.finished_at
    state = orchestrator.state(env.session, "dev-mini")
    assert {c: state[c]["current"] for c in CLASSES} == {c: "r-1" for c in CLASSES}
    live = env.session.query(PackageLabel).filter_by(package_id=package.id, key="live").one()
    assert live.value == "dev-mini"
    from datamanager.services import packages
    with pytest.raises(Exception, match="deployed"):
        packages.delete(env.session, "r-1")


def test_second_deploy_moves_the_live_label_and_records_the_previous_package(env):
    one, two = add_package(env, "r-1", "1"), add_package(env, "r-2", "2")
    orchestrator.run(env.session, "dev-mini", "r-1")
    third = add_package(env, "r-3", "3")
    orchestrator.run(env.session, "dev-mini", "r-2")
    deployment = orchestrator.run(env.session, "dev-mini", "r-3")
    assert deployment.previous_package_id == two.id
    tagged = lambda p: env.session.query(PackageLabel).filter_by(package_id=p.id, key="live").count()
    assert (tagged(one), tagged(two), tagged(third)) == (0, 1, 1)  # r-1 left the target, r-2 is previous, r-3 current


def test_failed_class_stops_the_sequence_and_a_rerun_resumes(env):
    add_package(env, "r-1")
    env.docker.update({"Status": "exited", "Running": False})
    deployment = orchestrator.run(env.session, "dev-mini", "r-1")
    assert deployment.status == "failed" and "exited" in deployment.detail_json["error"]
    assert list(deployment.detail_json["classes"]) == ["geodata"]  # nothing after the failed class was touched
    env.docker.update({"Status": "running", "Running": True})
    again = orchestrator.run(env.session, "dev-mini", "r-1")
    assert again.status == "succeeded"


def test_second_run_is_refused_while_one_is_active_and_stale_rows_are_failed(env):
    package = add_package(env, "r-1")
    config = orchestrator.get_config(env.session, "dev-mini")
    run = BuildRun(stage_key="deploy", status="running")
    env.session.add(run)
    env.session.commit()
    env.session.add(Deployment(package_id=package.id, package_tag="r-1", deploy_config_id=config.id, status="running", build_run_id=run.id))
    env.session.commit()
    with pytest.raises(DeployError, match="still running"):
        orchestrator.run(env.session, "dev-mini", "r-1", ["geodata"])
    run.status = "failed"  # the worker died
    env.session.commit()
    assert orchestrator.run(env.session, "dev-mini", "r-1", ["geodata"]).status == "succeeded"
    assert env.session.query(Deployment).filter_by(status="failed").count() == 1


def test_partial_deploy_warns_about_mixed_packages(env):
    add_package(env, "r-1", "1")
    add_package(env, "r-2", "2")
    orchestrator.run(env.session, "dev-mini", "r-1", ["geodata", "valhalla"])
    the_plan = orchestrator.plan(env.session, "dev-mini", "r-2", ["geodata"])
    assert any("different packages" in w for w in the_plan["warnings"])


def test_rollback_records_the_reverted_deployment_and_removes_the_release(env):
    add_package(env, "r-1", "1")
    add_package(env, "r-2", "2")
    first = orchestrator.run(env.session, "dev-mini", "r-1")
    second = orchestrator.run(env.session, "dev-mini", "r-2")
    back = orchestrator.rollback(env.session, "dev-mini")
    assert back.status == "succeeded" and back.package_tag == "r-1" and back.rolled_back_from_id == second.id
    env.session.refresh(second)
    assert second.status == "rolled_back" and first.status == "succeeded"
    state = orchestrator.state(env.session, "dev-mini")
    assert all(state[c]["current"] == "r-1" and state[c]["previous"] is None for c in CLASSES)
    assert env.session.query(PackageLabel).filter_by(key="live").count() == 1  # r-2 is no longer on the target
    with pytest.raises(DeployError, match="No previous release"):
        orchestrator.rollback(env.session, "dev-mini")


# ---- stage and CLI --------------------------------------------------------------------------------------------------

def test_stage_runs_a_deploy_and_a_rollback(env):
    from datamanager.jobs import tasks
    from datamanager.services import runs

    add_package(env, "r-1", "1")
    add_package(env, "r-2", "2")
    for tag in ("r-1", "r-2"):
        run = runs.create_run(env.session, "deploy", None, params={"deploy_config": "dev-mini", "tag": tag})
        result = tasks.run_stage(run.id)
        env.session.refresh(run)
        assert result["status"] == "approved", run.report_json
        assert run.status == "approved" and run.report_json["summary"]["tag"] == tag
    run = runs.create_run(env.session, "deploy", None, params={"deploy_config": "dev-mini", "rollback": True})
    tasks.run_stage(run.id)
    env.session.refresh(run)
    assert run.report_json["summary"]["rollback"] and run.report_json["summary"]["tag"] == "r-1"


def test_stage_reports_a_failed_deploy(env):
    from datamanager.jobs import tasks
    from datamanager.services import runs

    add_package(env, "r-1")
    run = runs.create_run(env.session, "deploy", None, params={"deploy_config": "nope", "tag": "r-1"})
    tasks.run_stage(run.id)
    env.session.refresh(run)
    assert run.status == "failed" and "No deploy configuration" in run.report_json["error"]


def test_cli_plan_deploy_state_and_rollback(env, app):
    add_package(env, "r-1", "1")
    add_package(env, "r-2", "2")
    runner = CliRunner()
    invoke = lambda *args: runner.invoke(app.cli, list(args))
    out = invoke("deploy-plan", "--config", "dev-mini", "--tag", "r-1")
    assert out.exit_code == 0 and "geodata" in out.output and "to copy" in out.output
    assert invoke("deploy", "--config", "dev-mini", "--tag", "r-1", "--inline").exit_code == 0
    assert invoke("deploy", "--config", "dev-mini", "--tag", "r-2", "--inline").exit_code == 0
    out = invoke("deploy-state", "--config", "dev-mini")
    assert "current=r-2 previous=r-1" in out.output and "succeeded" in out.output
    assert invoke("deploy-rollback", "--config", "dev-mini", "--inline").exit_code == 0
    assert "current=r-1 previous=None" in invoke("deploy-state", "--config", "dev-mini").output
    bad = invoke("deploy-plan", "--config", "nope")
    assert bad.exit_code != 0 and "No deploy configuration" in bad.output


def test_cli_config_save_from_the_example_file(env, app, tmp_path):
    from pathlib import Path
    runner = CliRunner()
    result = runner.invoke(app.cli, ["deploy-config-save", "example", "--file", "deploy-configs/dev-mini.example.json"])
    assert result.exit_code == 0, result.output
    assert "example" in runner.invoke(app.cli, ["deploy-config-list"]).output
