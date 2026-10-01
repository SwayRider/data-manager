import datetime
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from datamanager.config import config
from datamanager.db import SessionLocal
from datamanager.jobs import tasks
from datamanager.models import Asset, Country, DownloadRecord
from datamanager.services import assets, downloads, osmium, resolve, runs
from datamanager.services import carve as carve_service
from datamanager.services import config_profiles as profiles
from datamanager.services import regions as region_service
from datamanager.services import settings as settings_service
from datamanager.stages.osm_extract import plan_inputs

pytestmark = pytest.mark.skipif(shutil.which("osmium") is None, reason="osmium not installed")

# aa and bb are ~11 km apart, so each is in the other's overlap zone.
BOXES = {"aa": (0, 50, 1, 51), "bb": (1.15, 50, 2.15, 51)}
WEST_HALF = {"type": "Polygon", "coordinates": [[[-1, 49], [0.5, 49], [0.5, 52], [-1, 52], [-1, 49]]]}
# node id -> (lat, lon, version) per country download. Node 100 is a shared border object: v1 in aa, v2 in bb.
NODES = {
    "aa": {1: (50.5, 0.2, 1), 2: (50.5, 0.8, 1), 3: (55.0, 0.5, 1), 100: (50.5, 1.1, 1)},
    "bb": {10: (50.5, 1.5, 1), 100: (50.5, 1.1, 2)},
}


def _write_geo(iso):
    x0, y0, x1, y1 = BOXES[iso]
    with open(config.DATA_ROOT + f"/geo-{iso}.geojson", "w") as f:
        json.dump({"type": "Polygon", "coordinates": [[[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]]}, f)
    return f"geo-{iso}.geojson"


def _pbf(iso) -> Path:
    label = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
    directory = Path(config.DATA_ROOT) / "downloads" / "osm" / "europe" / iso / label
    directory.mkdir(parents=True)
    xml = ["<?xml version='1.0' encoding='UTF-8'?><osm version='0.6'>"]
    for nid, (lat, lon, version) in NODES[iso].items():
        xml.append(f"<node id='{nid}' version='{version}' timestamp='2024-01-0{version}T00:00:00Z' lat='{lat}' lon='{lon}'/>")
    xml.append("</osm>")
    (directory / "f.osm").write_text("".join(xml))
    subprocess.run(["osmium", "cat", str(directory / "f.osm"), "-o", str(directory / "f.osm.pbf")], check=True)
    return directory / "f.osm.pbf"


@pytest.fixture()
def cfg(app):
    session = SessionLocal()
    for iso in BOXES:
        session.add(Country(iso2=iso, name=iso.upper(), ne_geometry_ref=_write_geo(iso), bbox_json=[0, 0, 1, 1],
                            geofabrik_path=f"europe/{iso}", wof_code=iso, srtm_bbox_json=[50, 51, 0, 1]))
    session.commit()
    profile = profiles.create_profile(session, "dev")
    for name, iso in (("Region One", "aa"), ("r2", "bb")):
        region_service.assign_country(session, region_service.create_region(session, profile.id, name), iso)
    profile_id = profile.id
    settings_service.save(session, {"osm.source": "geofabrik"})  # the planet route has its own tests below
    for iso in BOXES:
        file = _pbf(iso)
        session.add(DownloadRecord(
            source_key=f"osm:europe/{iso}", version_label=file.parent.name, url="http://x", filename="f.osm.pbf",
            local_path=str(file.relative_to(config.DATA_ROOT)), size_bytes=file.stat().st_size, content_hash=iso,
            fetched_at=datetime.datetime.now(datetime.UTC).replace(tzinfo=None), status="approved"))
    session.commit()
    return profile_id


def _stage(profile_id, key, params=None, approve=True):
    session = SessionLocal()
    run = runs.create_run(session, key, profile_id, params=params)
    result = tasks.run_stage(run.id)
    if approve and result["status"] == "awaiting_review":
        runs.approve(SessionLocal(), runs.get_run(SessionLocal(), run.id))
    return run.id, result


def _node_ids(asset):
    out = subprocess.run(["osmium", "cat", str(assets.abs_path(asset)), "-f", "opl"], capture_output=True, text=True, check=True).stdout
    return sorted(int(line.split()[0][1:]) for line in out.splitlines() if line.startswith("n"))


def _current(profile_id, kind, name):
    return assets.current(SessionLocal(), profile_id, kind, name)


def test_needs_approved_polygons_first(cfg):
    _, result = _stage(cfg, "osm-extract")
    run = runs.get_run(SessionLocal(), 1)
    assert result["status"] == "failed" and "overlap polygon" in run.error_message


def test_needs_approved_downloads(cfg):
    _stage(cfg, "polygons")
    SessionLocal().query(DownloadRecord).filter_by(source_key="osm:europe/bb").update({"status": "fetched"})
    SessionLocal().commit()
    resolved = resolve.to_dict(resolve.resolve_config(SessionLocal(), cfg))
    _, problems = plan_inputs(SessionLocal(), cfg, resolved)
    assert any("no approved download of europe/bb" in p for p in problems)


def test_builds_core_and_region_files_clipped_and_deduplicated(cfg):
    _stage(cfg, "polygons")
    run_id, result = _stage(cfg, "osm-extract")
    assert result["status"] == "awaiting_review"
    report = runs.get_run(SessionLocal(), run_id).report_json
    assert report["summary"]["regions"] == 2 and report["summary"]["failed"] == 0

    core_one, one = _current(cfg, "osm-core-pbf", "region-one-core"), _current(cfg, "osm-pbf", "region-one")
    assert _node_ids(core_one) == [1, 2, 3, 100]  # core country kept whole, far node included
    assert _node_ids(one) == [1, 2, 3, 10, 100]  # bb clipped to the overlap polygon, shared node once
    r2 = _current(cfg, "osm-pbf", "r2")
    assert _node_ids(r2) == [1, 2, 10, 100]  # aa's far-away node 3 is outside r2's 100 km overlap
    info = osmium.fileinfo("osmium", assets.abs_path(one))
    assert info["multiple_versions"] is False and info["counts"]["nodes"] == 5
    assert one.source_download_ids and one.meta_json["counts"]["nodes"] == 5
    assert not (Path(config.DATA_ROOT) / "work" / str(run_id)).exists()
    assert not report["regions"][0]["warnings"]


def test_shared_border_object_keeps_the_newest_version(cfg):
    _stage(cfg, "polygons")
    _stage(cfg, "osm-extract")
    out = subprocess.run(["osmium", "cat", str(assets.abs_path(_current(cfg, "osm-pbf", "region-one"))), "-f", "opl"],
                         capture_output=True, text=True, check=True).stdout
    assert "n100 v2" in out and "n100 v1" not in out


def test_single_region_run(cfg):
    _stage(cfg, "polygons")
    run_id, _ = _stage(cfg, "osm-extract", {"regions": ["r2"]})
    names = {a.name for a in assets.for_run(SessionLocal(), run_id)}
    assert names == {"r2", "r2-core"}


def test_unknown_region_fails(cfg):
    _stage(cfg, "polygons")
    _, result = _stage(cfg, "osm-extract", {"regions": ["nope"]})
    assert result["status"] == "failed"


def test_carved_country_is_clipped(cfg):
    carve_service.set_carve(SessionLocal(), cfg, "aa", WEST_HALF)
    _stage(cfg, "polygons")
    _stage(cfg, "osm-extract")
    assert _node_ids(_current(cfg, "osm-core-pbf", "region-one-core")) == [1]  # lon 0.2 only; 0.8 and 55N are out
    assert 3 not in _node_ids(_current(cfg, "osm-pbf", "r2"))


def test_reject_removes_files(cfg):
    _stage(cfg, "polygons")
    run_id, _ = _stage(cfg, "osm-extract", approve=False)
    stored = assets.for_run(SessionLocal(), run_id)
    paths = [assets.abs_path(a) for a in stored]
    assert all(p.exists() for p in paths)
    runs.reject(SessionLocal(), runs.get_run(SessionLocal(), run_id))
    assert not any(p.exists() for p in paths)
    assert _current(cfg, "osm-pbf", "region-one") is None


def test_download_override_pins_a_version(cfg):
    session = SessionLocal()
    old = downloads.versions(session, "osm:europe/bb")[0]
    resolved = resolve.to_dict(resolve.resolve_config(session, cfg))
    plans, _ = plan_inputs(session, cfg, resolved, overrides={"osm:europe/bb": old.id})
    assert all(i.record.id == old.id for p in plans for i in p.inputs if i.source_key == "osm:europe/bb")


def test_downloads_used_by_assets_survive_cleanup(cfg):
    session = SessionLocal()
    _stage(cfg, "polygons")
    _stage(cfg, "osm-extract")
    used = {i for a in session.query(Asset) for i in a.source_download_ids}
    assert used and used <= downloads.protected_ids(session, "osm:europe/aa", 0) | downloads.protected_ids(session, "osm:europe/bb", 0)


def _planet_assets(profile_id):
    """Approved country-pbf assets (as the Extract countries stage leaves them) built from the same PBFs."""
    session = SessionLocal()
    settings_service.save(session, {"osm.source": "planet"})
    run = runs.create_run(session, "extract-countries", profile_id)
    for iso in BOXES:
        record = downloads.versions(session, f"osm:europe/{iso}")[0]
        file = Path(config.DATA_ROOT) / "library" / "assets" / "country-pbf" / str(run.id) / f"europe_{iso}.osm.pbf"
        file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(downloads.abs_path(record), file)
        assets.create(session, run.id, None, "country-pbf", f"europe/{iso}", file,
                      meta={"planet_version": "20260920T000000Z", "planet_data_timestamp": "2026-09-20T00:00:00Z"},
                      source_download_ids=[record.id])
    runs.mark_running(session, run)
    runs.finish(session, run, {})
    runs.approve(session, run)


def test_planet_source_needs_country_extracts(cfg):
    SessionLocal()
    settings_service.save(SessionLocal(), {"osm.source": "planet"})
    _, result = _stage(cfg, "osm-extract")
    run = runs.get_run(SessionLocal(), 1)
    assert result["status"] == "failed" and "Extract countries" in run.error_message


def test_planet_source_builds_from_country_assets(cfg):
    _stage(cfg, "polygons")
    _planet_assets(cfg)
    run_id, result = _stage(cfg, "osm-extract")
    report = runs.get_run(SessionLocal(), run_id).report_json
    assert result["status"] == "awaiting_review" and report["summary"]["failed"] == 0
    assert _node_ids(_current(cfg, "osm-pbf", "region-one")) == [1, 2, 3, 10, 100]
    first = report["regions"][0]["inputs"][0]
    assert first["from_planet"] and first["version"] == "20260920T000000Z" and first["asset_id"]
    region = _current(cfg, "osm-pbf", "region-one")
    assert region.meta_json["country_asset_ids"] and region.source_download_ids
