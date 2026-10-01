import gzip

import pytest

from datamanager.config import config
from datamanager.db import SessionLocal
from datamanager.jobs import tasks
from datamanager.models import Country, DownloadRecord
from datamanager.services import config_profiles as profiles
from datamanager.services import downloads, runs, srtm
from datamanager.services import regions as region_service
from datamanager.services import settings as settings_service
from datamanager.stages import status as stage_status
from tests.services.test_downloads import Upstream  # noqa: F401

TILE_BYTES = b"\x00\x01" * 1000


@pytest.fixture()
def upstream():
    server = Upstream()
    yield server
    server.close()


@pytest.fixture()
def cfg(app, upstream):
    session = SessionLocal()
    settings_service.save(session, {"download.srtm": upstream.base + "/"})
    session.add(Country(iso2="aa", name="AA", ne_geometry_ref="x", bbox_json=[0, 0, 1, 1], geofabrik_path="europe/aa",
                        wof_code="aa", srtm_bbox_json=[50, 51, 0, 1]))
    session.commit()
    profile = profiles.create_profile(session, "dev")
    region_service.assign_country(session, region_service.create_region(session, profile.id, "r1"), "aa")
    for name in ("N50E000", "N50E001", "N51E000"):  # N51E001 is open sea: 404
        upstream.files[f"/{name[:3]}/{name}.hgt.gz"] = (gzip.compress(TILE_BYTES + name.encode()), f'"{name}"')
    return profile


def _run(profile_id):
    session = SessionLocal()
    run = runs.create_run(session, "download-srtm", profile_id)
    return run, tasks.run_stage(run.id)


def test_tile_names_and_urls():
    assert srtm.tile_name(50, 4) == "N50E004" and srtm.tile_name(-3, -70) == "S03W070"
    assert srtm.tiles([[50, 51, 4, 5]]) == ["N50E004", "N50E005", "N51E004", "N51E005"]
    assert srtm.tile_url("s3://elevation-tiles-prod/skadi/", "N50E004") == \
        "https://elevation-tiles-prod.s3.amazonaws.com/skadi/N50/N50E004.hgt.gz"


def test_fetches_unpacks_and_counts_missing_tiles(cfg):
    run, result = _run(cfg.id)
    session = SessionLocal()
    session.refresh(run)
    assert result["status"] == "awaiting_review"
    summary = run.report_json["summary"]
    assert (summary["tiles"], summary["downloaded"], summary["unchanged"], summary["missing"]) == (4, 3, 0, 1)
    assert run.report_json["missing_tiles"] == ["N51E001"]
    unpacked = config.DATA_ROOT + f"/library/srtm/{run.id}/N50/N50E000.hgt"
    assert open(unpacked, "rb").read() == TILE_BYTES + b"N50E000"
    assert any("open sea" in w for w in run.report_json["warnings"])
    assert any("Unexpected tile size" in w for w in run.report_json["warnings"])  # test tiles are tiny
    assert downloads.resolve_version(session, "srtm:N50E000") is None  # approved only with the run
    runs.approve(session, run)
    assert downloads.resolve_version(session, "srtm:N50E000").status == "approved"


def test_unchanged_tiles_are_not_fetched_again(cfg):
    first, _ = _run(cfg.id)
    runs.approve(SessionLocal(), first)
    second, _ = _run(cfg.id)
    session = SessionLocal()
    session.refresh(second)
    assert second.report_json["summary"]["unchanged"] == 3 and second.report_json["summary"]["downloaded"] == 0
    assert session.query(DownloadRecord).filter(DownloadRecord.source_key == "srtm:N50E000").count() == 1


def test_status_follows_the_tile_set(cfg):
    session = SessionLocal()
    resolved = {"regions": [{"core": [], "overlap": [], "srtm": {"aa": [50, 51, 0, 1]}}], "border_regions": []}
    assert stage_status.compute(session, cfg.id, resolved, {})["download-srtm"].state == "todo"
    run, _ = _run(cfg.id)
    runs.approve(session, run)
    assert stage_status.compute(session, cfg.id, resolved, {})["download-srtm"].state == "ok"
    resolved["regions"][0]["srtm"] = {"aa": [50, 52, 0, 1]}
    assert stage_status.compute(session, cfg.id, resolved, {})["download-srtm"].state == "outdated"
