import json
import shutil
import subprocess
from pathlib import Path

import pytest
from shapely.geometry import box

from datamanager.config import config
from datamanager.db import SessionLocal
from datamanager.jobs import tasks
from datamanager.models import Country
from datamanager.services import assets, borders, osmium, runs
from datamanager.services import config_profiles as profiles
from datamanager.services import regions as region_service
from datamanager.stages import status as stage_status
from datamanager.stages.border import plan_inputs

pytestmark = pytest.mark.skipif(shutil.which("osmium") is None, reason="osmium not installed")

# aa (Region One) and bb (r2) are ~11 km apart. The pair is built from "r2" (slug order), so crossings are measured
# against bb's outline: a road leaving bb to the west enters "Region One"'s side.
BOXES = {"aa": (0, 50, 1, 51), "bb": (1.15, 50, 2.15, 51)}
BORDER_POLY = "border\n1\n   1.0 50.0\n   1.3 50.0\n   1.3 51.0\n   1.0 51.0\n   1.0 50.0\nEND\nEND\n"


def _osm(outline_box, roads) -> str:
    """OSM XML (nodes, then ways, then the relation: the order osmium requires) with one admin_level=2 multipolygon
    square and `roads` = [(id, lat, lon0, lon1, tags)]."""
    x0, y0, x1, y1 = outline_box
    stamp = "version='1' timestamp='2024-01-01T00:00:00Z'"
    nodes = [(1, y0, x0), (2, y0, x1), (3, y1, x1), (4, y1, x0)]
    ways = ["<way id='1' %s><nd ref='1'/><nd ref='2'/><nd ref='3'/><nd ref='4'/><nd ref='1'/></way>" % stamp]
    nid = 100
    for way_id, lat, lon0, lon1, tags in roads:
        nodes += [(nid, lat, lon0), (nid + 1, lat, lon1)]
        ways.append(f"<way id='{way_id}' {stamp}><nd ref='{nid}'/><nd ref='{nid + 1}'/>"
                    + "".join(f"<tag k='{k}' v='{v}'/>" for k, v in tags.items()) + "</way>")
        nid += 2
    relation = (f"<relation id='1' {stamp}><member type='way' ref='1' role='outer'/><tag k='type' v='multipolygon'/>"
                "<tag k='boundary' v='administrative'/><tag k='admin_level' v='2'/></relation>")
    return ("<?xml version='1.0' encoding='UTF-8'?><osm version='0.6'>"
            + "".join(f"<node id='{n}' {stamp} lat='{lat}' lon='{lon}'/>" for n, lat, lon in sorted(nodes))
            + "".join(ways) + relation + "</osm>")


ROADS = [
    (10, 50.2, 1.0, 1.3, {"highway": "primary"}),               # two-way, crosses bb's west edge
    (11, 50.4, 1.0, 1.3, {"highway": "motorway", "oneway": "yes"}),   # one-way, eastbound
    (12, 50.6, 1.0, 1.3, {"highway": "residential"}),            # ignored type
    (13, 50.8, 1.2, 1.4, {"highway": "primary"}),                # fully inside bb: no crossing
]


def _make_asset(profile_id, run, asset_type, name, filename, content=None, xml=None):
    directory = assets.asset_dir("test", run.id)
    target = directory / filename
    if xml is not None:
        subprocess.run(["osmium", "cat", "-F", "osm", "-", "-o", str(target), "--overwrite"], input=xml, text=True, check=True)
    else:
        target.write_text(content)
    session = SessionLocal()
    asset = assets.create(session, run.id, profile_id, asset_type, name, target)
    asset.status = "approved"
    session.commit()
    return asset


@pytest.fixture()
def cfg(app):
    session = SessionLocal()
    for iso, (x0, y0, x1, y1) in BOXES.items():
        path = Path(config.DATA_ROOT) / f"geo-{iso}.geojson"
        path.write_text(json.dumps({"type": "Polygon", "coordinates": [[[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]]}))
        session.add(Country(iso2=iso, name=iso.upper(), ne_geometry_ref=path.name, bbox_json=[0, 0, 1, 1],
                            geofabrik_path=f"europe/{iso}", wof_code=iso, srtm_bbox_json=[50, 51, 0, 1]))
    session.commit()
    profile = profiles.create_profile(session, "dev")
    for name, iso in (("Region One", "aa"), ("r2", "bb")):
        region_service.assign_country(session, region_service.create_region(session, profile.id, name), iso)
    return profile.id


@pytest.fixture()
def inputs(cfg):
    run = runs.create_run(SessionLocal(), "osm-extract", cfg)
    for slug, box_ in (("region-one", BOXES["aa"]), ("r2", BOXES["bb"])):
        xml = _osm(box_, ROADS)
        _make_asset(cfg, run, "osm-core-pbf", f"{slug}-core", f"{slug}-core.osm.pbf", xml=xml)
        _make_asset(cfg, run, "osm-pbf", slug, f"{slug}.osm.pbf", xml=xml)
    _make_asset(cfg, run, "border-polygon", "r2-region-one-border", "r2-region-one-border.poly", content=BORDER_POLY)
    return cfg


def _run(profile_id, params=None, approve=True):
    session = SessionLocal()
    run = runs.create_run(session, "border", profile_id, params=params)
    result = tasks.run_stage(run.id)
    if approve and result["status"] == "awaiting_review":
        runs.approve(SessionLocal(), runs.get_run(SessionLocal(), run.id))
    return run.id, result


def _report(run_id):
    return runs.get_run(SessionLocal(), run_id).report_json


def _csv_rows(profile_id, name):
    asset = assets.current(SessionLocal(), profile_id, "border-crossings", name)
    return [line.split(",") for line in assets.abs_path(asset).read_text().splitlines()]


def test_detect_crossings_directions_and_filters():
    poly = [box(1.15, 50, 2.15, 51)]

    def feature(osm_id, highway, lon0, lon1, **extra):
        return {"geometry": {"type": "LineString", "coordinates": [[lon0, 50.5], [lon1, 50.5]]},
                "properties": {"@id": osm_id, "highway": highway, **extra}}

    found = borders.detect([
        feature(1, "primary", 1.0, 1.3),                       # two-way
        feature(2, "motorway", 1.0, 1.3, oneway="yes"),        # forward only: outside -> inside
        feature(3, "motorway", 1.0, 1.3, oneway="-1"),         # backward only
        feature(4, "residential", 1.0, 1.3),                   # not a listed type
        feature(5, "primary", 1.2, 1.4),                       # inside, no crossing
    ], poly, "r2", "Region One")
    assert [(c.osm_id, c.from_region, c.to_region) for c in found] == [
        (1, "Region One", "r2"), (1, "r2", "Region One"), (2, "Region One", "r2"), (3, "r2", "Region One")]
    assert round(found[0].location.x, 3) == 1.15 and round(found[0].location.y, 3) == 50.5


def test_blocked_without_approved_inputs(cfg):
    plan = plan_inputs(SessionLocal(), cfg, _resolved(cfg))
    assert any("no approved region PBFs" in p for p in plan.problems)
    run_id, result = _run(cfg)
    assert result["status"] == "failed" and "OSM extract" in _report(run_id)["error"]


def _resolved(profile_id):
    from datamanager.services import resolve

    return resolve.to_dict(resolve.resolve_config(SessionLocal(), profile_id))


def test_needs_the_border_polygon(cfg):
    run = runs.create_run(SessionLocal(), "osm-extract", cfg)
    for slug, box_ in (("region-one", BOXES["aa"]), ("r2", BOXES["bb"])):
        _make_asset(cfg, run, "osm-core-pbf", f"{slug}-core", f"{slug}-core.osm.pbf", xml=_osm(box_, ROADS))
        _make_asset(cfg, run, "osm-pbf", slug, f"{slug}.osm.pbf", xml=_osm(box_, ROADS))
    plan = plan_inputs(SessionLocal(), cfg, _resolved(cfg))
    assert plan.problems == ["r2 / Region One: no approved border polygon (run and approve Region polygons)"]


def test_builds_outlines_and_crossings(inputs):
    run_id, result = _run(inputs)
    report = _report(run_id)
    assert result["status"] == "awaiting_review", report["borders"]
    assert (report["summary"]["regions"], report["summary"]["pairs"], report["summary"]["crossings"]) == (2, 1, 3)
    rows = _csv_rows(inputs, "r2-region-one")
    assert rows[0] == borders.CSV_HEADER
    assert len(rows) == 4  # header + 3
    assert {r[1] for r in rows[1:]} == {"primary", "motorway"}
    outline = assets.current(SessionLocal(), inputs, "region-outline", "r2-core")
    parts = borders.load_outline(assets.abs_path(outline))
    assert len(parts) == 1 and parts[0].bounds == pytest.approx((1.15, 50, 2.15, 51))
    assert report["borders"][0]["by_type"] == {"motorway": 1, "primary": 2}
    review = report["map"]
    assert {o["name"] for o in review["outlines"]} >= {"r2-core"} and all(o["preview"]["type"] for o in review["outlines"])
    assert len(review["crossings"]) == 1 and len(review["crossings"][0]["points"]) == 3
    assert {p[2] for p in review["crossings"][0]["points"]} == {"primary", "motorway"}


def test_rerun_skips_unchanged_inputs_and_status_follows(inputs):
    _run(inputs)
    session = SessionLocal()
    resolved = _resolved(inputs)
    assert stage_status.compute(session, inputs, resolved, {})["border"].state == "ok"
    run_id, _ = _run(inputs)
    report = _report(run_id)
    assert report["borders"][0]["result"] == "unchanged"
    assert {f["status"] for o in report["outlines"] for f in o["files"]} == {"unchanged"}
    assert report["map"]["outlines"] and report["map"]["crossings"][0]["points"]  # unchanged results still map
    # a newer approved region PBF makes it outdated
    run = runs.create_run(session, "osm-extract", inputs)
    _make_asset(inputs, run, "osm-pbf", "r2", "r2.osm.pbf", xml=_osm(BOXES["bb"], ROADS[:1]))
    assert stage_status.compute(session, inputs, resolved, {})["border"].state == "outdated"


def test_single_region_run(inputs):
    run_id, _ = _run(inputs, params={"regions": ["Region One"]})
    report = _report(run_id)
    assert [o["name"] for o in report["outlines"]] == ["Region One"] and report["borders"] == []  # pairs start at r2


def test_reject_marks_assets_rejected(inputs):
    run_id, result = _run(inputs, approve=False)
    runs.reject(SessionLocal(), runs.get_run(SessionLocal(), run_id), "no")
    assert assets.current(SessionLocal(), inputs, "border-crossings", "r2-region-one") is None
