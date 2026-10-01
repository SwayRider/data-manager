import json

import pytest

from datamanager.config import config
from datamanager.db import SessionLocal
from datamanager.errors import ValidationError
from datamanager.jobs import tasks
from datamanager.models import Country
from datamanager.services import config_profiles as profiles
from datamanager.services import downloads, runs
from datamanager.services import regions as region_service
from datamanager.services import settings as settings_service
from datamanager.stages import download_osm
from tests.services.test_downloads import Upstream  # noqa: F401  (fixture below builds on it)


@pytest.fixture()
def upstream():
    server = Upstream()
    yield server
    server.close()


@pytest.fixture()
def cfg(app, upstream, monkeypatch):
    session = SessionLocal()
    with open(config.DATA_ROOT + "/geo-aa.geojson", "w") as f:
        json.dump({"type": "Polygon", "coordinates": [[[0, 50], [1, 50], [1, 51], [0, 51], [0, 50]]]}, f)
    session.add(Country(iso2="aa", name="Aa", ne_geometry_ref="geo-aa.geojson", bbox_json=[0, 0, 1, 1],
                        geofabrik_path="europe/aa", wof_code="aa", srtm_bbox_json=[50, 51, 0, 1]))
    session.commit()
    profile = profiles.create_profile(session, "dev")
    region = region_service.create_region(session, profile.id, "r1")
    region_service.assign_country(session, region, "aa")
    settings_service.save(session, {"download.osm": upstream.base + "/"})
    upstream.files["/europe/aa-latest.osm.pbf"] = (b"pbf-one", '"e1"')
    monkeypatch.setattr(download_osm, "_header_check", lambda session, path: {"ok": True, "data_timestamp": None})
    return profile


def _run(profile_id):
    session = SessionLocal()
    run = runs.create_run(session, "download-osm", profile_id)
    return run, tasks.run_stage(run.id)


def test_run_downloads_then_awaits_review(cfg):
    run, result = _run(cfg.id)
    session = SessionLocal()
    session.refresh(run)
    assert result["status"] == "awaiting_review" and run.status == "awaiting_review"
    assert run.report_json["summary"] == {"sources": 1, "downloaded": 1, "unchanged": 0, "failed": 0, "bytes": 7}
    assert [s.name for s in run.steps] == ["europe/aa (1/1)"] and run.steps[0].status == "success"
    assert run.steps[0].progress_current == 7 and run.started_at and run.finished_at
    # not usable until approved
    assert downloads.resolve_version(session, "osm:europe/aa") is None


def test_approve_makes_version_selectable_reject_does_not(cfg):
    session = SessionLocal()
    run, _ = _run(cfg.id)
    runs.approve(session, run, "looks right")
    assert run.status == "approved" and run.review_note == "looks right"
    assert downloads.resolve_version(session, "osm:europe/aa").version_label

    with pytest.raises(ValidationError, match="awaiting review"):
        runs.approve(session, run)


def test_second_run_unchanged_is_claimed_by_review(cfg):
    session = SessionLocal()
    first, _ = _run(cfg.id)
    second, _ = _run(cfg.id)  # upstream unchanged, first version still unreviewed
    session.refresh(second)
    assert second.report_json["summary"]["unchanged"] == 1
    runs.approve(session, second)
    assert downloads.resolve_version(session, "osm:europe/aa") is not None
    assert len(downloads.versions(session, "osm:europe/aa")) == 1


def test_other_config_reuses_existing_version_and_shows_where_from(cfg):
    session = SessionLocal()
    first, _ = _run(cfg.id)
    runs.approve(session, first)
    other = profiles.create_profile(session, "other")
    region = region_service.create_region(session, other.id, "r2")
    region_service.assign_country(session, region, "aa")
    second, _ = _run(other.id)
    session.refresh(second)
    entry = second.report_json["files"][0]
    assert entry["status"] == "unchanged" and entry["fetched_by_run"] == first.id and entry["record_status"] == "approved"
    assert second.report_json["summary"]["bytes"] == 0
    assert len(downloads.versions(session, "osm:europe/aa")) == 1


def test_reject_rejects_what_the_run_fetched(cfg):
    session = SessionLocal()
    run, _ = _run(cfg.id)
    runs.reject(session, run, "wrong file")
    assert run.status == "rejected"
    assert [r.status for r in downloads.versions(session, "osm:europe/aa")] == ["rejected"]
    assert downloads.resolve_version(session, "osm:europe/aa") is None


def test_missing_upstream_file_fails_the_run_and_keeps_report(cfg, upstream):
    del upstream.files["/europe/aa-latest.osm.pbf"]
    session = SessionLocal()
    run = runs.create_run(session, "download-osm", cfg.id)
    tasks.run_stage(run.id)
    session.refresh(run)
    assert run.status == "failed" and run.error_type == "StageFailedError" and "europe/aa" in run.error_message
    assert run.report_json["files"][0]["status"] == "failed"


def test_latest_redirect_loop_falls_back_to_newest_dated_file(cfg, upstream):
    upstream.loops.add("/europe/aa-latest.osm.pbf")
    upstream.listings["/europe/"] = (
        '<a href="aa-260101.osm.pbf">x</a> <a href="aa-260929.osm.pbf">y</a> <a href="aa-260929.osm.pbf.md5">z</a>'
        ' <a href="aab-270101.osm.pbf">other</a>'
    )
    upstream.files["/europe/aa-260929.osm.pbf"] = (b"dated", '"d1"')
    run, result = _run(cfg.id)
    session = SessionLocal()
    session.refresh(run)
    assert result["status"] == "awaiting_review"
    entry = run.report_json["files"][0]
    assert entry["status"] == "downloaded" and "redirect-looped" in entry["note"]
    record = downloads.versions(session, "osm:europe/aa")[0]
    assert record.url.endswith("/europe/aa-260929.osm.pbf") and record.size_bytes == 5


def test_redirect_loop_without_dated_file_fails_with_message(cfg, upstream):
    upstream.loops.add("/europe/aa-latest.osm.pbf")
    upstream.listings["/europe/"] = "<html>nothing here</html>"
    run = runs.create_run(SessionLocal(), "download-osm", cfg.id)
    tasks.run_stage(run.id)
    session = SessionLocal()
    session.refresh(run)
    assert run.status == "failed" and "No dated extract" in run.report_json["files"][0]["message"]


def test_config_without_countries_fails_cleanly(app):
    session = SessionLocal()
    profile = profiles.create_profile(session, "empty")
    run = runs.create_run(session, "download-osm", profile.id)
    with pytest.raises(ValidationError):
        tasks.run_stage(run.id)
    session.refresh(run)
    assert run.status == "failed" and run.error_type == "ValidationError"


def test_planned_paths_unique_and_sorted():
    resolved = {"regions": [
        {"core": [{"geofabrik_path": "europe/b"}], "overlap": [{"geofabrik_path": "europe/a"}, {"geofabrik_path": None}]},
        {"core": [{"geofabrik_path": "europe/a"}], "overlap": []},
    ]}
    assert download_osm.planned_paths(resolved) == ["europe/a", "europe/b"]


def test_dated_file_is_found_on_the_region_page(cfg, upstream):
    from datamanager.services import geofabrik

    upstream.listings["/europe/aa.html"] = '<a href="aa-260929.osm.pbf">a</a><a href="aa-260930.osm.pbf">b</a><a href="aa-latest.osm.pbf">l</a>'
    assert geofabrik.newest_dated_pbf_url(upstream.base, "europe/aa").endswith("/europe/aa-260930.osm.pbf")
