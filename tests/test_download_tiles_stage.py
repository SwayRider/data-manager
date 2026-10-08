import gzip
import json
import struct

import pytest

from datamanager.db import SessionLocal
from datamanager.jobs import tasks
from datamanager.services import downloads, pmtiles, runs
from datamanager.services import settings as settings_service
from tests.services.test_downloads import Upstream  # noqa: F401

LAYERS = ["boundaries", "buildings", "earth", "landuse", "natural", "places", "pois", "roads", "transit", "water"]


def make_pmtiles(layers=LAYERS, max_zoom=15, tile_type=1, bounds=(-180.0, -85.0, 180.0, 85.0), padding=100) -> bytes:
    """A minimal PMTiles v3 file: header, gzip JSON metadata, some filler for the directories/tile data."""
    metadata = gzip.compress(json.dumps({"name": "test", "vector_layers": [{"id": i} for i in layers]}).encode())
    header = bytearray(b"PMTiles" + bytes([3]))
    header += struct.pack("<11Q", 127, 0, 127, len(metadata), 0, 0, 127 + len(metadata), padding, 10, 10, 10)
    header += struct.pack("<6B", 1, 2, 2, tile_type, 0, max_zoom)
    header += struct.pack("<4i", *(int(v * 1e7) for v in bounds))
    header += struct.pack("<Bii", 3, 0, 0)
    assert len(header) == 127
    return bytes(header) + metadata + b"\0" * padding


@pytest.fixture()
def upstream():
    server = Upstream()
    yield server
    server.close()


@pytest.fixture()
def builds(app, upstream):
    session = SessionLocal()
    settings_service.save(session, {
        "download.tiles_builds": upstream.base + "/builds.json", "download.tiles_build": upstream.base + "/",
    })
    upstream.set_build = lambda key, data, etag: (
        upstream.files.__setitem__("/" + key, (data, etag)),
        upstream.files.__setitem__("/builds.json", (json.dumps([{"key": key, "size": len(data), "version": "4.2.0"}]).encode(), '"b"')),
    )
    upstream.set_build("20260930.pmtiles", make_pmtiles(), '"t1"')
    return upstream


def _run(**params):
    session = SessionLocal()
    run = runs.create_run(session, "download-tiles", None, params=params)
    result = tasks.run_stage(run.id)
    return run.id, result


def _report(run_id):
    return runs.get_run(SessionLocal(), run_id).report_json


def test_header_and_metadata_are_read(tmp_path):
    path = tmp_path / "t.pmtiles"
    path.write_bytes(make_pmtiles())
    info = pmtiles.read_info(path)
    assert info["max_zoom"] == 15 and info["tile_type"] == "mvt" and info["layers"] == sorted(LAYERS)
    assert info["bounds"] == [-180.0, -85.0, 180.0, 85.0]


def test_not_a_pmtiles_file_is_rejected(tmp_path):
    path = tmp_path / "x.pmtiles"
    path.write_bytes(b"nope" * 100)
    with pytest.raises(pmtiles.PmtilesError):
        pmtiles.read_info(path)


def test_downloads_the_newest_build_and_awaits_review(builds):
    run_id, result = _run()
    report = _report(run_id)
    assert result["status"] == "awaiting_review"
    assert report["summary"]["status"] == "downloaded" and report["summary"]["version"]
    assert report["tiles"]["max_zoom"] == 15 and report["tiles"]["schema_version"] == "4.2.0"
    assert report["file"]["filename"] == "20260930.pmtiles"
    assert downloads.resolve_version(SessionLocal(), "tiles:planet") is None
    runs.approve(SessionLocal(), runs.get_run(SessionLocal(), run_id))
    assert downloads.resolve_version(SessionLocal(), "tiles:planet").data_timestamp == "2026-09-30"


def test_unchanged_build_is_not_downloaded_again(builds):
    first, _ = _run()
    runs.approve(SessionLocal(), runs.get_run(SessionLocal(), first))
    second, _ = _run()
    assert _report(second)["summary"]["status"] == "unchanged"
    assert len(downloads.versions(SessionLocal(), "tiles:planet")) == 1


def test_newer_build_replaces_the_old_one_after_approval(builds):
    settings_service.save(SessionLocal(), {"download.tiles_keep": 1})
    first, _ = _run()
    runs.approve(SessionLocal(), runs.get_run(SessionLocal(), first))
    builds.set_build("20261001.pmtiles", make_pmtiles(padding=200), '"t2"')
    second, _ = _run()
    assert _report(second)["summary"]["status"] == "downloaded"
    runs.approve(SessionLocal(), runs.get_run(SessionLocal(), second))
    found = downloads.versions(SessionLocal(), "tiles:planet")
    assert len(found) == 1 and found[0].filename == "20261001.pmtiles"  # download.tiles_keep = 1 (default is 2)


def test_wrong_schema_and_zoom_give_warnings(builds):
    builds.set_build("20260930.pmtiles", make_pmtiles(layers=["water"], max_zoom=12), '"t9"')
    run_id, _ = _run()
    warnings = " ".join(_report(run_id)["warnings"])
    assert "Maximum zoom is 12" in warnings and "Layers missing" in warnings


def test_raster_tiles_fail_the_run(builds):
    builds.set_build("20260930.pmtiles", make_pmtiles(tile_type=2), '"r"')
    run_id, result = _run()
    assert result["status"] == "failed" and "vector" in _report(run_id)["error"]


def test_local_file_is_registered_in_place(app, tmp_path):
    path = tmp_path / "20260901.pmtiles"
    path.write_bytes(make_pmtiles())
    run_id, result = _run(local_file=str(path))
    assert result["status"] == "awaiting_review" and _report(run_id)["summary"]["status"] == "imported"
    assert path.exists()


def test_build_list_failure_falls_back_to_dated_probe(builds):
    builds.files.pop("/builds.json")
    import datetime
    today = datetime.datetime.now(datetime.UTC).strftime("%Y%m%d") + ".pmtiles"
    builds.files["/" + today] = (make_pmtiles(), '"d"')
    run_id, result = _run()
    assert result["status"] == "awaiting_review" and _report(run_id)["file"]["filename"] == today


def test_two_builds_are_kept_by_default(builds):
    first, _ = _run()
    runs.approve(SessionLocal(), runs.get_run(SessionLocal(), first))
    builds.set_build("20261001.pmtiles", make_pmtiles(padding=200), '"t2"')
    second, _ = _run()
    runs.approve(SessionLocal(), runs.get_run(SessionLocal(), second))
    assert len(downloads.versions(SessionLocal(), "tiles:planet")) == 2
