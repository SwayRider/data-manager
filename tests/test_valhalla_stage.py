import json
import stat
import textwrap
from pathlib import Path

import pytest

from datamanager.config import config
from datamanager.db import SessionLocal
from datamanager.jobs import tasks
from datamanager.models import Country
from datamanager.services import assets, runs, tools, valhalla_build
from datamanager.services import config_profiles as profiles
from datamanager.services import regions as region_service
from datamanager.services import resolve
from datamanager.stages import status as stage_status
from datamanager.stages.valhalla import plan_inputs

# Stand-ins for the compiled Valhalla binaries: tiny scripts that write the same kinds of files.
FAKES = {
    "valhalla_build_config": """
        import json, sys
        a = sys.argv[1:]
        get = lambda k: a[a.index(k) + 1]
        print(json.dumps({"extract": get("--mjolnir-tile-extract"), "admin": get("--mjolnir-admin"), "elevation": get("--additional-data-elevation")}))
    """,
    "valhalla_build_admins": """
        import json, sqlite3, sys
        cfg = json.load(open(sys.argv[sys.argv.index("--config") + 1]))
        db = sqlite3.connect(cfg["admin"]); db.execute("create table admins (id integer)"); db.commit()
    """,
    "valhalla_build_timezones": """
        import sqlite3, sys, tempfile, os
        path = tempfile.mktemp()
        db = sqlite3.connect(path); db.execute("create table tz (id integer)"); db.commit(); db.close()
        sys.stdout.buffer.write(open(path, "rb").read()); os.remove(path)
    """,
    "valhalla_build_tiles": """
        import sys
        assert "--concurrency=4" in sys.argv and sys.argv[-1].endswith(".osm.pbf")
    """,
    "valhalla_build_extract": """
        import io, json, sys, tarfile
        cfg = json.load(open(sys.argv[sys.argv.index("--config") + 1]))
        with tarfile.open(cfg["extract"], "w") as t:
            for name in ("2/000/818/660.gph", "1/051/305.gph"):
                data = b"graph"; info = tarfile.TarInfo(name); info.size = len(data); t.addfile(info, io.BytesIO(data))
    """,
    "valhalla_export_edges": """
        print("_encoded_polyline_1"); print("_encoded_polyline_2")
    """,
}


@pytest.fixture()
def built(app):
    """A 'compiled Valhalla' recorded as built in the tools dir."""
    root = Path(config.DATA_ROOT) / "tools" / "valhalla" / "valhalla-v3.5.1-x"
    (root / "build").mkdir(parents=True)
    for name, body in FAKES.items():
        script = root / "build" / name
        script.write_text("#!/usr/bin/env python3\n" + textwrap.dedent(body))
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
    valhalla_build._write_state({"status": "built", "tag": "latest", "resolved": "v3.5.1", "flags": valhalla_build.flags_hash(), "dir": str(root)})
    return root


@pytest.fixture()
def cfg(app):
    session = SessionLocal()
    session.add(Country(iso2="aa", name="AA", ne_geometry_ref="x", bbox_json=[0, 0, 1, 1], geofabrik_path="europe/aa",
                        wof_code="aa", srtm_bbox_json=[50, 51, 0, 1]))
    session.commit()
    profile = profiles.create_profile(session, "dev")
    region_service.assign_country(session, region_service.create_region(session, profile.id, "Region One"), "aa")
    return profile.id


def _approved_asset(profile_id, asset_type, name, content=b"pbf"):
    session = SessionLocal()
    run = runs.create_run(session, "osm-extract", profile_id)
    target = assets.asset_dir("test", run.id) / f"{name}.osm.pbf"
    target.write_bytes(content)
    asset = assets.create(session, run.id, profile_id, asset_type, name, target)
    asset.status = "approved"
    session.commit()
    return asset


def _approve_srtm(profile_id):
    session = SessionLocal()
    run = runs.create_run(session, "download-srtm", profile_id)
    (Path(config.DATA_ROOT) / "library" / "srtm" / str(run.id) / "N50").mkdir(parents=True)
    run.report_json = {"summary": {"directory": f"library/srtm/{run.id}"}, "record_ids": []}
    run.status = "approved"
    session.commit()
    return run


@pytest.fixture()
def inputs(cfg, built):
    _approved_asset(cfg, "osm-pbf", "region-one")
    _approve_srtm(cfg)
    return cfg


def _run(profile_id, params=None, approve=True):
    session = SessionLocal()
    run = runs.create_run(session, "valhalla", profile_id, params=params)
    result = tasks.run_stage(run.id)
    if approve and result["status"] == "awaiting_review":
        runs.approve(SessionLocal(), runs.get_run(SessionLocal(), run.id))
    return run.id, result


def _resolved(profile_id):
    session = SessionLocal()
    return session, resolve.to_dict(resolve.resolve_config(session, profile_id))


def test_blocked_when_valhalla_is_not_built(cfg):
    session, resolved = _resolved(cfg)
    problems = plan_inputs(session, cfg, resolved).problems
    assert any("not built" in p for p in problems)
    assert any("elevation" in p for p in problems) and any("no approved region PBF" in p for p in problems)
    assert tools.detect(session, "valhalla").status == "missing"
    run_id, result = _run(cfg)
    assert result["status"] == "failed" and "not built" in runs.get_run(SessionLocal(), run_id).report_json["error"]


def test_builds_region_assets(inputs):
    run_id, result = _run(inputs)
    assert result["status"] == "awaiting_review"
    report = runs.get_run(SessionLocal(), run_id).report_json
    entry = report["routing"][0]
    assert entry["result"] == "built" and entry["graph_tiles"] == 2 and entry["polyline_lines"] == 2
    session = SessionLocal()
    for asset_type, filename in (("valhalla-tiles", "tiles.tar"), ("valhalla-admin", "admin.sqlite"),
                                 ("valhalla-timezones", "tz_world.sqlite"), ("valhalla-polylines", "polylines.0sv.gz")):
        asset = assets.current(session, inputs, asset_type, "region-one")
        assert asset is not None and assets.abs_path(asset).name == filename and asset.size_bytes > 0
    assert not (Path(config.DATA_ROOT) / "work" / str(run_id)).exists()


def test_rerun_skips_unchanged_and_status_follows(inputs):
    session, resolved = _resolved(inputs)
    assert stage_status.compute(session, inputs, resolved, {})["valhalla"].state == "todo"
    _run(inputs)
    assert stage_status.compute(session, inputs, resolved, {})["valhalla"].state == "ok"
    run_id, _ = _run(inputs)
    assert runs.get_run(SessionLocal(), run_id).report_json["routing"][0]["result"] == "unchanged"
    _approved_asset(inputs, "osm-pbf", "region-one", content=b"changed pbf")
    session, resolved = _resolved(inputs)
    assert stage_status.compute(session, inputs, resolved, {})["valhalla"].state == "outdated"
    run_id, _ = _run(inputs)
    assert runs.get_run(SessionLocal(), run_id).report_json["routing"][0]["result"] == "built"


def test_a_new_valhalla_build_makes_regions_outdated(inputs, built):
    _run(inputs)
    state = valhalla_build.read_state()
    valhalla_build._write_state({**state, "resolved": "v3.6.0"})
    session, resolved = _resolved(inputs)
    assert stage_status.compute(session, inputs, resolved, {})["valhalla"].state == "outdated"


def test_failed_tool_fails_the_run_and_discards(inputs, built):
    (built / "build" / "valhalla_build_tiles").write_text("#!/bin/sh\nexit 3\n")
    run_id, result = _run(inputs)
    assert result["status"] == "failed"
    assert assets.current(SessionLocal(), inputs, "valhalla-tiles", "region-one") is None


def test_unknown_region_param(inputs):
    session, resolved = _resolved(inputs)
    assert any("Unknown region" in p for p in plan_inputs(session, inputs, resolved, ["nope"]).problems)


def test_tool_detection_states(app, built):
    session = SessionLocal()
    assert tools.detect(session, "valhalla").status == "ok"
    valhalla_build._write_state({**valhalla_build.read_state(), "status": "building"})
    assert tools.detect(session, "valhalla").status == "building"
    valhalla_build._write_state({**valhalla_build.read_state(), "status": "built", "flags": "other"})
    assert tools.detect(session, "valhalla").status == "outdated"
    valhalla_build._write_state({**valhalla_build.read_state(), "flags": valhalla_build.flags_hash()})
    from datamanager.services import settings as settings_service
    settings_service.save(session, {"tool.valhalla_tag": "v3.4.0"})
    assert tools.detect(session, "valhalla").status == "outdated"


def test_build_runs_the_expected_commands(app, monkeypatch):
    calls = []

    def fake_run(args, **kwargs):
        calls.append([str(a) for a in args])
        if args[0] == "git" and args[1] == "clone":
            Path(args[-1]).mkdir(parents=True)
        if args[0] == "make":
            for name in valhalla_build.BINARIES:
                (Path(kwargs["cwd"]) / name).write_text("")

    monkeypatch.setattr(valhalla_build, "run", fake_run)
    monkeypatch.setattr(valhalla_build, "resolve_tag", lambda tag: "v3.5.1")
    state = valhalla_build.build(SessionLocal())
    assert state["status"] == "built" and Path(state["dir"]).name.startswith("valhalla-v3.5.1-")
    assert calls[0][:6] == ["git", "clone", "--branch", "v3.5.1", "--single-branch", "--depth"]
    assert any(c[0] == "cmake" and "-DENABLE_TESTS=OFF" in c for c in calls)
    assert calls[-1][:3] == ["make", "all", "-j4"]
    assert tools.detect(SessionLocal(), "valhalla").status == "ok"


def test_failed_build_is_recorded_and_cleaned_up(app, monkeypatch):
    def fake_run(args, **kwargs):
        if args[0] == "git" and args[1] == "clone":
            Path(args[-1]).mkdir(parents=True)
        if args[0] == "cmake":
            raise valhalla_build.BuildError("cmake failed (exit 1)")

    monkeypatch.setattr(valhalla_build, "run", fake_run)
    monkeypatch.setattr(valhalla_build, "resolve_tag", lambda tag: "v3.5.1")
    with pytest.raises(valhalla_build.BuildError):
        valhalla_build.build(SessionLocal())
    status = tools.detect(SessionLocal(), "valhalla")
    assert status.status == "error" and "cmake failed" in status.message
    assert not any(p.name.startswith("valhalla-v3") for p in valhalla_build.tools_dir().iterdir())


def test_settings_tools_has_build_button_and_enqueues(client, monkeypatch):
    html = client.get("/settings/").get_data(as_text=True)
    assert "Valhalla (compiled)" in html and "/settings/tools/valhalla/build" in html
    monkeypatch.setattr("datamanager.blueprints.settings.routes.enqueue_build", lambda key: "job")
    monkeypatch.setattr(valhalla_build, "PREREQUISITES", ())
    response = client.post("/settings/tools/valhalla/build")
    assert response.status_code == 200 and valhalla_build.read_state()["status"] == "queued"
    assert client.post("/settings/tools/valhalla/build").status_code == 422  # already queued
    assert client.post("/settings/tools/osmium/build").status_code == 404


def test_build_page_shows_card_and_blocked_reason(client, cfg):
    html = client.get(f"/build/?config={cfg}").get_data(as_text=True)
    assert "Valhalla routing data" in html and "Valhalla is not built" in html
