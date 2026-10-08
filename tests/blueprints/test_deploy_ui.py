import json

import pytest

from datamanager.blueprints.build import routes as build_routes
from datamanager.db import SessionLocal
from datamanager.deploy import orchestrator
from datamanager.models import BuildRun, Deployment, PackageLabel
from tests.test_deploy_orchestrator import add_package, env  # noqa: F401  (fixtures and helper)


@pytest.fixture()
def ui(env, client, monkeypatch):  # noqa: F811
    queued = []
    monkeypatch.setattr(build_routes, "enqueue_run", lambda run_id: queued.append(run_id) or f"job-{run_id}")
    env.queued = queued
    env.client = client
    return env


def test_index_lists_configurations_and_history(ui):
    html = ui.client.get("/deploy/").get_data(as_text=True)
    assert "dev-mini" in html and "Nothing deployed yet" in html and "New configuration" in html


def test_new_configuration_is_prefilled_validated_and_saved(ui):
    page = ui.client.get("/deploy/new").get_data(as_text=True)
    assert "compose-single-machine" in page and "regionservice" in page and "compose_file" in page  # the example
    config = {"classes": {"geodata": {"root": "/srv/geodata"}}}
    bad = ui.client.post("/deploy/new", data={"key": "bad key", "config": json.dumps(config)})
    assert bad.status_code == 422
    broken = ui.client.post("/deploy/new", data={"key": "lab", "config": "{nope"})
    assert broken.status_code == 422 and "Not valid JSON" in broken.get_data(as_text=True)
    remote = ui.client.post("/deploy/new", data={"key": "lab", "config": json.dumps({"host": {"ssh": "x"}, **config})})
    assert remote.status_code == 422 and "host" in remote.get_data(as_text=True)
    ok = ui.client.post("/deploy/new", data={"key": "lab", "description": "second", "config": json.dumps(config)})
    assert ok.status_code == 303 and ok.headers["Location"].endswith("/deploy/lab")
    assert ui.client.post("/deploy/new", data={"key": "lab", "config": json.dumps(config)}).status_code == 422  # exists


def test_edit_and_delete_configuration(ui):
    config = {"classes": {"geodata": {"root": "/srv/other"}}}
    assert ui.client.post("/deploy/dev-mini/edit", data={"description": "edited", "config": json.dumps(config)}).status_code == 303
    row = orchestrator.get_config(SessionLocal(), "dev-mini")
    assert row.description == "edited" and row.config_json == config
    assert "/srv/other" in ui.client.get("/deploy/dev-mini/edit").get_data(as_text=True)
    assert ui.client.post("/deploy/dev-mini/delete").status_code == 303
    assert ui.client.get("/deploy/dev-mini").status_code == 404


def test_workspace_offers_the_newest_verified_package(ui):
    add_package(ui, "r-1", "1")
    add_package(ui, "r-2", "2", verified=False)
    html = ui.client.get("/deploy/dev-mini").get_data(as_text=True)
    assert 'value="r-1" selected' in html and "NOT verified" in html
    for c in ("geodata", "valhalla", "pelias", "tiles"):
        assert f'value="{c}"' in html


def test_state_partial_reads_the_target_and_survives_errors(ui, monkeypatch):
    add_package(ui, "r-1")
    orchestrator.run(SessionLocal(), "dev-mini", "r-1", ["geodata"])
    html = ui.client.get("/deploy/dev-mini/state").get_data(as_text=True)
    assert "r-1" in html and "geodata" in html

    def broken(*a, **k):
        raise RuntimeError("object store unreachable")
    monkeypatch.setattr(orchestrator, "state", broken)
    assert "object store unreachable" in ui.client.get("/deploy/dev-mini/state").get_data(as_text=True)


def test_plan_partial_shows_classes_problems_and_the_start_button(ui):
    add_package(ui, "r-1", "1")
    add_package(ui, "r-2", "2", verified=False)
    ok = ui.client.post("/deploy/dev-mini/plan", data={"package": "r-1", "classes": ["geodata", "tiles"]}).get_data(as_text=True)
    assert "Start deploy of r-1" in ok and "geodata" in ok and "tiles" in ok and "valhalla" not in ok
    bad = ui.client.post("/deploy/dev-mini/plan", data={"package": "r-2"}).get_data(as_text=True)
    assert "not been verified" in bad and "disabled" in bad
    missing = ui.client.post("/deploy/dev-mini/plan", data={"package": "nope"}).get_data(as_text=True)
    assert "No package" in missing


def test_start_creates_a_deploy_run_and_enqueues_it(ui):
    add_package(ui, "r-1")
    response = ui.client.post("/deploy/dev-mini/start", data={"package": "r-1", "classes": ["geodata", "valhalla"]})
    assert response.status_code == 303 and "/build/runs/" in response.headers["Location"]
    run = SessionLocal().query(BuildRun).one()
    assert run.stage_key == "deploy" and ui.queued == [run.id]
    assert run.params_json == {"deploy_config": "dev-mini", "tag": "r-1", "classes": ["geodata", "valhalla"], "triggered_by": "ui", "drop_previous": False}
    # the run page renders (queued) for a deploy run
    assert ui.client.get(response.headers["Location"]).status_code == 200


def test_start_replans_and_refuses_unverified_or_running(ui):
    add_package(ui, "r-1", verified=False)
    refused = ui.client.post("/deploy/dev-mini/start", data={"package": "r-1"})
    assert refused.status_code == 303 and "error=" in refused.headers["Location"] and not ui.queued
    add_package(ui, "r-2")
    config = orchestrator.get_config(SessionLocal(), "dev-mini")
    session = SessionLocal()
    run = BuildRun(stage_key="deploy", status="running")
    session.add(run)
    session.commit()
    session.add(Deployment(package_id=None, package_tag="r-2", deploy_config_id=config.id, status="running", build_run_id=run.id))
    session.commit()
    again = ui.client.post("/deploy/dev-mini/start", data={"package": "r-2"})
    assert "still+running" in again.headers["Location"] or "still%20running" in again.headers["Location"]
    assert not ui.queued


def test_a_finished_deploy_shows_in_history_and_on_the_repo_pages(ui):
    package = add_package(ui, "r-1")
    orchestrator.run(SessionLocal(), "dev-mini", "r-1")
    history = ui.client.get("/deploy/dev-mini").get_data(as_text=True)
    assert "succeeded" in history and "r-1" in history
    detail = ui.client.get("/repo/r-1").get_data(as_text=True)
    assert "live on dev-mini" in detail and "Live on dev-mini" in detail
    index = ui.client.get("/repo/").get_data(as_text=True)
    assert "live on dev-mini" in index
    assert ui.client.post("/repo/r-1/delete").status_code in (303, 422)
    from datamanager.models import Package
    assert SessionLocal().query(Package).filter_by(tag="r-1").count() == 1  # deploy protection


def test_rollback_needs_a_previous_release(ui):
    add_package(ui, "r-1", "1")
    orchestrator.run(SessionLocal(), "dev-mini", "r-1", ["geodata"])
    refused = ui.client.post("/deploy/dev-mini/rollback", data={"classes": ["geodata"]})
    assert refused.status_code == 303 and "error=" in refused.headers["Location"] and not ui.queued
    add_package(ui, "r-2", "2")
    orchestrator.run(SessionLocal(), "dev-mini", "r-2", ["geodata"])
    done = ui.client.post("/deploy/dev-mini/rollback", data={"classes": ["geodata"]})
    assert done.status_code == 303 and "/build/runs/" in done.headers["Location"]
    run = SessionLocal().query(BuildRun).one()
    assert run.params_json["rollback"] is True and run.params_json["classes"] == ["geodata"]


def test_the_deploy_form_can_ask_to_drop_the_previous_release(ui):
    add_package(ui, "r-1", "1")
    add_package(ui, "r-2", "2")
    add_package(ui, "r-3", "3")
    from datamanager.deploy import orchestrator as orch
    orch.run(SessionLocal(), "dev-mini", "r-1", ["geodata"])
    orch.run(SessionLocal(), "dev-mini", "r-2", ["geodata"])
    html = ui.client.post("/deploy/dev-mini/plan", data={"package": "r-3", "classes": ["geodata"], "drop_previous": "1"}).get_data(as_text=True)
    assert "previous release r-1 is removed before the copy starts" in html
    assert "removed before" not in ui.client.post("/deploy/dev-mini/plan", data={"package": "r-3", "classes": ["geodata"]}).get_data(as_text=True)
    ui.client.post("/deploy/dev-mini/start", data={"package": "r-3", "classes": ["geodata"], "drop_previous": "1"})
    assert SessionLocal().query(BuildRun).one().params_json["drop_previous"] is True
