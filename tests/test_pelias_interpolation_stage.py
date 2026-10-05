import datetime
import gzip
import json
import platform
import sqlite3
import stat
from pathlib import Path

import pytest

from datamanager.config import config
from datamanager.db import SessionLocal
from datamanager.jobs import tasks
from datamanager.models import Country, DownloadRecord
from datamanager.services import assets, pelias_build, pelias_interpolation, runs
from datamanager.services import config_profiles as profiles
from datamanager.services import regions as region_service
from datamanager.stages import pelias_interpolation as stage
from datamanager.stages import status as stage_status
from datamanager.stages.registry import default_registry

# a "node" that records its stdin and writes the databases the real scripts would create
FAKE_NODE = """#!/usr/bin/env python3
import os, sqlite3, sys
script = os.path.basename(sys.argv[1])
data = sys.stdin.buffer.read() if not sys.stdin.isatty() else b""
open(os.path.join(os.environ["CALLS_DIR"], script + ".stdin"), "wb").write(data)
open(os.path.join(os.environ["CALLS_DIR"], "calls.log"), "a").write(script + " " + " ".join(sys.argv[2:]) + "\\n")
if script == os.environ.get("FAIL_SCRIPT"):
    sys.stderr.write("boom in " + script + "\\n")
    sys.exit(1)
if script == "polyline.js":
    c = sqlite3.connect(sys.argv[2]); c.execute("CREATE TABLE polyline (id)"); c.execute("CREATE TABLE names (id)")
    c.execute("INSERT INTO polyline VALUES (1)"); c.execute("INSERT INTO polyline VALUES (2)"); c.commit()
elif script in ("oa.js", "osm.js"):
    c = sqlite3.connect(sys.argv[2]); c.execute("CREATE TABLE IF NOT EXISTS address (id)")
    c.execute("INSERT INTO address VALUES (1)"); c.commit()
os.write(3, b"skipped record\\n") if script == "oa.js" else None
"""

FAKE_PBF2JSON = "#!/bin/sh\necho '{\"id\":1}'\n"


def _script(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


@pytest.fixture()
def env(tmp_path, monkeypatch):
    calls = tmp_path / "calls"
    calls.mkdir()
    monkeypatch.setenv("CALLS_DIR", str(calls))
    node = _script(tmp_path / "bin" / "node", FAKE_NODE)
    tools_dir = tmp_path / "pelias-tools"
    (tools_dir / "interpolation" / "cmd").mkdir(parents=True)
    _script(pelias_interpolation.pbf2json_binary(tools_dir / "interpolation"), FAKE_PBF2JSON)
    monkeypatch.setattr(stage, "node_bin", lambda session: str(node))
    monkeypatch.setattr(pelias_build, "detect", lambda session: {"status": "ok", "message": "", "path": str(tools_dir), "version": "abc"})
    monkeypatch.setattr(pelias_build, "require", lambda session, name: tools_dir / name)
    monkeypatch.setattr(pelias_build, "version", lambda session=None: "commits-1")
    return type("Env", (), {"calls": calls, "monkeypatch": monkeypatch, "tools_dir": tools_dir})


def _asset(session, run, asset_type, name, filename, content, config_id):
    file = assets.asset_dir("test", run.id) / filename
    file.write_bytes(content)
    asset = assets.create(session, run.id, config_id, asset_type, name, file)
    asset.status = "approved"
    session.commit()


def _oa_record(session, source, lines: list[dict]):
    directory = Path(config.DATA_ROOT) / "downloads" / "openaddresses" / source
    directory.mkdir(parents=True, exist_ok=True)
    file = directory / "source.geojson.gz"
    file.write_bytes(gzip.compress(("\n".join(json.dumps(x) for x in lines) + "\n").encode()))
    session.add(DownloadRecord(source_key=f"openaddresses:{source}", version_label="20260101T000000Z", url="x", filename=file.name,
                               local_path=str(file.relative_to(config.DATA_ROOT)), size_bytes=1, content_hash=f"h-{source}", status="approved",
                               fetched_at=datetime.datetime(2026, 1, 1)))
    session.commit()


def _feature(number, street, lon=4.0, lat=50.0, **extra):
    return {"type": "Feature", "properties": {"hash": f"h{number}{street}", "number": number, "street": street, "city": "Gent", **extra},
            "geometry": {"type": "Point", "coordinates": [lon, lat]}}


@pytest.fixture()
def cfg(app, env):
    from datamanager.country_sources import SourceState
    from datamanager.services import address_sources as address_service

    session = SessionLocal()
    session.add(Country(iso2="aa", name="AA", ne_geometry_ref="x", bbox_json=[0, 0, 1, 1], geofabrik_path="europe/aa", wof_code="aa", srtm_bbox_json=[50, 51, 0, 1]))
    session.commit()
    address_service.set_state(session.get(Country, "aa"), "openaddresses", SourceState(True, {"files": ["aa/countrywide", "aa/missing"]}))
    session.commit()
    profile = profiles.create_profile(session, "dev")
    region = region_service.create_region(session, profile.id, "Region One")
    region_service.assign_country(session, region, "aa")
    run = runs.create_run(session, "osm-extract", profile.id)
    _asset(session, run, "osm-pbf", "region-one", "region-one.osm.pbf", b"pbf", profile.id)
    poly = b"log line without separators\n" + b"1\x00encoded\x00Main Street\n" + b"2\x00encoded\x00Side Road\n"
    _asset(session, run, "valhalla-polylines", "region-one", "polylines.0sv.gz", gzip.compress(poly), profile.id)
    _oa_record(session, "aa/countrywide", [_feature("2", "Zeta Road"), _feature("1", "Alpha Road"), _feature("1", "Alpha Road"), _feature("", "No Number"),
                                           _feature("5", "Alpha Road", unit="B")])
    return profile


def _resolved(profile_id):
    from datamanager.services import resolve

    return resolve.to_dict(resolve.resolve_config(SessionLocal(), profile_id))


def _run(profile_id, params=None):
    run = runs.create_run(SessionLocal(), "pelias-interpolation", profile_id, params=params)
    result = tasks.run_stage(run.id)
    session = SessionLocal()
    session.refresh(run)
    return run, result


def test_csv_conversion_filters_sorts_and_keeps_the_header(tmp_path):
    source = tmp_path / "a.geojson.gz"
    source.write_bytes(gzip.compress("\n".join(json.dumps(f) for f in [_feature("2", "Zeta Road"), _feature("10", "Alpha Road"), _feature("9", "Alpha Road"),
                                                                    _feature("", "x"), {"broken": 1}]).encode() + b"\nnot json\n"))
    result = pelias_interpolation.openaddresses_csv({"aa/x": source}, tmp_path / "out.csv", tmp_path / "tmp")
    assert result == {"rows": 3, "skipped": 3}
    lines = (tmp_path / "out.csv").read_text().splitlines()
    assert lines[0] == "LON,LAT,NUMBER,STREET,UNIT,CITY,DISTRICT,REGION,POSTCODE,ID,HASH"
    assert [line.split(",")[3] + line.split(",")[2] for line in lines[1:]] == ["Alpha Road9", "Alpha Road10", "Zeta Road2"]
    assert lines[1].endswith("aa/x:h9Alpha Road")


def test_csv_has_no_quotes_or_commas_in_fields(tmp_path):
    source = tmp_path / "a.geojson.gz"
    source.write_bytes(gzip.compress(json.dumps(_feature("1", 'CALLE "PASIONARIA", EMILIO', city='A "B" C')).encode() + b"\n"))
    pelias_interpolation.openaddresses_csv({"es/x": source}, tmp_path / "out.csv", tmp_path / "tmp")
    body = (tmp_path / "out.csv").read_text().splitlines()[1]
    assert '"' not in body and body.split(",")[3] == "CALLE PASIONARIA EMILIO" and len(body.split(",")) == 11


def test_a_script_that_stops_reading_is_killed(tmp_path, monkeypatch):
    stuck = tmp_path / "node"
    stuck.write_text("#!/bin/sh\nexec sleep 60\n")
    stuck.chmod(stuck.stat().st_mode | stat.S_IEXEC)
    (tmp_path / "repo" / "cmd").mkdir(parents=True)
    monkeypatch.setattr(pelias_interpolation, "STALL_SECONDS", 0.5)
    lines = (b"x" * 1000 + b"\n" for _ in range(200000))  # more than a pipe holds
    with pytest.raises(pelias_interpolation.BuildError, match="stalled"):
        pelias_interpolation.run_script(str(stuck), tmp_path / "repo", "oa.js", ["a", "b"], tmp_path / "logs", "oa", tmp_path / "tmp", lines)


def test_plan_lists_inputs_warnings_and_blocked_reasons(cfg, env):
    plan = stage.plan_inputs(SessionLocal(), cfg.id, _resolved(cfg.id))
    assert plan.problems == [] and plan.regions[0].fingerprint and list(plan.regions[0].openaddresses) == ["aa/countrywide"]
    assert any("aa/missing" in w for w in plan.regions[0].warnings)
    env.monkeypatch.setattr(pelias_build, "detect", lambda session: {"status": "missing", "message": "Not built yet.", "path": None, "version": None})
    assert any("importers are not built" in p for p in stage.plan_inputs(SessionLocal(), cfg.id, _resolved(cfg.id)).problems)


def test_builds_both_databases_in_order_and_feeds_the_scripts(cfg, env):
    run, result = _run(cfg.id)
    report = run.report_json
    assert result["status"] == "awaiting_review", report
    calls = [line.split()[0] for line in (env.calls / "calls.log").read_text().splitlines()]
    assert calls == ["polyline.js", "oa.js", "osm.js", "vertices.js"]
    assert (env.calls / "polyline.js.stdin").read_bytes() == b"1\x00encoded\x00Main Street\n2\x00encoded\x00Side Road\n"  # log line dropped
    oa = (env.calls / "oa.js.stdin").read_text().splitlines()
    assert oa[0].startswith("LON,LAT") and len(oa) == 4  # header + 3 usable rows, the duplicate collapsed
    assert [x.split(",")[3] for x in oa[1:]] == ["Alpha Road", "Alpha Road", "Zeta Road"] and (env.calls / "osm.js.stdin").read_text().strip() == '{"id":1}'
    entry = report["interpolation"][0]
    assert entry["result"] == "built" and entry["counts"]["streets"] == 2 and entry["counts"]["addresses"] == 2
    assert [s["name"] for s in entry["steps"]] == ["polyline", "openaddresses-csv", "oa", "osm", "vertices"]
    assert next(s for s in entry["steps"] if s["name"] == "oa")["skipped"] == 1
    assert not (Path(config.DATA_ROOT) / "work" / str(run.id)).exists()
    runs.approve(SessionLocal(), runs.get_run(SessionLocal(), run.id))
    street = assets.current(SessionLocal(), cfg.id, "pelias-interpolation-street-db", "region-one")
    address = assets.current(SessionLocal(), cfg.id, "pelias-interpolation-address-db", "region-one")
    assert assets.abs_path(street).name == "street.db" and street.meta_json["counts"]["streets"] == 2
    assert sqlite3.connect(assets.abs_path(address)).execute("SELECT COUNT(*) FROM address").fetchone()[0] == 2


def test_step_failure_keeps_no_assets_and_reports_the_error(cfg, env):
    env.monkeypatch.setenv("FAIL_SCRIPT", "osm.js")
    run, result = _run(cfg.id)
    entry = run.report_json["interpolation"][0]
    assert result["status"] == "failed" and entry["status"] == "failed" and "boom in osm.js" in entry["message"]
    assert assets.current(SessionLocal(), cfg.id, "pelias-interpolation-street-db", "region-one") is None
    assert not (Path(config.DATA_ROOT) / "work" / str(run.id)).exists()


def test_unchanged_region_is_skipped_and_status_follows(cfg, env):
    assert stage_status.compute(SessionLocal(), cfg.id, _resolved(cfg.id), {})["pelias-interpolation"].state == "todo"
    run, _ = _run(cfg.id)
    runs.approve(SessionLocal(), runs.get_run(SessionLocal(), run.id))
    assert stage_status.compute(SessionLocal(), cfg.id, _resolved(cfg.id), {})["pelias-interpolation"].state == "ok"
    before = (env.calls / "calls.log").read_text()
    run, _ = _run(cfg.id)
    assert run.report_json["interpolation"][0]["result"] == "unchanged" and (env.calls / "calls.log").read_text() == before
    runs.approve(SessionLocal(), runs.get_run(SessionLocal(), run.id))
    env.monkeypatch.setattr(pelias_build, "version", lambda session=None: "commits-2")
    assert stage_status.compute(SessionLocal(), cfg.id, _resolved(cfg.id), {})["pelias-interpolation"].state == "outdated"


def test_registry_pulls_in_both_producers_and_card_shows(cfg, client):
    order = default_registry.resolve_order(["pelias-interpolation"])
    assert order.index("osm-extract") < order.index("pelias-interpolation") and order.index("valhalla") < order.index("pelias-interpolation")
    assert "Pelias interpolation" in client.get("/build/").get_data(as_text=True)
