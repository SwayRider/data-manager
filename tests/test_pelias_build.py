from pathlib import Path

import pytest

from datamanager.db import SessionLocal
from datamanager.services import built_tools, pelias_build, tools
from datamanager.services import settings as settings_service


def _fake_run(calls, fail_on=None):
    def fake_run(args, **kwargs):
        calls.append(([str(a) for a in args], kwargs.get("cwd")))
        if args[0] == "git" and args[1] == "clone":
            (Path(args[-1]) / "node_modules").mkdir(parents=True)
        if fail_on and args[0] == "npm" and Path(kwargs["cwd"]).name == fail_on:
            raise pelias_build.BuildError("npm install failed (exit 1)")

    return fake_run


@pytest.fixture()
def fakes(monkeypatch):
    calls = []
    monkeypatch.setattr(pelias_build, "run", _fake_run(calls))
    monkeypatch.setattr(pelias_build, "_head", lambda repo: repo.name.ljust(40, "a"))
    return calls


def test_build_clones_and_installs_every_repository(app, fakes):
    state = pelias_build.build(SessionLocal())
    assert state["status"] == "built" and Path(state["dir"]).name.startswith("pelias-master-")
    clones = [c for c, _ in fakes if c[:2] == ["git", "clone"]]
    assert [c[-1].rsplit("/", 1)[1] for c in clones] == list(pelias_build.REPOS) and "interpolation" in pelias_build.REPOS
    assert clones[0][2:4] == ["--branch", "master"] and clones[0][-2] == "https://github.com/pelias/schema"
    assert [(c[:2], cwd.name) for c, cwd in fakes if c[0] == "npm"][:2] == [(["npm", "install"], "schema"), (["npm", "install"], "schema")]
    assert tools.detect(SessionLocal(), "pelias").status == "ok"
    assert pelias_build.require(SessionLocal(), "schema") == Path(state["dir"]) / "schema"
    assert pelias_build.version().split("-")[0] == "schemaa"  # first seven characters of each commit


def test_ref_setting_makes_the_build_outdated(app, fakes):
    pelias_build.build(SessionLocal())
    session = SessionLocal()
    settings_service.save(session, {"tool.pelias_ref": "v2.0.0"})
    status = tools.detect(session, "pelias")
    assert status.status == "outdated" and "v2.0.0" in status.message


def test_failed_build_is_cleaned_up_and_keeps_the_previous_one(app, monkeypatch, fakes):
    first = pelias_build.build(SessionLocal())
    monkeypatch.setattr(pelias_build, "run", _fake_run([], fail_on="geonames"))
    with pytest.raises(pelias_build.BuildError):
        pelias_build.build(SessionLocal())
    assert tools.detect(SessionLocal(), "pelias").status == "error"
    assert [p for p in pelias_build.tools_dir().iterdir() if p.is_dir()] == [Path(first["dir"])]
    assert pelias_build.read_state()["dir"] == first["dir"]


def test_missing_and_not_built(app):
    assert tools.detect(SessionLocal(), "pelias").status == "missing"
    with pytest.raises(Exception, match="not built"):
        pelias_build.require(SessionLocal(), "schema")


def test_request_refuses_a_data_root_with_spaces(app, monkeypatch):
    monkeypatch.setattr(pelias_build, "PREREQUISITES", ())
    monkeypatch.setattr(pelias_build, "tools_dir", lambda: Path("/data root/tools/pelias"))
    with pytest.raises(Exception, match="spaces"):
        pelias_build.request(SessionLocal())


def test_settings_tools_has_pelias_build_button_and_enqueues(client, monkeypatch):
    html = client.get("/settings/").get_data(as_text=True)
    assert "Pelias importers" in html and "/settings/tools/pelias/build" in html
    assert "Elasticsearch work directory" in html
    monkeypatch.setattr("datamanager.blueprints.settings.routes.enqueue_build", lambda key: "job")
    monkeypatch.setattr(pelias_build, "PREREQUISITES", ())
    monkeypatch.setattr(built_tools, "_live_build", lambda key: True)  # the enqueue above is faked: pretend its job is queued
    response = client.post("/settings/tools/pelias/build")
    assert response.status_code == 200 and pelias_build.read_state()["status"] == "queued"
    assert client.post("/settings/tools/pelias/build").status_code == 422


def test_a_build_without_a_job_is_recovered_and_can_be_started_again(client, monkeypatch):
    monkeypatch.setattr(pelias_build, "PREREQUISITES", ())
    monkeypatch.setattr("datamanager.blueprints.settings.routes.enqueue_build", lambda key: "job")
    pelias_build._write_state({"status": "queued", "message": ""})
    monkeypatch.setattr(built_tools, "_live_build", lambda key: True)
    assert built_tools.recover_stale("pelias") is False and pelias_build.read_state()["status"] == "queued"
    monkeypatch.setattr(built_tools, "_live_build", lambda key: False)  # worker killed, queue empty
    assert built_tools.recover_stale("pelias") is False  # ...but not within a minute of the last change: a worker may just have taken the job
    monkeypatch.setattr(built_tools, "STALE_AFTER_S", 0)
    assert "Build" in client.get("/settings/tools").get_data(as_text=True)
    state = pelias_build.read_state()
    assert state["status"] == "failed" and "interrupted" in state["message"]
    monkeypatch.setattr(built_tools, "_live_build", lambda key: True)  # the faked enqueue made no job: pretend it did
    assert client.post("/settings/tools/pelias/build").status_code == 200 and pelias_build.read_state()["status"] == "queued"


def test_broker_down_leaves_the_state_alone(app, monkeypatch):
    pelias_build._write_state({"status": "building"})

    def down(key):
        raise ConnectionError("redis down")

    monkeypatch.setattr(built_tools, "_live_build", down)
    assert built_tools.recover_stale("pelias") is False and pelias_build.read_state()["status"] == "building"


def test_unexpected_error_in_the_job_does_not_leave_the_build_running(app, monkeypatch):
    from datamanager.jobs import tasks

    def boom(session):
        pelias_build._write_state({"status": "building"})
        raise RuntimeError("boom")

    monkeypatch.setattr(pelias_build, "build", boom)
    with pytest.raises(RuntimeError):
        tasks.build_tool("pelias")
    assert pelias_build.read_state()["status"] == "failed" and "boom" in pelias_build.read_state()["message"]


def test_a_build_without_the_interpolation_repo_is_outdated(app, fakes):
    pelias_build.build(SessionLocal())
    pelias_build._write_state({**pelias_build.read_state(), "flags": "from-an-older-repository-list"})
    status = tools.detect(SessionLocal(), "pelias")
    assert status.status == "outdated" and "rebuild" in status.message


def test_pelias_build_needs_libpostal(app, monkeypatch):
    assert "libpostal" in pelias_build.PREREQUISITES
    monkeypatch.setattr(pelias_build, "PREREQUISITES", ("libpostal",))
    monkeypatch.setattr(tools, "detect", lambda session, key, force=False: tools.ToolStatus(key, "missing"))
    with pytest.raises(Exception, match="libpostal"):
        pelias_build.request(SessionLocal())
