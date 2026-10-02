import bz2
import datetime
import json
import shutil
import sqlite3
from pathlib import Path

import pytest
from shapely.geometry import Point, box, mapping

from datamanager.config import config
from datamanager.country_sources import SourceState
from datamanager.db import SessionLocal
from datamanager.jobs import tasks
from datamanager.models import Country, DownloadRecord
from datamanager.services import assets, osmium, runs, wof_patch
from datamanager.services import boundary_sources as boundary_service
from datamanager.services import config_profiles as profiles
from datamanager.services import regions as region_service
from datamanager.stages import status as stage_status
from datamanager.stages import wof_patch as stage

SCHEMA = """
CREATE TABLE spr (id INTEGER NOT NULL PRIMARY KEY, parent_id INTEGER, name TEXT, placetype TEXT, country TEXT, repo TEXT,
  latitude REAL, longitude REAL, min_latitude REAL, min_longitude REAL, max_latitude REAL, max_longitude REAL, is_current INTEGER,
  is_deprecated INTEGER, is_ceased INTEGER, is_superseded INTEGER, is_superseding INTEGER, superseded_by TEXT, supersedes TEXT, lastmodified INTEGER);
CREATE TABLE geojson (id INTEGER NOT NULL, body TEXT, source TEXT, alt_label TEXT, is_alt BOOLEAN, lastmodified INTEGER);
CREATE TABLE ancestors (id INTEGER NOT NULL, ancestor_id INTEGER NOT NULL, ancestor_placetype TEXT, lastmodified INTEGER);
CREATE TABLE names (id INTEGER NOT NULL, name TEXT);
CREATE TABLE concordances (id INTEGER NOT NULL, other_id TEXT);
"""


def _add(db, id_, name, placetype, geometry, hierarchy):
    centre = geometry.centroid
    props = {"wof:id": id_, "wof:name": name, "wof:placetype": placetype, "wof:country": "AA", "wof:hierarchy": [hierarchy] if hierarchy else []}
    db.execute("INSERT INTO spr (id,name,placetype,country,latitude,longitude,is_current,is_deprecated,is_superseded) VALUES (?,?,?,?,?,?,1,0,0)",
               (id_, name, placetype, "AA", centre.y, centre.x))
    db.execute("INSERT INTO geojson (id,body,source,alt_label,is_alt) VALUES (?,?,?,?,0)",
               (id_, json.dumps({"id": id_, "type": "Feature", "properties": props, "geometry": mapping(geometry)}), "wof", ""))
    db.execute("INSERT INTO names (id, name) VALUES (?, ?)", (id_, name))


def make_wof(path: Path) -> Path:
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    _add(db, 1, "Aland", "country", box(0, 0, 10, 10), {"country_id": 1})
    _add(db, 2, "North", "region", box(0, 0, 10, 5), {"country_id": 1, "region_id": 2})
    _add(db, 3, "Countyone", "county", box(0, 0, 5, 5), {"country_id": 1, "region_id": 2, "county_id": 3})
    _add(db, 10, "Wrong", "locality", box(1, 1, 2, 2), {"country_id": 1, "region_id": 2, "locality_id": 10})  # polygon, replaced
    _add(db, 11, "Olmen", "locality", Point(1.5, 1.5), {"country_id": 1, "locality_id": 11})  # point of the same place as a new polygon: duplicate
    _add(db, 50, "Oldmuni", "localadmin", box(0.5, 0.5, 3, 3), {"country_id": 1, "region_id": 2, "county_id": 3, "localadmin_id": 50})  # replaced by Muni
    _add(db, 14, "Hamlet", "locality", Point(1.6, 1.6), {"country_id": 1, "county_id": 3, "localadmin_id": 50, "locality_id": 14})  # other name: stays, hierarchy re-homed
    _add(db, 15, "Hood", "neighbourhood", Point(1.5, 1.5), {"country_id": 1, "localadmin_id": 50, "locality_id": 10, "neighbourhood_id": 15})  # both parents replaced
    _add(db, 16, "Lonely", "neighbourhood", Point(9, 9), {"country_id": 1, "localadmin_id": 50, "neighbourhood_id": 16})  # no new polygon covers it: level dropped
    _add(db, 12, "Far", "locality", Point(8, 8), {"country_id": 1, "locality_id": 12})  # point outside every new polygon: stays
    _add(db, 13, "Gone", "locality", box(4, 4, 4.5, 4.5), {"country_id": 1, "locality_id": 13})  # polygon without replacement
    db.commit()
    db.close()
    return path


NEW = [
    wof_patch.Boundary("osm", 100, 8, "Muni", box(0.5, 0.5, 3, 3)),
    wof_patch.Boundary("osm", 200, 9, "Olmen", box(1, 1, 2, 2)),
    wof_patch.Boundary("osm", 300, 9, "Elsewhere", box(20, 20, 21, 21)),  # outside the country
]


def _split():
    return [b for b in NEW if b.level == 9], [b for b in NEW if b.level == 8]


def test_patch_plan_parents_removals_and_unreplaced(tmp_path):
    db = sqlite3.connect(make_wof(tmp_path / "wof.db"))
    locality, localadmin = _split()
    patch = wof_patch.build_patch(db, locality, localadmin)
    by_name = {n.name: n for n in patch.new}
    assert set(by_name) == {"Muni", "Olmen"} and patch.skipped["outside_country"] == 1
    muni, olmen = by_name["Muni"], by_name["Olmen"]
    assert muni.id == 8_000_000_000_000 + 100 and muni.parent.id == 3 and muni.parent_level == "county"
    assert olmen.id == 9_000_000_000_000 + 200 and olmen.parent_level == "localadmin"
    assert olmen.hierarchy == {"country_id": 1, "region_id": 2, "county_id": 3, "localadmin_id": muni.id, "locality_id": olmen.id}
    assert patch.remove == {10: "polygon", 11: "point-inside", 50: "polygon"}  # replaced polygon + duplicate point; Gone (no covering polygon), Far and Hamlet stay
    assert [item["name"] for item in patch.unreplaced] == ["Gone"]
    assert patch.rehome[14]["localadmin_id"] == muni.id  # Hamlet
    assert patch.rehome[15] == {"country_id": 1, "localadmin_id": muni.id, "locality_id": olmen.id, "neighbourhood_id": 15}
    assert patch.rehome[16] == {"country_id": 1, "neighbourhood_id": 16}
    assert patch.stats["locality"]["kept_polygons"] == 1 and patch.stats["locality"]["kept_points"] == 2 and patch.stats["locality"]["removed_points"] == 1


def test_written_patch_is_what_the_pelias_readers_need(tmp_path):
    source = make_wof(tmp_path / "wof.db")
    locality, localadmin = _split()
    out = tmp_path / "out" / "patched.db"
    wof_patch.patch_country(source, locality, localadmin, out)
    db = sqlite3.connect(out)
    rows = db.execute("SELECT id, placetype, name FROM spr WHERE placetype IN ('locality','localadmin') ORDER BY id").fetchall()
    assert rows == [(12, "locality", "Far"), (13, "locality", "Gone"), (14, "locality", "Hamlet"), (8_000_000_000_100, "localadmin", "Muni"), (9_000_000_000_200, "locality", "Olmen")]
    # exactly the importer's / lookup's own query: active, named, with a geojson row
    found = db.execute("SELECT geojson.id, geojson.body FROM geojson JOIN spr ON geojson.id = spr.id WHERE geojson.id != 1 AND geojson.is_alt != 1 "
                       "AND spr.is_deprecated = 0 AND spr.is_superseded = 0 AND NOT TRIM(IFNULL(spr.name, '')) = '' "
                       "AND spr.placetype = 'locality' ORDER BY geojson.id").fetchall()
    body = json.loads(found[-1][1])
    assert body["properties"]["wof:hierarchy"][0]["localadmin_id"] == 8_000_000_000_100 and body["geometry"]["type"] == "Polygon"
    assert db.execute("SELECT COUNT(*) FROM names WHERE id IN (10, 11, 50)").fetchone() == (0,)
    hood = json.loads(db.execute("SELECT body FROM geojson WHERE id = 15").fetchone()[0])["properties"]["wof:hierarchy"][0]
    assert hood["localadmin_id"] == 8_000_000_000_100 and hood["locality_id"] == 9_000_000_000_200
    assert (8_000_000_000_100,) in db.execute("SELECT ancestor_id FROM ancestors WHERE id = 15").fetchall()
    assert db.execute("SELECT ancestor_placetype FROM ancestors WHERE id = ? ORDER BY 1", (9_000_000_000_200,)).fetchall() == \
        [("country",), ("county",), ("localadmin",), ("region",)]
    assert sqlite3.connect(source).execute("SELECT COUNT(*) FROM spr").fetchone() == (11,)  # the download itself is untouched


def test_parent_needs_half_coverage(tmp_path):
    db = sqlite3.connect(make_wof(tmp_path / "wof.db"))
    straddling = wof_patch.Boundary("osm", 1, 9, "Border town", box(4.8, 4.0, 6.2, 4.5))  # mostly in the region, barely in the county
    patch = wof_patch.build_patch(db, [straddling], [])
    assert patch.new[0].parent.id == 2 and patch.new[0].parent_level == "region"


def test_detect_suggests_municipality_and_locality_levels(tmp_path):
    path = tmp_path / "wof.db"
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    _add(db, 1, "Aland", "country", box(0, 0, 10, 10), {"country_id": 1})
    for i in range(25):
        _add(db, 100 + i, f"M{i}", "localadmin", Point(0.1, 0.1), {})
    for i in range(120):
        _add(db, 1000 + i, f"L{i}", "locality", Point(0.1, 0.1), {})
    db.commit()
    db.close()
    boundaries = [wof_patch.Boundary("osm", 1000 + i, 6, f"Province{i}", box(i * 3.4, 0, i * 3.4 + 3.4, 10)) for i in range(3)]
    boundaries += [wof_patch.Boundary("osm", 2000 + i, 8, f"M{i}", box((i % 5) * 2, (i // 5) * 2, (i % 5) * 2 + 2, (i // 5) * 2 + 2)) for i in range(25)]
    boundaries += [wof_patch.Boundary("osm", 3000 + i, 9, f"L{i}", box((i % 12) * 10 / 12, (i // 12), (i % 12) * 10 / 12 + 10 / 12, (i // 12) + 1)) for i in range(120)]
    result = wof_patch.detect_levels(path, boundaries)
    assert result["suggest"] == {"localadmin_levels": [8], "levels": [9]}
    assert [r["level"] for r in result["levels"]] == [6, 8, 9]
    assert result["levels"][1]["match_localadmin"] == 1.0 and result["levels"][1]["coverage"] == pytest.approx(1.0)


@pytest.mark.skipif(shutil.which("osmium") is None, reason="osmium not installed")
def test_reads_relation_polygons_with_osmium(tmp_path):
    stamp = "version='1' timestamp='2024-01-01T00:00:00Z'"
    nodes = [(1, 50.0, 4.0), (2, 50.0, 4.1), (3, 50.1, 4.1), (4, 50.1, 4.0)]
    xml = ("<?xml version='1.0' encoding='UTF-8'?><osm version='0.6'>"
           + "".join(f"<node id='{n}' {stamp} lat='{lat}' lon='{lon}'/>" for n, lat, lon in nodes)
           + f"<way id='1' {stamp}><nd ref='1'/><nd ref='2'/><nd ref='3'/><nd ref='4'/><nd ref='1'/></way>"
           + f"<relation id='7' {stamp}><member type='way' ref='1' role='outer'/><tag k='type' v='multipolygon'/><tag k='boundary' v='administrative'/>"
             "<tag k='admin_level' v='9'/><tag k='name' v='Olmen'/></relation>"
           + f"<relation id='8' {stamp}><member type='way' ref='1' role='outer'/><tag k='type' v='multipolygon'/><tag k='boundary' v='administrative'/>"
             "<tag k='admin_level' v='8'/></relation></osm>")  # nameless: skipped
    source = tmp_path / "in.osm.pbf"
    import subprocess

    subprocess.run(["osmium", "cat", "-F", "osm", "-", "-o", str(source), "--overwrite"], input=xml, text=True, check=True)
    found = wof_patch.read_osm_boundaries("osmium", source, [8, 9], tmp_path / "work")
    assert [(b.osm_id, b.level, b.name) for b in found] == [(7, 9, "Olmen")]
    assert found[0].geom.bounds == pytest.approx((4.0, 50.0, 4.1, 50.1))


# ---- the stage ---------------------------------------------------------------------------------------------------

@pytest.fixture()
def cfg(app, monkeypatch):
    session = SessionLocal()
    session.add(Country(iso2="aa", name="Aland", ne_geometry_ref="x", bbox_json=[0, 0, 10, 10], geofabrik_path="europe/aa", wof_code="aa",
                        srtm_bbox_json=[0, 1, 0, 1]))
    session.commit()
    boundary_service.set_state(session.get(Country, "aa"), "osm_admin", SourceState(True, {"levels": [9], "localadmin_levels": [8]}))
    session.commit()
    profile = profiles.create_profile(session, "dev")
    region_service.assign_country(session, region_service.create_region(session, profile.id, "r1"), "aa")
    # approved inputs: the country PBF asset and the WOF bundle download
    run = runs.create_run(session, "extract-countries", None)
    pbf = Path(config.DATA_ROOT) / "aa.osm.pbf"
    pbf.write_bytes(b"pbf")
    asset = assets.create(session, run.id, None, "country-pbf", "europe/aa", pbf)
    asset.status = "approved"
    wof_dir = Path(config.DATA_ROOT) / "downloads" / "wof" / "admin-aa" / "20260101T000000Z"
    wof_dir.mkdir(parents=True)
    plain = make_wof(Path(config.DATA_ROOT) / "plain.db")
    bundle = wof_dir / "whosonfirst-data-admin-aa-latest.db.bz2"
    bundle.write_bytes(bz2.compress(plain.read_bytes()))
    session.add(DownloadRecord(source_key="wof:admin-aa", version_label="20260101T000000Z", url="x", filename=bundle.name,
                               local_path=str(bundle.relative_to(config.DATA_ROOT)), size_bytes=1, content_hash="h", status="approved", fetched_at=datetime.datetime(2026, 1, 1)))
    session.commit()
    monkeypatch.setattr(osmium, "binary", lambda session: "osmium")
    monkeypatch.setattr(wof_patch, "read_osm_boundaries",
                        lambda exe, pbf, levels, work: [b for b in NEW if b.level in set(levels)])
    return profile


def _resolved(profile_id):
    from datamanager.services import resolve

    return resolve.to_dict(resolve.resolve_config(SessionLocal(), profile_id))


def _run(profile_id, params=None):
    run = runs.create_run(SessionLocal(), "wof-patch", profile_id, params=params)
    result = tasks.run_stage(run.id)
    session = SessionLocal()
    session.refresh(run)
    return run, result


def test_stage_patches_then_skips_unchanged_and_status_follows(cfg):
    session = SessionLocal()
    assert stage_status.compute(session, cfg.id, _resolved(cfg.id), {})["wof-patch"].state == "todo"
    run, result = _run(cfg.id)
    assert result["status"] == "awaiting_review", run.report_json
    entry = run.report_json["patch"][0]
    assert entry["result"] == "patched" and entry["stats"]["locality"]["new"] == 1 and entry["unreplaced_total"] == 1
    assert [x["name"] for x in entry["unreplaced"]] == ["Gone"]
    runs.approve(SessionLocal(), runs.get_run(SessionLocal(), run.id))
    asset = assets.current(SessionLocal(), None, "wof-patched-sqlite", "aa")
    assert asset is not None and asset.path.endswith("whosonfirst-data-admin-aa-latest.db")
    assert sqlite3.connect(assets.abs_path(asset)).execute("SELECT COUNT(*) FROM spr WHERE repo = 'swayrider-patch'").fetchone() == (2,)
    assert stage_status.compute(SessionLocal(), cfg.id, _resolved(cfg.id), {})["wof-patch"].state == "ok"
    run, _ = _run(cfg.id)
    assert run.report_json["patch"][0]["result"] == "unchanged"
    runs.approve(SessionLocal(), runs.get_run(SessionLocal(), run.id))
    boundary_service.set_state(SessionLocal().get(Country, "aa"), "osm_admin", SourceState(True, {"levels": [9], "localadmin_levels": []}))
    SessionLocal().commit()
    assert stage_status.compute(SessionLocal(), cfg.id, _resolved(cfg.id), {})["wof-patch"].state == "outdated"


def test_stage_blocked_without_approved_inputs(cfg):
    session = SessionLocal()
    session.query(DownloadRecord).update({"status": "fetched"})
    session.commit()
    run, result = _run(cfg.id)
    assert result["status"] == "failed" and "Who's On First bundle" in run.report_json["error"]


def test_stage_without_any_boundary_source_has_nothing_to_do(cfg):
    boundary_service.set_state(SessionLocal().get(Country, "aa"), "osm_admin", SourceState(False, {"levels": [], "localadmin_levels": []}))
    SessionLocal().commit()
    run, result = _run(cfg.id)
    assert result["status"] == "failed" and "No country has a locality boundary source" in run.report_json["error"]


def test_detect_run_reports_levels_without_patching(cfg, monkeypatch):
    monkeypatch.setattr(wof_patch, "read_osm_boundaries", lambda exe, pbf, levels, work: NEW)
    run, result = _run(cfg.id, params={"detect": ["aa"]})
    assert result["status"] == "awaiting_review", run.report_json
    assert run.report_json["detect"][0]["iso2"] == "aa" and {r["level"] for r in run.report_json["detect"][0]["levels"]} == {8, 9}
    assert assets.current(SessionLocal(), None, "wof-patched-sqlite", "aa") is None


def test_modal_detect_and_apply_endpoints(cfg, client, monkeypatch):
    monkeypatch.setattr("datamanager.blueprints.build.routes.enqueue_run", lambda run_id: "job")
    page = client.post("/countries/aa/detect-levels")
    assert page.status_code == 200 and "started" in page.get_data(as_text=True)
    assert runs.list_runs(SessionLocal())[0].params_json == {"detect": ["aa"]}
    saved = client.post("/countries/aa/apply-levels", data={"osm_admin_levels": "10", "osm_localadmin_levels": "8"})
    assert saved.status_code == 200
    assert boundary_service.get_state(SessionLocal().get(Country, "aa"), "osm_admin").config == {"levels": [10], "localadmin_levels": [8]}
    assert client.post("/countries/aa/apply-levels", data={"osm_admin_levels": "99"}).status_code == 422


def test_build_page_shows_the_card(cfg, client):
    assert "Patch WOF localities" in client.get("/build/").get_data(as_text=True)


def test_nested_parents_pick_the_smallest_and_are_not_ambiguous(tmp_path):
    """Luxembourg-style: a district region around a canton region, both covering a municipality completely."""
    path = tmp_path / "wof.db"
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    _add(db, 1, "Aland", "country", box(0, 0, 10, 10), {"country_id": 1})
    _add(db, 2, "District", "region", box(0, 0, 10, 10), {"country_id": 1, "region_id": 2})
    _add(db, 3, "Canton", "region", box(0, 0, 5, 5), {"country_id": 1, "region_id": 3})
    _add(db, 4, "Other canton", "region", box(4, 4, 9, 9), {"country_id": 1, "region_id": 4})  # overlaps Canton, not nested
    db.commit()
    inside = wof_patch.Boundary("osm", 1, 8, "Inside", box(1, 1, 2, 2))
    straddle = wof_patch.Boundary("osm", 2, 8, "Straddle", box(3.9, 3.9, 4.6, 4.6))
    patch = wof_patch.build_patch(db, [], [inside, straddle])
    by_name = {n.name: n for n in patch.new}
    assert by_name["Inside"].parent.id == 3  # the canton, not the district
    assert patch.stats["localadmin"]["ambiguous"] == 1  # only the one between two non-nested cantons
