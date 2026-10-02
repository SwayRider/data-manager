import json

import pytest

from datamanager.db import SessionLocal
from datamanager.jobs import tasks
from datamanager.models import Country, DownloadRecord
from datamanager.services import boundary_sources as boundary_service
from datamanager.services import config_profiles as profiles
from datamanager.services import pelias_sources, runs
from datamanager.services import regions as region_service
from datamanager.services import settings as settings_service
from datamanager.country_sources import SourceState
from datamanager.stages import download_pelias
from datamanager.stages import status as stage_status
from tests.services.test_downloads import Upstream  # noqa: F401

INVENTORY = [
    {"name": "whosonfirst-data-admin-aa-latest.db", "name_compressed": "whosonfirst-data-admin-aa-latest.db.bz2", "last_modified": "2026-01-01"},
    {"name": "whosonfirst-data-admin-aa-latest.db", "name_compressed": "whosonfirst-data-admin-aa-latest.db.tar.bz2", "last_modified": "2026-02-01"},
    {"name": "whosonfirst-data-admin-bb-latest.db", "name_compressed": "whosonfirst-data-admin-bb-latest.db.bz2", "last_modified": "2026-01-01"},
    {"name": "whosonfirst-data-postalcode-aa-latest.db", "name_compressed": "whosonfirst-data-postalcode-aa-latest.db.bz2", "last_modified": "2026-01-01"},
    {"name": "whosonfirst-data-venue-aa-latest.db", "name_compressed": "whosonfirst-data-venue-aa-latest.db.bz2", "last_modified": "2026-01-01"},
]
JOB_URL = "/api/job/77/output/source.geojson.gz"
LOOKUP = "/api/data?source=aa%2Fcountrywide&layer=addresses&validated=false"


@pytest.fixture()
def upstream():
    server = Upstream()
    yield server
    server.close()


@pytest.fixture(autouse=True)
def no_wait(monkeypatch):
    monkeypatch.setattr(download_pelias, "OA_INTERVAL_S", 0)


@pytest.fixture()
def cfg(app, upstream):
    session = SessionLocal()
    settings_service.save(session, {
        "download.wof": upstream.base + "/wof", "download.geonames": upstream.base + "/geonames",
        "download.openaddresses": upstream.base, "download.placeholder": upstream.base + "/placeholder/store.sqlite3.gz",
        "pelias.openaddresses_token": "tok"})
    for iso in ("aa", "bb"):
        session.add(Country(iso2=iso, name=iso.upper(), ne_geometry_ref="x", bbox_json=[0, 0, 1, 1], geofabrik_path=f"europe/{iso}",
                            wof_code=iso, srtm_bbox_json=[50, 51, 0, 1]))
    session.commit()
    boundary_service.set_state(session.get(Country, "aa"), "official_polygons",
                               SourceState(enabled=True, config={"url": upstream.base + "/official/aa.geojson", "format": "geojson", "name_field": "n"}))
    from datamanager.services import address_sources as address_service

    address_service.set_state(session.get(Country, "aa"), "openaddresses", SourceState(enabled=True, config={"files": ["aa/countrywide"]}))
    session.commit()
    profile = profiles.create_profile(session, "dev")
    region = region_service.create_region(session, profile.id, "r1")
    region_service.assign_country(session, region, "aa")
    region_service.assign_country(session, region, "bb")
    files = {
        "/wof/sqlite/inventory.json": (json.dumps(INVENTORY).encode(), '"inv"'),
        "/wof/sqlite/whosonfirst-data-admin-aa-latest.db.bz2": (b"wof-aa", '"a"'),
        "/wof/sqlite/whosonfirst-data-admin-bb-latest.db.bz2": (b"wof-bb", '"b"'),
        "/wof/sqlite/whosonfirst-data-postalcode-aa-latest.db.bz2": (b"wof-pc-aa", '"p"'),
        "/geonames/dump/AA.zip": (b"geonames-aa", '"g"'),
        "/geonames/dump/BB.zip": (b"geonames-bb", '"h"'),
        "/placeholder/store.sqlite3.gz": (b"placeholder", '"s"'),
        "/official/aa.geojson": (b"{}", '"o"'),
        LOOKUP: (json.dumps([{"job": 77}]).encode(), '"l"'),
        JOB_URL: (b"oa-data", '"j"'),
    }
    upstream.files.update(files)
    return profile


def _run(profile_id):
    run = runs.create_run(SessionLocal(), "download-pelias-data", profile_id)
    result = tasks.run_stage(run.id)
    session = SessionLocal()
    session.refresh(run)
    return run, result


def test_plan_lists_every_source(cfg):
    resolved = _resolved(cfg.id)
    keys = [i.key for i in download_pelias.planned(SessionLocal(), resolved)]
    assert keys == ["placeholder:store", "geonames:aa", "wof:admin-aa", "wof:postalcode-aa", "official:aa",
                    "geonames:bb", "wof:admin-bb", "wof:postalcode-bb", "openaddresses:aa/countrywide"]


def _resolved(profile_id):
    from datamanager.services import resolve

    return resolve.to_dict(resolve.resolve_config(SessionLocal(), profile_id))


def test_wof_inventory_prefers_plain_bz2_and_filters(cfg, upstream):
    found = pelias_sources.wof_bundles(upstream.base + "/wof", {"wof:admin-aa", "wof:admin-bb", "wof:postalcode-bb"})
    assert found == {"wof:admin-aa": upstream.base + "/wof/sqlite/whosonfirst-data-admin-aa-latest.db.bz2",
                     "wof:admin-bb": upstream.base + "/wof/sqlite/whosonfirst-data-admin-bb-latest.db.bz2"}


def test_fetches_everything_and_reports(cfg):
    run, result = _run(cfg.id)
    report = run.report_json
    assert result["status"] == "awaiting_review", report
    rows = {r["key"]: r for r in report["sources"]}
    assert rows["geonames:aa"]["status"] == "downloaded" and rows["openaddresses:aa/countrywide"]["status"] == "downloaded"
    assert rows["wof:postalcode-bb"]["status"] == "missing"  # no such bundle: a warning, not a failure
    assert any("WOF postal codes BB" in w for w in report["warnings"])
    assert report["summary"]["downloaded"] == 8 and report["summary"]["missing"] == 1
    assert SessionLocal().query(DownloadRecord).filter_by(source_key="openaddresses:aa/countrywide").one().version_label


def test_rerun_is_unchanged_and_approval_makes_status_ok(cfg):
    run, _ = _run(cfg.id)
    runs.approve(SessionLocal(), runs.get_run(SessionLocal(), run.id))
    run, _ = _run(cfg.id)
    assert {r["status"] for r in run.report_json["sources"] if r["key"] != "wof:postalcode-bb"} == {"unchanged"}
    assert SessionLocal().query(DownloadRecord).filter_by(source_key="geonames:aa").count() == 1
    assert stage_status.compute(SessionLocal(), cfg.id, _resolved(cfg.id), {})["download-pelias-data"].state in ("ok", "review")


def test_status_outdated_when_sources_change(cfg):
    run, _ = _run(cfg.id)
    runs.approve(SessionLocal(), runs.get_run(SessionLocal(), run.id))
    assert stage_status.compute(SessionLocal(), cfg.id, _resolved(cfg.id), {})["download-pelias-data"].state == "ok"
    settings_service.save(SessionLocal(), {"download.geonames": "http://elsewhere.invalid/geonames"})
    assert stage_status.compute(SessionLocal(), cfg.id, _resolved(cfg.id), {})["download-pelias-data"].state == "outdated"


def test_openaddresses_without_token_is_skipped(cfg):
    settings_service.save(SessionLocal(), {}, clear=frozenset({"pelias.openaddresses_token"}))
    run, result = _run(cfg.id)
    assert result["status"] == "awaiting_review"
    assert any("no token" in w for w in run.report_json["warnings"])
    assert {r["status"] for r in run.report_json["sources"] if r["kind"] == "openaddresses"} == {"skipped"}


def test_unknown_openaddresses_source_is_a_warning(cfg, upstream):
    del upstream.files[LOOKUP]
    run, result = _run(cfg.id)
    assert result["status"] == "awaiting_review"
    assert any("openaddresses" in w.lower() or "OpenAddresses" in w for w in run.report_json["warnings"])


def test_missing_essential_source_fails(cfg, upstream):
    del upstream.files["/geonames/dump/AA.zip"]
    run, result = _run(cfg.id)
    assert result["status"] == "failed" and "GeoNames AA" in run.report_json["error"]
