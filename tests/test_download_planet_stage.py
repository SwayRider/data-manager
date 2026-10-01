import datetime

import pytest

from datamanager.db import SessionLocal
from datamanager.jobs import tasks
from datamanager.services import downloads, runs
from datamanager.services import settings as settings_service
from datamanager.stages import download_planet
from tests.services.test_downloads import Upstream  # noqa: F401

PATH = "/pbf/planet-latest.osm.pbf"


@pytest.fixture()
def upstream():
    server = Upstream()
    yield server
    server.close()


@pytest.fixture()
def planet(app, upstream, monkeypatch):
    session = SessionLocal()
    settings_service.save(session, {"download.planet": upstream.base + PATH})
    stamp = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    monkeypatch.setattr(download_planet, "_header_check", lambda session, path: {"ok": True, "data_timestamp": stamp})
    upstream.files[PATH] = (b"planet-one" * 10, '"p1"')
    return upstream


def _run():
    session = SessionLocal()
    run = runs.create_run(session, "download-planet", None)
    result = tasks.run_stage(run.id)
    return run.id, result


def _report(run_id):
    return runs.get_run(SessionLocal(), run_id).report_json


def test_downloads_planet_without_a_configuration_and_awaits_review(planet):
    run_id, result = _run()
    report = _report(run_id)
    assert result["status"] == "awaiting_review"
    assert report["summary"]["status"] == "downloaded" and report["summary"]["size_bytes"] == 100
    assert report["summary"]["data_timestamp"] and report["summary"]["age_days"] == 0
    assert downloads.resolve_version(SessionLocal(), "planet:osm") is None  # not approved yet
    runs.approve(SessionLocal(), runs.get_run(SessionLocal(), run_id))
    assert downloads.resolve_version(SessionLocal(), "planet:osm").version_label == report["summary"]["version"]


def test_unchanged_upstream_downloads_nothing_new(planet):
    first, _ = _run()
    runs.approve(SessionLocal(), runs.get_run(SessionLocal(), first))
    second, result = _run()
    assert result["status"] == "awaiting_review" and _report(second)["summary"]["status"] == "unchanged"
    assert len(downloads.versions(SessionLocal(), "planet:osm")) == 1
    gets = [r for r in planet.requests if r[2] is None]  # full-file GETs plus HEADs: no second body download
    assert planet.requests.count((PATH, 0, None)) == gets.count((PATH, 0, None))


def test_new_upstream_version_is_kept_next_to_the_previous_and_old_ones_pruned(planet):
    ids = []
    for n in (1, 2, 3):
        planet.files[PATH] = (f"planet-{n}".encode() * 10, f'"p{n}"')
        run_id, _ = _run()
        runs.approve(SessionLocal(), runs.get_run(SessionLocal(), run_id))
        ids.append(run_id)
    kept = downloads.versions(SessionLocal(), "planet:osm")
    assert len(kept) == 2  # download.planet_keep default: current + previous
    assert all(r.status == "approved" for r in kept)


def test_keep_setting_changes_retention(planet):
    settings_service.save(SessionLocal(), {"download.planet_keep": "1"})
    for n in (1, 2):
        planet.files[PATH] = (f"planet-{n}".encode() * 10, f'"p{n}"')
        run_id, _ = _run()
        runs.approve(SessionLocal(), runs.get_run(SessionLocal(), run_id))
    assert len(downloads.versions(SessionLocal(), "planet:osm")) == 1


def test_missing_planet_fails_with_the_url(planet):
    del planet.files[PATH]
    run_id, result = _run()
    run = runs.get_run(SessionLocal(), run_id)
    assert result["status"] == "failed" and PATH in run.error_message


def test_reject_discards_the_new_version(planet):
    run_id, _ = _run()
    runs.reject(SessionLocal(), runs.get_run(SessionLocal(), run_id))
    record = downloads.versions(SessionLocal(), "planet:osm")[0]
    assert record.status == "rejected"


def test_old_planet_data_and_missing_md5_are_warnings(planet, monkeypatch):
    monkeypatch.setattr(download_planet, "_header_check", lambda session, path: {"ok": True, "data_timestamp": "2020-01-01T00:00:00Z"})
    run_id, _ = _run()
    warnings = _report(run_id)["warnings"]
    assert any("days old" in w for w in warnings) and any(".md5" in w for w in warnings)


def test_unreadable_header_fails_the_run(planet, monkeypatch):
    monkeypatch.setattr(download_planet, "_header_check", lambda session, path: {"ok": False, "message": "not a pbf"})
    run_id, result = _run()
    assert result["status"] == "failed" and "not a pbf" in runs.get_run(SessionLocal(), run_id).error_message


def _run_with(params):
    session = SessionLocal()
    run = runs.create_run(session, "download-planet", None, params=params)
    return run.id, tasks.run_stage(run.id)


def test_use_existing_skips_the_upstream_check(planet):
    first, _ = _run()
    runs.approve(SessionLocal(), runs.get_run(SessionLocal(), first))
    planet.requests.clear()
    planet.files.clear()  # upstream gone or changed: must not matter
    run_id, result = _run_with({"use_existing": True})
    assert result["status"] == "awaiting_review" and _report(run_id)["summary"]["status"] == "existing"
    assert planet.requests == []
    assert len(downloads.versions(SessionLocal(), "planet:osm")) == 1


def test_use_existing_without_a_version_fails_clearly(planet):
    run_id, result = _run_with({"use_existing": True})
    assert result["status"] == "failed" and "No planet version" in runs.get_run(SessionLocal(), run_id).error_message


def test_local_file_is_registered_in_place_and_never_deleted(planet, tmp_path):
    mine = tmp_path / "planet-mine.osm.pbf"
    mine.write_bytes(b"my own planet" * 10)
    run_id, result = _run_with({"local_file": str(mine)})
    report = _report(run_id)
    assert result["status"] == "awaiting_review" and report["summary"]["status"] == "imported"
    session = SessionLocal()
    record = downloads.versions(session, "planet:osm")[0]
    assert downloads.abs_path(record) == mine.resolve() and report["summary"]["size_bytes"] == mine.stat().st_size
    runs.approve(session, runs.get_run(session, run_id))
    assert downloads.resolve_version(SessionLocal(), "planet:osm").id == record.id
    downloads.unpin(SessionLocal(), record)
    downloads._remove(SessionLocal(), SessionLocal().get(type(record), record.id))
    assert mine.exists() and not downloads.versions(SessionLocal(), "planet:osm")  # record gone, file untouched


def test_local_file_that_does_not_exist_fails(planet, tmp_path):
    run_id, result = _run_with({"local_file": str(tmp_path / "nope.pbf")})
    assert result["status"] == "failed" and "not a file" in runs.get_run(SessionLocal(), run_id).error_message
