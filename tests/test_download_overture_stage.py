import io
import json
import stat
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from datamanager.country_sources import SourceState
from datamanager.db import SessionLocal
from datamanager.jobs import tasks
from datamanager.models import Asset, Country, DownloadRecord
from datamanager.services import assets, overture, runs, tools
from datamanager.services import address_sources as address_service
from datamanager.services import config_profiles as profiles
from datamanager.services import regions as region_service
from datamanager.stages import download_overture as stage
from datamanager.stages import status as stage_status
from tests.services.test_downloads import Upstream  # noqa: F401

FAKE_OVERTURE = """#!/bin/sh
echo "$@" >> "$OVERTURE_LOG"
if [ "$1" = "releases" ]; then echo "2026-09-23.1"; exit 0; fi
[ -n "$OVERTURE_FAILS" ] && [ "$OVERTURE_FAILS" = "$(echo "$@" | sed 's/.*--type \\([a-z]*\\).*/\\1/')" ] && { echo "boom" >&2; exit 3; }
while [ $# -gt 0 ]; do case "$1" in --type) type=$2;; -o) out=$2;; esac; shift; done
if [ "$type" = "place" ]; then
  echo '{"id":"p1","type":"Feature","geometry":{"type":"Point","coordinates":[6.1,49.6]},"properties":{"names":{"primary":"Cafe"},"taxonomy":{"primary":"cafe"},"addresses":[{"freeform":"44 Av. Pasteur","locality":"Luxembourg","postcode":"2310","country":"AA"}]}}' > "$out"
  echo '{"id":"p2","type":"Feature","geometry":{"type":"Point","coordinates":[6.2,49.6]},"properties":{"names":{"primary":"Elsewhere"},"addresses":[{"country":"ZZ"}]}}' >> "$out"
  echo '{"id":"p3","type":"Feature","geometry":{"type":"Point","coordinates":[6.2,49.6]},"properties":{"addresses":[]}}' >> "$out"
else
  echo '{"id":"a1","type":"Feature","geometry":{"type":"Point","coordinates":[6.1,49.6]},"properties":{"street":"Rue X","number":"5","postcode":"1950","address_levels":[{"value":"Lux"}],"country":"AA"}}' > "$out"
  echo '{"id":"a2","type":"Feature","geometry":{"type":"Point","coordinates":[6.1,49.6]},"properties":{"street":"","number":"5","country":"AA"}}' >> "$out"
fi
echo state > "$out.state"
"""


def _zip(with_stops=True) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("stops.txt" if with_stops else "other.txt", "stop_id,stop_name\n1,A\n")
    return buffer.getvalue()


@pytest.fixture()
def upstream():
    server = Upstream()
    yield server
    server.close()


@pytest.fixture()
def env(tmp_path, monkeypatch):
    binary = tmp_path / "bin" / "overturemaps"
    binary.parent.mkdir()
    binary.write_text(FAKE_OVERTURE)
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("OVERTURE_LOG", str(tmp_path / "overture.log"))
    real = tools.detect
    monkeypatch.setattr(tools, "detect", lambda session, key, force=False: SimpleNamespace(ok=True, path=str(binary), message="", status="ok")
                        if key == "overturemaps" else real(session, key, force=force))
    return SimpleNamespace(log=tmp_path / "overture.log", monkeypatch=monkeypatch)


def _polygon(session, run, kind, name, bounds):
    directory = assets.asset_dir("polygons", run.id)
    poly = directory / f"{name}.poly"
    poly.write_text("x")
    west, south, east, north = bounds
    ring = [[west, south], [east, south], [east, north], [west, north], [west, south]]
    (directory / f"{name}.geojson").write_text(json.dumps({"type": "Polygon", "coordinates": [ring]}))
    asset = assets.create(session, run.id, run.config_profile_id, f"{kind}-polygon", name, poly)
    asset.status = "approved"
    session.commit()


@pytest.fixture()
def cfg(app, env, upstream):
    session = SessionLocal()
    session.add(Country(iso2="aa", name="AA", ne_geometry_ref="x", bbox_json=[0, 0, 1, 1], geofabrik_path="europe/aa", wof_code="aa", srtm_bbox_json=[50, 51, 0, 1]))
    session.commit()
    address_service.set_state(session.get(Country, "aa"), "overture", SourceState(enabled=True))
    session.commit()
    profile = profiles.create_profile(session, "dev")
    region = region_service.create_region(session, profile.id, "Region One")
    region_service.assign_country(session, region, "aa")
    region_service.add_gtfs_feed(session, region, upstream.base + "/gtfs/a.zip", "Feed A")
    upstream.files["/gtfs/a.zip"] = (_zip(), '"g1"')
    run = runs.create_run(session, "polygons", profile.id)
    _polygon(session, run, "core", "region-one-core", (5.0, 49.0, 7.0, 50.0))
    return profile


def _resolved(profile_id):
    from datamanager.services import resolve

    return resolve.to_dict(resolve.resolve_config(SessionLocal(), profile_id))


def _run(profile_id, params=None):
    run = runs.create_run(SessionLocal(), "download-overture-gtfs", profile_id, params=params)
    result = tasks.run_stage(run.id)
    session = SessionLocal()
    session.refresh(run)
    return run, result


def test_conversions_follow_the_current_schema(tmp_path):
    source = tmp_path / "p.seq"
    source.write_text('{"id":"1","geometry":{"coordinates":[6,49]},"properties":{"names":{"primary":"N"},"basic_category":"bar","addresses":[{"freeform":"Rue Zithe 12","country":"lu"}]}}\n'
                      'not json\n{"id":"2","geometry":{"coordinates":[6,49]},"properties":{"names":{"primary":"Far"},"addresses":[{"country":"de"}]}}\n')
    counts = overture.places_to_csv(source, tmp_path / "p.csv", {"LU"})
    assert counts == {"rows": 1, "skipped": 1}
    row = (tmp_path / "p.csv").read_text().splitlines()[1].split(",")
    assert row[1] == "N" and row[6] == "bar" and row[7] == "12" and row[8] == "Rue Zithe"
    assert overture.split_freeform("44 Av. Pasteur") == ("44", "Av. Pasteur") and overture.split_freeform("Place d'Armes") == ("", "")


def test_plan_needs_an_approved_polygon(cfg):
    assert stage.plan_inputs(SessionLocal(), cfg.id, _resolved(cfg.id)).problems == []
    SessionLocal().query(Asset).update({"status": "produced"})
    SessionLocal().commit()
    assert any("no approved core polygon" in p for p in stage.plan_inputs(SessionLocal(), cfg.id, _resolved(cfg.id)).problems)


def test_downloads_converts_registers_and_skips_an_unchanged_release(cfg, env):
    run, result = _run(cfg.id)
    assert result["status"] == "awaiting_review", run.report_json
    rows = {s["key"]: s for s in run.report_json["overture_sources"]}
    assert rows["overture:region-one-places"]["rows"] == 1 and rows["overture:region-one-places"]["skipped"] == 2
    assert rows["overture:region-one-addresses"]["rows"] == 1 and rows["gtfs:region-one-1"]["status"] == "downloaded"
    calls = env.log.read_text().splitlines()
    assert any("--bbox 5.000000,49.000000,7.000000,50.000000" in c and "-f geojsonseq" in c and "-r 2026-09-23.1" in c for c in calls)
    record = SessionLocal().query(DownloadRecord).filter_by(source_key="overture:region-one-places").one()
    text = (Path(stage.config.DATA_ROOT) / record.local_path).read_text()
    assert text.splitlines()[0].startswith("id,name,lat,lon") and "Cafe" in text and "Elsewhere" not in text
    runs.approve(SessionLocal(), runs.get_run(SessionLocal(), run.id))
    assert {r.status for r in SessionLocal().query(DownloadRecord).filter(DownloadRecord.source_key.like("overture:%") | DownloadRecord.source_key.like("gtfs:%"))} == {"approved"}
    assert stage_status.compute(SessionLocal(), cfg.id, _resolved(cfg.id), {})["download-overture-gtfs"].state == "ok"
    again, _ = _run(cfg.id)
    assert {s["status"] for s in again.report_json["overture_sources"]} == {"unchanged"}
    assert len([c for c in env.log.read_text().splitlines() if c.startswith("download")]) == 2  # nothing fetched again


def test_failures_are_warnings(cfg, env, upstream):
    env.monkeypatch.setenv("OVERTURE_FAILS", "address")
    upstream.files["/gtfs/a.zip"] = (_zip(with_stops=False), '"g2"')
    run, result = _run(cfg.id)
    statuses = {s["key"]: s["status"] for s in run.report_json["overture_sources"]}
    assert result["status"] == "awaiting_review" and statuses == {"overture:region-one-places": "downloaded", "overture:region-one-addresses": "failed", "gtfs:region-one-1": "invalid"}
    assert len(run.report_json["warnings"]) == 2


def test_registry_pulls_in_polygons_and_the_card_shows(cfg, client):
    from datamanager.stages.registry import default_registry

    assert default_registry.resolve_order(["download-overture-gtfs"]) == ["polygons", "download-overture-gtfs"]
    assert "Download Overture" in client.get("/build/").get_data(as_text=True)
