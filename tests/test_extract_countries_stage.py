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
from datamanager.services import assets, downloads, osmium, runs
from datamanager.services import config_profiles as profiles
from datamanager.services import regions as region_service
from datamanager.services import settings as settings_service
from tests.services.test_downloads import Upstream  # noqa: F401

pytestmark = pytest.mark.skipif(shutil.which("osmium") is None, reason="osmium not installed")

BOXES = {"aa": (0, 50, 1, 51), "bb": (1.15, 50, 2.15, 51)}
# node id -> (lat, lon); 1,2 in aa; 10 in bb; 99 in the sea between nothing; 3 is far north of both
NODES = {1: (50.5, 0.2), 2: (50.5, 0.8), 10: (50.5, 1.5), 3: (55.0, 0.5), 11: (50.9, 2.0)}


def _poly(box, name="x"):
    x0, y0, x1, y1 = box
    ring = [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
    return name + "\n1\n" + "".join(f"   {x:.6f}   {y:.6f}\n" for x, y in ring) + "END\nEND\n"


def _planet(label: str, nodes=NODES) -> DownloadRecord:
    directory = Path(config.DATA_ROOT) / "downloads" / "planet" / "osm" / label
    directory.mkdir(parents=True)
    xml = ["<?xml version='1.0' encoding='UTF-8'?><osm version='0.6'>"]
    for nid, (lat, lon) in sorted(nodes.items()):  # a real planet is sorted by id
        xml.append(f"<node id='{nid}' version='1' timestamp='2024-01-01T00:00:00Z' lat='{lat}' lon='{lon}'/>")
    xml.append("</osm>")
    (directory / "p.osm").write_text("".join(xml))
    subprocess.run(["osmium", "cat", str(directory / "p.osm"), "-o", str(directory / "planet.osm.pbf")], check=True)
    session = SessionLocal()
    record = DownloadRecord(
        source_key="planet:osm", version_label=label, url="http://x", filename="planet.osm.pbf",
        local_path=str((directory / "planet.osm.pbf").relative_to(config.DATA_ROOT)),
        size_bytes=(directory / "planet.osm.pbf").stat().st_size, content_hash="h" + label,
        fetched_at=datetime.datetime.strptime(label, "%Y%m%dT%H%M%SZ"), status="approved", data_timestamp="2026-09-20T00:00:00Z")
    session.add(record)
    session.commit()
    return record.id


@pytest.fixture()
def upstream():
    server = Upstream()
    yield server
    server.close()


@pytest.fixture()
def cfg(app, upstream):
    session = SessionLocal()
    for iso, box in BOXES.items():
        with open(config.DATA_ROOT + f"/geo-{iso}.geojson", "w") as f:
            x0, y0, x1, y1 = box
            json.dump({"type": "Polygon", "coordinates": [[[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]]}, f)
        session.add(Country(iso2=iso, name=iso.upper(), ne_geometry_ref=f"geo-{iso}.geojson", bbox_json=[0, 0, 1, 1],
                            geofabrik_path=f"europe/{iso}", wof_code=iso, srtm_bbox_json=[50, 51, 0, 1]))
        upstream.files[f"/europe/{iso}.poly"] = (_poly(box, iso).encode(), f'"{iso}-1"')
    session.commit()
    profile = profiles.create_profile(session, "dev")
    for name, iso in (("Region One", "aa"), ("r2", "bb")):
        region_service.assign_country(session, region_service.create_region(session, profile.id, name), iso)
    settings_service.save(session, {"download.country_polys": upstream.base + "/"})
    return profile.id


def _run(profile_id, params=None, approve=True):
    session = SessionLocal()
    run = runs.create_run(session, "extract-countries", profile_id, params=params)
    result = tasks.run_stage(run.id)
    if approve and result["status"] == "awaiting_review":
        runs.approve(SessionLocal(), runs.get_run(SessionLocal(), run.id))
    return run.id, result


def _report(run_id):
    return runs.get_run(SessionLocal(), run_id).report_json


def _ids(asset):
    out = subprocess.run(["osmium", "cat", str(assets.abs_path(asset)), "-f", "opl"], capture_output=True, text=True, check=True).stdout
    return sorted(int(line.split()[0][1:]) for line in out.splitlines() if line.startswith("n"))


def _current(path):
    return assets.current(SessionLocal(), None, "country-pbf", path)


def test_needs_an_approved_planet(cfg):
    run_id, result = _run(cfg)
    assert result["status"] == "failed" and "approved planet" in runs.get_run(SessionLocal(), run_id).error_message


def test_cuts_each_country_from_the_planet_in_one_pass(cfg, monkeypatch):
    _planet("20260920T000000Z")
    calls = []
    real = osmium.extract_many
    monkeypatch.setattr(osmium, "extract_many", lambda *a, **k: calls.append(1) or real(*a, **k))
    run_id, result = _run(cfg)
    report = _report(run_id)
    assert result["status"] == "awaiting_review", _report(run_id)
    assert calls == [1]
    assert report["summary"]["extracted"] == 2 and report["summary"]["unchanged"] == 0 and report["summary"]["failed"] == 0
    aa, bb = _current("europe/aa"), _current("europe/bb")
    assert _ids(aa) == [1, 2] and _ids(bb) == [10, 11]  # far node 3 is in neither country
    assert aa.config_profile_id is None and aa.meta_json["planet_version"] == "20260920T000000Z"
    assert aa.meta_json["poly_hash"] and len(aa.source_download_ids) == 2
    assert not (Path(config.DATA_ROOT) / "work" / str(run_id)).exists()


def test_second_run_extracts_nothing_when_the_planet_and_polygons_are_unchanged(cfg, monkeypatch):
    _planet("20260920T000000Z")
    _run(cfg)
    monkeypatch.setattr(osmium, "extract_many", lambda *a, **k: pytest.fail("must not extract again"))
    run_id, result = _run(cfg)
    summary = _report(run_id)["summary"]
    assert result["status"] == "awaiting_review" and summary["extracted"] == 0 and summary["unchanged"] == 2
    assert SessionLocal().query(Asset).filter_by(asset_type="country-pbf").count() == 2


def test_new_planet_version_re_extracts_and_keeps_the_previous_one(cfg):
    _planet("20260920T000000Z")
    _run(cfg)
    _planet("20260927T000000Z", {**NODES, 12: (50.6, 0.4)})
    run_id, _ = _run(cfg)
    assert _report(run_id)["summary"]["extracted"] == 2
    assert _ids(_current("europe/aa")) == [1, 2, 12]
    assert SessionLocal().query(Asset).filter_by(asset_type="country-pbf", name="europe/aa", status="approved").count() == 2


def test_only_two_versions_of_a_country_are_kept(cfg):
    for n, label in enumerate(("20260920T000000Z", "20260927T000000Z", "20261004T000000Z")):
        _planet(label, {**NODES, 20 + n: (50.6, 0.4)})
        # the newest approved planet is picked by default
        _run(cfg)
    names = SessionLocal().query(Asset).filter_by(asset_type="country-pbf", name="europe/aa", status="approved").count()
    assert names == 2


def test_changed_polygon_re_extracts_only_that_country(cfg, upstream):
    _planet("20260920T000000Z")
    _run(cfg)
    upstream.files["/europe/bb.poly"] = (_poly((1.15, 50, 1.6, 51), "bb").encode(), '"bb-2"')
    run_id, _ = _run(cfg)
    report = _report(run_id)
    by_path = {c["path"]: c for c in report["countries"]}
    assert by_path["europe/aa"]["status"] == "unchanged" and by_path["europe/bb"]["status"] == "extracted"
    assert _ids(_current("europe/bb")) == [10]  # node 11 (lon 2.0) is outside the smaller polygon


def test_paths_param_limits_the_run(cfg):
    _planet("20260920T000000Z")
    run_id, _ = _run(cfg, {"paths": ["europe/bb"]})
    assert [c["path"] for c in _report(run_id)["countries"]] == ["europe/bb"]
    assert _current("europe/aa") is None and _current("europe/bb") is not None


def test_unknown_path_fails(cfg):
    _planet("20260920T000000Z")
    _, result = _run(cfg, {"paths": ["europe/zz"]})
    assert result["status"] == "failed"


def test_reject_removes_files_and_keeps_nothing_current(cfg):
    _planet("20260920T000000Z")
    run_id, _ = _run(cfg, approve=False)
    stored = assets.for_run(SessionLocal(), run_id)
    files = [assets.abs_path(a) for a in stored]
    assert len(files) == 2 and all(f.exists() for f in files)
    runs.reject(SessionLocal(), runs.get_run(SessionLocal(), run_id))
    assert not any(f.exists() for f in files) and _current("europe/aa") is None


def test_reusing_a_produced_extract_in_a_later_run_approves_it(cfg):
    _planet("20260920T000000Z")
    _run(cfg, approve=False)  # produced, awaiting review, never approved
    run_id, _ = _run(cfg)  # second run finds both unchanged and approving it approves them
    assert _report(run_id)["summary"]["unchanged"] == 2
    assert _current("europe/aa") is not None and _current("europe/bb") is not None


def test_missing_polygon_fails_the_run_without_leaving_files(cfg, upstream):
    _planet("20260920T000000Z")
    del upstream.files["/europe/bb.poly"]
    run_id, result = _run(cfg)
    assert result["status"] == "failed" and "europe/bb" in runs.get_run(SessionLocal(), run_id).error_message
    assert SessionLocal().query(Asset).count() == 0


def test_another_configuration_reuses_the_same_extracts(cfg, monkeypatch):
    _planet("20260920T000000Z")
    _run(cfg)
    session = SessionLocal()
    other = profiles.create_profile(session, "other")
    region_service.assign_country(session, region_service.create_region(session, other.id, "solo"), "aa")
    monkeypatch.setattr(osmium, "extract_many", lambda *a, **k: pytest.fail("must not extract again"))
    run_id, result = _run(other.id)
    assert result["status"] == "awaiting_review" and _report(run_id)["summary"]["unchanged"] == 2  # aa core + bb overlap


def test_countries_are_cut_in_batches_that_fit_the_memory_budget(cfg, monkeypatch):
    _planet("20260920T000000Z")
    settings_service.save(SessionLocal(), {"run.extract_memory_gb": "4"})  # 4 // 3 = 1 country per batch
    batches = []
    real = osmium.extract_many

    def spy(exe, source, jobs, work, progress_cb=None, min_free_gb=0):
        batches.append(len(jobs))
        return real(exe, source, jobs, work, progress_cb or (lambda *a: None), min_free_gb)

    monkeypatch.setattr(osmium, "extract_many", spy)
    run_id, result = _run(cfg)
    summary = _report(run_id)["summary"]
    assert result["status"] == "awaiting_review" and batches == [1, 1]
    assert summary["batches"] == 2 and summary["batch_size"] == 1 and summary["extracted"] == 2
    assert _ids(_current("europe/aa")) == [1, 2] and _ids(_current("europe/bb")) == [10, 11]


def test_big_budget_cuts_everything_in_one_run(cfg, monkeypatch):
    _planet("20260920T000000Z")
    batches = []
    real = osmium.extract_many
    monkeypatch.setattr(osmium, "extract_many", lambda e, s, j, w, cb=None, min_free_gb=0: batches.append(len(j)) or real(e, s, j, w, cb or (lambda *a: None), min_free_gb))
    _run(cfg)
    assert batches == [2]


def test_low_memory_stops_osmium_and_fails_the_run_keeping_finished_batches(cfg, monkeypatch):
    _planet("20260920T000000Z")
    settings_service.save(SessionLocal(), {"run.extract_memory_gb": "4"})
    calls = []
    real = osmium.extract_many

    def flaky(exe, source, jobs, work, progress_cb=None, min_free_gb=0):
        calls.append(1)
        if len(calls) == 2:
            raise osmium.OsmiumMemoryError("osmium was stopped: only 1.0 GB memory was available")
        return real(exe, source, jobs, work, progress_cb or (lambda *a: None), min_free_gb)

    monkeypatch.setattr(osmium, "extract_many", flaky)
    run_id, result = _run(cfg)
    assert result["status"] == "failed" and "only 1.0 GB" in runs.get_run(SessionLocal(), run_id).error_message
    report = _report(run_id)
    by_path = {c["path"]: c for c in report["countries"]}
    assert by_path["europe/aa"]["status"] == "extracted" and by_path["europe/bb"]["status"] == "failed"
    # the finished batch is kept, so the re-run only cuts what is missing
    monkeypatch.setattr(osmium, "extract_many", lambda e, s, j, w, cb=None, min_free_gb=0: (calls.append(len(j)), real(e, s, j, w, cb or (lambda *a: None), min_free_gb))[1])
    calls.clear()
    run_id, result = _run(cfg)
    by_path = {c["path"]: c for c in _report(run_id)["countries"]}
    assert result["status"] == "awaiting_review" and calls == [1]
    assert by_path["europe/aa"]["status"] == "unchanged" and by_path["europe/bb"]["status"] == "extracted"
    assert _current("europe/aa") is not None and _current("europe/bb") is not None
