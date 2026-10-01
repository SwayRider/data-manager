import json

import pytest
from shapely.geometry import Polygon, box

from datamanager.config import config
from datamanager.db import SessionLocal
from datamanager.errors import ValidationError
from datamanager.jobs import tasks
from datamanager.models import Asset, Country
from datamanager.services import assets, polygons, runs
from datamanager.services import carve as carve_service
from datamanager.services import config_profiles as profiles
from datamanager.services import regions as region_service

# aa and bb are ~11 km apart (border pair); cc is far away.
BOXES = {"aa": (0, 50, 1, 51), "bb": (1.15, 50, 2.15, 51), "cc": (10, 50, 11, 51)}
WEST_HALF = {"type": "Polygon", "coordinates": [[[-1, 49], [0.5, 49], [0.5, 52], [-1, 52], [-1, 49]]]}


def _write(iso):
    x0, y0, x1, y1 = BOXES[iso]
    with open(config.DATA_ROOT + f"/geo-{iso}.geojson", "w") as f:
        json.dump({"type": "Polygon", "coordinates": [[[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]]}, f)
    return f"geo-{iso}.geojson"


@pytest.fixture()
def cfg(app):
    session = SessionLocal()
    for iso in BOXES:
        session.add(Country(iso2=iso, name=iso.upper(), ne_geometry_ref=_write(iso), bbox_json=[0, 0, 1, 1],
                            geofabrik_path=f"europe/{iso}", wof_code=iso, srtm_bbox_json=[50, 51, 0, 1]))
    session.commit()
    profile = profiles.create_profile(session, "dev")
    for name, iso in (("Region One", "aa"), ("r2", "bb")):
        region_service.assign_country(session, region_service.create_region(session, profile.id, name), iso)
    return profile


def test_compute_core_overlap_and_border_specs(cfg):
    specs = {s.name: s for s in polygons.compute(SessionLocal(), cfg.id)}
    assert set(specs) == {"region-one-core", "region-one-overlap", "r2-core", "r2-overlap", "r2-region-one-border"}
    core, zone = specs["region-one-core"].geometry, specs["region-one-overlap"].geometry
    assert core.bounds == (0, 50, 1, 51)
    assert zone.contains(core)
    assert zone.bounds[2] == pytest.approx(1 + 100 / 71.0, abs=0.05)  # 100 km east at ~50.5°N, not the old 64 km
    assert specs["r2-region-one-border"].regions == ["r2", "Region One"]
    border = specs["r2-region-one-border"].geometry
    assert not border.is_empty and 0.9 < border.bounds[0] < 1.1  # the strip around the ~11 km gap


def test_no_core_is_an_error(app):
    profile = profiles.create_profile(SessionLocal(), "empty")
    with pytest.raises(ValidationError, match="core country"):
        polygons.compute(SessionLocal(), profile.id)


def test_carve_polygon_and_carved_core(cfg):
    session = SessionLocal()
    carve_service.set_carve(session, cfg.id, "aa", WEST_HALF)
    specs = {s.name: s for s in polygons.compute(session, cfg.id)}
    assert specs["carve-aa"].kind == "carve" and specs["carve-aa"].countries == ["aa"]
    assert specs["region-one-core"].geometry.bounds == (0, 50, 0.5, 51)  # only the kept part


def test_finalize_reports_facts_and_fixes_invalid():
    geometry, facts = polygons.finalize(box(0, 0, 1, 1))
    assert facts["valid"] and not facts["fixed"] and facts["parts"] == 1 and facts["vertices"] == 5
    assert facts["bbox"] == [0, 0, 1, 1] and 12_000 < facts["area_km2"] < 12_500  # ~1°x1° at the equator
    bowtie = Polygon([(0, 0), (2, 2), (2, 0), (0, 2)])
    fixed, facts = polygons.finalize(bowtie)
    assert facts["fixed"] and facts["valid"] and fixed.is_valid
    _, empty = polygons.finalize(Polygon())
    assert not empty["valid"] and empty["empty"]


def test_to_poly_format_with_hole_and_parts():
    holed = Polygon([(0, 0), (4, 0), (4, 4), (0, 4)], [[(1, 1), (2, 1), (2, 2), (1, 2)]])
    text = polygons.to_poly("demo", holed)
    lines = text.splitlines()
    assert lines[0] == "demo" and lines[1] == "1" and lines[-1] == "END" and "!1" in lines
    assert lines.count("END") == 3  # outer ring, hole, file
    assert "   0.000000   0.000000" in lines
    two = polygons.to_poly("two", box(0, 0, 1, 1).union(box(5, 5, 6, 6)))
    assert "1" in two.splitlines() and "2" in two.splitlines()


def _run(profile_id):
    session = SessionLocal()
    run = runs.create_run(session, "polygons", profile_id)
    return run, tasks.run_stage(run.id)


def test_stage_writes_assets_and_awaits_review(cfg):
    run, result = _run(cfg.id)
    session = SessionLocal()
    session.refresh(run)
    assert result["status"] == "awaiting_review"
    summary = run.report_json["summary"]
    assert summary["polygons"] == 5 and summary["by_kind"] == {"core": 2, "overlap": 2, "border": 1, "carve": 0}
    assert summary["invalid"] == 0
    stored = assets.for_run(session, run.id)
    assert len(stored) == 5 and {a.status for a in stored} == {"produced"}
    overlap_asset = next(a for a in stored if a.name == "region-one-overlap")
    assert overlap_asset.asset_type == "overlap-polygon" and assets.abs_path(overlap_asset).read_text().startswith("region-one-overlap\n")
    geojson = json.loads((assets.abs_path(overlap_asset).parent / "region-one-overlap.geojson").read_text())
    assert geojson["type"] in ("Polygon", "MultiPolygon")
    assert overlap_asset.content_hash == assets.sha256_of(assets.abs_path(overlap_asset))
    assert [s.name for s in run.steps][0] == "Deriving polygons" and len(run.steps) == 6
    entry = next(p for p in run.report_json["polygons"] if p["name"] == "region-one-overlap")
    assert entry["preview"]["type"] in ("Polygon", "MultiPolygon") and entry["area_km2"] > 0


def test_assets_are_not_current_until_approved_and_newest_wins(cfg):
    session = SessionLocal()
    assert assets.current(session, cfg.id, "overlap-polygon", "region-one-overlap") is None
    first, _ = _run(cfg.id)
    assert assets.current(session, cfg.id, "overlap-polygon", "region-one-overlap") is None
    runs.approve(session, first)
    a1 = assets.current(session, cfg.id, "overlap-polygon", "region-one-overlap")
    assert a1.status == "approved" and a1.produced_by_run_id == first.id

    second, _ = _run(cfg.id)
    runs.approve(session, second)
    assert assets.current(session, cfg.id, "overlap-polygon", "region-one-overlap").produced_by_run_id == second.id


def test_reject_marks_assets_rejected(cfg):
    session = SessionLocal()
    run, _ = _run(cfg.id)
    runs.reject(session, run, "wrong")
    assert {a.status for a in session.query(Asset).all()} == {"rejected"}
    assert assets.current(session, cfg.id, "core-polygon", "r2-core") is None


def test_stage_without_core_fails_the_run(app):
    session = SessionLocal()
    profile = profiles.create_profile(session, "empty")
    run = runs.create_run(session, "polygons", profile.id)
    with pytest.raises(ValidationError):
        tasks.run_stage(run.id)
    session.refresh(run)
    assert run.status == "failed" and run.error_type == "ValidationError"


def test_carved_overlap_country_gets_a_carve_polygon(cfg):
    session = SessionLocal()
    other = profiles.create_profile(session, "solo")
    region_service.assign_country(session, region_service.create_region(session, other.id, "solo"), "aa")  # bb is its overlap
    carve_service.set_carve(session, other.id, "bb", {"type": "Polygon", "coordinates": [[[1, 49], [2, 49], [2, 52], [1, 52], [1, 49]]]})
    specs = {s.name: s for s in polygons.compute(session, other.id)}
    assert specs["carve-bb"].regions == ["solo"]
