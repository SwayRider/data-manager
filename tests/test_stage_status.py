import datetime
import json

import pytest

from datamanager.config import config
from datamanager.db import SessionLocal
from datamanager.models import Asset, Country, DownloadRecord
from datamanager.services import config_profiles as profiles
from datamanager.services import regions as region_service
from datamanager.services import resolve, runs
from datamanager.stages import status
from datamanager.stages.polygons import polygons_fingerprint


@pytest.fixture()
def cfg(app):
    session = SessionLocal()
    with open(config.DATA_ROOT + "/geo-aa.geojson", "w") as f:
        json.dump({"type": "Polygon", "coordinates": [[[0, 50], [1, 50], [1, 51], [0, 51], [0, 50]]]}, f)
    session.add(Country(iso2="aa", name="Aa", ne_geometry_ref="geo-aa.geojson", bbox_json=[0, 0, 1, 1],
                        geofabrik_path="europe/aa", wof_code="aa", srtm_bbox_json=[50, 51, 0, 1]))
    session.commit()
    profile = profiles.create_profile(session, "dev")
    region_service.assign_country(session, region_service.create_region(session, profile.id, "r1"), "aa")
    return profile.id


def _resolved(config_id):
    return resolve.to_dict(resolve.resolve_config(SessionLocal(), config_id))


def _state(config_id, key, blocked=None):
    return status.compute(SessionLocal(), config_id, _resolved(config_id), blocked or {})[key]


def _planet(label="20261001T000000Z", data="now", approved=True):
    stamp = (datetime.datetime.now(datetime.UTC) if data == "now" else data).strftime("%Y-%m-%dT%H:%M:%SZ") if data != "none" else None
    session = SessionLocal()
    record = DownloadRecord(source_key="planet:osm", version_label=label, url="x", filename="p", local_path="p", size_bytes=1,
                            content_hash=label, fetched_at=datetime.datetime(2026, 10, 1), status="approved" if approved else "fetched",
                            data_timestamp=stamp)
    session.add(record)
    session.commit()
    return record.id


def _asset(config_id, asset_type, name, meta, status_="approved"):
    session = SessionLocal()
    asset = Asset(asset_type=asset_type, name=name, config_profile_id=config_id, path="x", content_hash="h", size_bytes=1,
                  status=status_, meta_json=meta)
    session.add(asset)
    session.commit()
    return asset.id


def test_nothing_done_yet(cfg):
    assert _state(cfg, "download-planet").state == "todo"
    assert _state(cfg, "extract-countries", {"extract-countries": "No approved planet"}).state == "blocked"
    assert _state(cfg, "polygons").state == "todo"
    assert _state(cfg, "osm-extract", {"osm-extract": "no country extract"}).state == "blocked"


def test_planet_fresh_old_and_unapproved(cfg):
    _planet(approved=False)
    assert _state(cfg, "download-planet").state == "todo"  # fetched but not approved yet
    old = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=30)
    SessionLocal().query(DownloadRecord).delete()
    SessionLocal().commit()
    _planet(data=old)
    assert _state(cfg, "download-planet").state == "outdated" and "30 days" in _state(cfg, "download-planet").detail
    SessionLocal().query(DownloadRecord).delete()
    SessionLocal().commit()
    _planet()
    assert _state(cfg, "download-planet").state == "ok"


def test_countries_follow_the_current_planet(cfg):
    first = _planet("20260920T000000Z")
    assert _state(cfg, "extract-countries").state == "todo"
    _asset(None, "country-pbf", "europe/aa", {"planet_record_id": first})
    assert _state(cfg, "extract-countries").state == "ok"
    newer = _planet("20261001T000000Z")  # a newer approved planet arrives
    state = _state(cfg, "extract-countries")
    assert state.state == "outdated" and "older planet" in state.detail
    _asset(None, "country-pbf", "europe/aa", {"planet_record_id": newer})
    assert _state(cfg, "extract-countries").state == "ok"


def test_polygons_become_outdated_when_the_regions_change(cfg):
    _asset(cfg, "core-polygon", "r1-core", {"fingerprint": polygons_fingerprint(_resolved(cfg))})
    assert _state(cfg, "polygons").state == "ok"
    session = SessionLocal()
    session.add(Country(iso2="bb", name="Bb", ne_geometry_ref="geo-aa.geojson", bbox_json=[0, 0, 1, 1],
                        geofabrik_path="europe/bb", wof_code="bb", srtm_bbox_json=[50, 51, 0, 1]))
    session.commit()
    region = region_service.list_regions(session, cfg)[0]
    region_service.assign_country(session, region, "bb")
    assert _state(cfg, "polygons").state == "outdated"
    _asset(cfg, "core-polygon", "r1-core", {})  # an asset that never recorded a fingerprint is outdated too
    assert _state(cfg, "polygons").state == "outdated"


def test_polygons_unaffected_by_changes_they_do_not_depend_on(cfg):
    fingerprint = polygons_fingerprint(_resolved(cfg))
    from datamanager.services import resolve as resolve_service
    resolved = _resolved(cfg)
    resolved["regions"][0]["hash"] = "different"  # e.g. an OpenAddresses file toggled
    assert polygons_fingerprint(resolved) == fingerprint


def test_regions_follow_their_inputs(cfg):
    from datamanager.services import settings as settings_service
    from datamanager.stages.osm_extract import plan_inputs, region_fingerprint

    country = _asset(None, "country-pbf", "europe/aa", {"planet_version": "p1"})
    plans, problems = plan_inputs(SessionLocal(), cfg, _resolved(cfg))
    assert not problems
    assert _state(cfg, "osm-extract").state == "todo"
    _asset(cfg, "osm-pbf", "r1", {"fingerprint": region_fingerprint(plans[0])})
    assert _state(cfg, "osm-extract").state == "ok"
    newer = _asset(None, "country-pbf", "europe/aa", {"planet_version": "p2"})  # country re-extracted and approved
    state = _state(cfg, "osm-extract")
    assert state.state == "outdated" and "r1" in state.detail and newer != country


def test_a_region_that_was_never_built_is_listed(cfg):
    _asset(None, "country-pbf", "europe/aa", {})
    _asset(cfg, "osm-pbf", "other", {"fingerprint": "x"})
    state = _state(cfg, "osm-extract")
    assert state.state == "todo"


def test_latest_run_overrides_the_content_state(cfg):
    session = SessionLocal()
    _planet()
    run = runs.create_run(session, "download-planet", cfg)
    assert _state(cfg, "download-planet").state == "running"
    runs.mark_running(session, run)
    runs.finish(session, run, {"record_ids": []})
    assert _state(cfg, "download-planet").state == "review"
    runs.approve(session, run)
    assert _state(cfg, "download-planet").state == "ok"
    failed = runs.create_run(session, "polygons", cfg)
    runs.fail(session, failed, RuntimeError("boom"))
    state = _state(cfg, "polygons")
    assert state.state == "failed" and "boom" in state.detail
    _asset(cfg, "core-polygon", "r1-core", {"fingerprint": polygons_fingerprint(_resolved(cfg))})
    assert _state(cfg, "polygons").state == "ok"  # an up to date result beats an older failed attempt


def test_regions_built_before_fingerprints_are_compared_by_their_recorded_inputs(cfg):
    from datamanager.stages.osm_extract import plan_inputs

    country = _asset(None, "country-pbf", "europe/aa", {})
    plans, _ = plan_inputs(SessionLocal(), cfg, _resolved(cfg))
    legacy = _asset(cfg, "osm-pbf", "r1", {"country_asset_ids": [country], "overlap_polygon_asset_id": None})
    assert _state(cfg, "osm-extract").state == "ok"  # same inputs: no re-run needed
    SessionLocal().query(Asset).filter_by(id=legacy).update({"meta_json": {"country_asset_ids": [country + 99], "overlap_polygon_asset_id": None}})
    SessionLocal().commit()
    assert _state(cfg, "osm-extract").state == "outdated"
    SessionLocal().query(Asset).filter_by(id=legacy).update({"meta_json": {}})  # nothing recorded at all
    SessionLocal().commit()
    assert _state(cfg, "osm-extract").state == "outdated"
