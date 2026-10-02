"""Patching Who's On First locality and municipality polygons with better boundaries (OSM `admin_level` relations or an
official polygon file), and detecting which OSM levels a country uses.

Why (DESIGN.md "Locality boundaries"): Pelias assigns a town to an address by point-in-polygon against WOF, and WOF's
polygons are missing (points only) or misplaced in many countries. The Pelias WOF importer and its admin lookup read
nothing but `geojson.body` joined to `spr` of every `<datapath>/sqlite/whosonfirst-data-*.db`, so a new record in those two
tables is enough (verified by the 2026-10-02 spike). Rules:

* a patched placetype (`locality`, `localadmin`) loses the WOF polygon records that a new polygon covers (replaced) and
  the point records lying inside a patched polygon of the same name (duplicates). A WOF polygon that no new polygon
  covers stays and is listed for review, so a source that does not tile the country (Germany's levels 9 and 10) leaves
  no holes; other point records stay too: hamlets that exist only as points are searchable places and take no part in
  the lookup. (Removing every point inside a polygon would remove nearly all of them, because the levels tile the country.)
* a new record's parent is the best-overlapping polygon of the next coarser level that covers at least half of it
  (new localadmin → county → region → country for localities, county → region → country for municipalities), because
  the lookup resolves the hierarchy against polygon records only;
* ids are `BASE[source] + OSM relation id` (stable, collision-free, safe as a JS number)."""
import bz2
import hashlib
import json
import shutil
import sqlite3
import tarfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from shapely.geometry import Point, mapping, shape
from shapely.ops import unary_union
from shapely.prepared import prep
from shapely.strtree import STRtree

from datamanager.services import osmium

PATCH_VERSION = 2  # bump when the record layout or the rules change: every country is patched again (2: re-homing, parent tie-break)
BASE = {"locality": 9_000_000_000_000, "localadmin": 8_000_000_000_000}
OFFICIAL_BASE = 7_000_000_000_000
MIN_COVER = 0.5  # a parent must cover this share of the new polygon
AMBIGUOUS = 0.3  # a second candidate with this share of the best overlap is counted as ambiguous
NESTED = 0.9  # two candidates of which one lies for this share inside the other are nested, not ambiguous
COUNTRY_MARGIN = 0.02  # degrees: polygons whose inner point lies this close to the country still count
REPO = "swayrider-patch"
LEVELS = range(5, 13)
# keys of a parent's hierarchy that are finer than the record being created are dropped
FINER = {"localadmin": ("localadmin_id", "locality_id", "borough_id", "macrohood_id", "neighbourhood_id", "microhood_id"),
         "locality": ("locality_id", "borough_id", "macrohood_id", "neighbourhood_id", "microhood_id")}


class PatchError(Exception):
    pass


@dataclass
class Boundary:
    """One polygon from a source: an OSM relation (`osm_id`) or an official feature (`osm_id` = stable hash)."""

    source: str  # osm | official
    osm_id: int
    level: int | None
    name: str
    geom: object


@dataclass
class Rec:
    """A WOF record as read from the bundle (or a new one)."""

    id: int
    name: str
    placetype: str
    geom: object | None  # None for a point record
    hierarchy: dict
    lat: float = 0.0
    lon: float = 0.0
    country: str = ""


@dataclass
class NewRecord:
    placetype: str
    id: int
    name: str
    geom: object
    parent: Rec
    parent_level: str
    hierarchy: dict
    source: str
    osm_id: int


@dataclass
class Patch:
    new: list[NewRecord] = field(default_factory=list)
    remove: dict[int, str] = field(default_factory=dict)  # wof id -> polygon | point-inside
    rehome: dict[int, dict] = field(default_factory=dict)  # kept record id -> hierarchy with removed ids replaced / dropped
    unreplaced: list[dict] = field(default_factory=list)  # WOF polygon records no new polygon covers: they stay, listed for review
    stats: dict = field(default_factory=dict)
    skipped: dict = field(default_factory=dict)


# ---- reading sources ------------------------------------------------------------------------------------------

def unpack_wof(source: Path, target: Path) -> Path:
    """The WOF bundle (`.db.bz2`, or `.db.tar.bz2`) as a plain SQLite file at `target`."""
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(".part")
    if source.name.endswith(".tar.bz2"):
        with tarfile.open(source, "r:bz2") as archive:
            member = next((m for m in archive.getmembers() if m.name.endswith(".db")), None)
            if member is None:
                raise PatchError(f"{source.name} holds no .db file")
            with archive.extractfile(member) as src, open(temp, "wb") as out:
                shutil.copyfileobj(src, out, 1024 * 1024)
    else:
        with bz2.open(source, "rb") as src, open(temp, "wb") as out:
            shutil.copyfileobj(src, out, 1024 * 1024)
    temp.replace(target)
    return target


def read_osm_boundaries(exe: str, pbf: Path, levels, work: Path) -> list[Boundary]:
    """All named administrative relations of the given `admin_level`s as polygons (`osmium tags-filter` + `export`)."""
    levels = sorted({int(level) for level in levels})
    if not levels:
        return []
    work.mkdir(parents=True, exist_ok=True)
    filtered, exported = work / "admin.osm.pbf", work / "admin.geojsonseq"
    osmium._run([exe, "tags-filter", str(pbf), "r/admin_level=" + ",".join(map(str, levels)), "-o", str(filtered), "--overwrite"])
    osmium._run([exe, "export", str(filtered), "-f", "geojsonseq", "-u", "type_id", "--geometry-types=polygon", "-o", str(exported), "--overwrite"])
    found = []
    with open(exported, encoding="utf-8") as lines:
        for line in lines:
            feature = json.loads(line.lstrip("\x1e"))
            props, ident = feature.get("properties", {}), str(feature.get("id", ""))
            # relation multipolygons arrive as areas with the odd id 2 * relation + 1; even ids are closed ways
            if not ident.startswith("a") or not ident[1:].isdigit() or int(ident[1:]) % 2 == 0:
                continue
            if props.get("boundary") != "administrative" or not str(props.get("admin_level", "")).isdigit():
                continue
            name = (props.get("name") or "").strip()
            if not name or feature["geometry"]["type"] not in ("Polygon", "MultiPolygon"):
                continue
            geom = shape(feature["geometry"]).buffer(0)
            if not geom.is_empty:
                found.append(Boundary("osm", (int(ident[1:]) - 1) // 2, int(props["admin_level"]), name, geom))
    filtered.unlink(missing_ok=True)
    exported.unlink(missing_ok=True)
    return found


def read_official(file: Path, name_field: str, work: Path) -> list[Boundary]:
    """Polygons of an official GeoJSON / zipped shapefile; the place name comes from `name_field`."""
    import geopandas

    path = f"zip://{file}" if file.suffix.lower() == ".zip" else str(file)
    try:
        frame = geopandas.read_file(path)
    except Exception as exc:  # noqa: BLE001 (pyogrio raises its own error types)
        raise PatchError(f"Cannot read {file.name}: {exc}") from exc
    if name_field not in frame.columns:
        raise PatchError(f"{file.name} has no attribute {name_field!r} (it has: {', '.join(map(str, frame.columns[:12]))})")
    if frame.crs is not None and frame.crs.to_epsg() != 4326:
        frame = frame.to_crs(4326)
    found, seen = [], set()
    for name, geometry in zip(frame[name_field], frame.geometry):
        name = str(name or "").strip()
        if not name or geometry is None or geometry.is_empty or geometry.geom_type not in ("Polygon", "MultiPolygon"):
            continue
        geometry = geometry.buffer(0)
        centre = geometry.representative_point()
        ident = int(hashlib.sha1(f"{name}|{centre.x:.5f}|{centre.y:.5f}".encode()).hexdigest()[:8], 16)
        if ident not in seen:
            seen.add(ident)
            found.append(Boundary("official", ident, None, name, geometry))
    return found


# ---- reading the WOF bundle ----------------------------------------------------------------------------------

def _hierarchy(props: dict) -> dict:
    hierarchies = props.get("wof:hierarchy") or [{}]
    return dict(hierarchies[0]) if isinstance(hierarchies, list) and hierarchies else {}


def wof_records(db: sqlite3.Connection, placetype: str, geometry: bool = True) -> list[Rec]:
    """Active records of a placetype (not deprecated, not superseded, named). Points have `geom` None."""
    found = []
    query = ("SELECT s.id, s.name, s.latitude, s.longitude, g.body FROM spr s JOIN geojson g ON g.id = s.id AND g.is_alt = 0 "
             "WHERE s.placetype = ? AND s.is_deprecated = 0 AND s.is_superseded = 0 AND TRIM(IFNULL(s.name, '')) != ''")
    for id_, name, lat, lon, body in db.execute(query, (placetype,)):
        feature = json.loads(body)
        kind = feature.get("geometry", {}).get("type")
        geom = None
        if kind in ("Polygon", "MultiPolygon") and geometry:
            geom = shape(feature["geometry"]).buffer(0)
        props = feature.get("properties", {})
        found.append(Rec(id_, name, placetype, geom, _hierarchy(props), lat or 0.0, lon or 0.0, props.get("wof:country", "")))
    return found


class Pool:
    """Polygons of one placetype for parent lookups."""

    def __init__(self, label: str, recs: list[Rec]):
        self.label = label
        self.recs = [r for r in recs if r.geom is not None]
        self.tree = STRtree([r.geom for r in self.recs]) if self.recs else None

    def best(self, geom) -> tuple[Rec | None, float, bool]:
        """(record, covered share of `geom`, ambiguous) of the best overlapping record. At an equal cover the smallest record
        wins (WOF lists nested regions, e.g. Luxembourg's districts around its cantons, and WOF itself uses the specific one).
        Ambiguous = a second record covers a similar share without being nested with the best one."""
        if self.tree is None or geom.area == 0:
            return None, 0.0, False
        found = []
        for index in self.tree.query(geom):
            rec = self.recs[int(index)]
            area = geom.intersection(rec.geom).area
            if area > 0:
                found.append((round(area / geom.area, 6), rec))
        if not found:
            return None, 0.0, False
        found.sort(key=lambda item: (-item[0], item[1].geom.area))
        cover, best = found[0]
        for other_cover, other in found[1:]:
            if other_cover < AMBIGUOUS * cover:
                break
            small = min(best.geom.area, other.geom.area)
            if best.geom.intersection(other.geom).area < NESTED * small:
                return best, cover, True
        return best, cover, False


def choose_parent(geom, pools: list[Pool], fallback: Rec | None) -> tuple[Rec | None, str, bool]:
    """First pool whose best record covers at least MIN_COVER of `geom`; else the country record as last resort."""
    for pool in pools:
        rec, cover, unsure = pool.best(geom)
        if rec is not None and cover >= MIN_COVER:
            return rec, pool.label, unsure
    return fallback, "country", False


# ---- the patch ---------------------------------------------------------------------------------------------------

def near_country(country, margin: float = COUNTRY_MARGIN):
    """Test for "inner point lies in or close to the country". The buffer of a big country outline is expensive, so it is
    made and prepared once (buffering it per polygon took hours for Germany)."""
    if country is None:
        return lambda geom: True
    prepared = prep(country.buffer(margin))
    return lambda geom: prepared.contains(geom.representative_point())


def build_patch(db: sqlite3.Connection, locality: list[Boundary], localadmin: list[Boundary]) -> Patch:
    """Plan the patch of one country: new records, removals and the review lists. Nothing is written."""
    patch = Patch()
    countries = wof_records(db, "country")
    country_geom = unary_union([c.geom for c in countries if c.geom is not None]) if countries else None
    country_rec = countries[0] if countries else None
    if country_geom is not None and country_geom.is_empty:
        country_geom = None

    inside = near_country(country_geom)

    def clip(items):
        kept = [b for b in items if inside(b.geom)]
        return kept, len(items) - len(kept)

    locality, outside_l = clip(locality)
    localadmin, outside_a = clip(localadmin)
    patch.skipped = {"outside_country": outside_l + outside_a}
    county, region = wof_records(db, "county"), wof_records(db, "region")
    base_pools = [Pool("county", county), Pool("region", region)]

    def make(items, placetype, pools):
        out, levels, ambiguous = [], {}, 0
        for item in items:
            parent, level, unsure = choose_parent(item.geom, pools, country_rec)
            if parent is None:
                patch.skipped["no_parent"] = patch.skipped.get("no_parent", 0) + 1
                continue
            ambiguous += unsure
            new_id = (OFFICIAL_BASE if item.source == "official" else BASE[placetype]) + item.osm_id
            hierarchy = {k: v for k, v in parent.hierarchy.items() if k not in FINER[placetype]}
            hierarchy[f"{placetype}_id"] = new_id
            levels[level] = levels.get(level, 0) + 1
            out.append(NewRecord(placetype, new_id, item.name, item.geom, parent, level, hierarchy, item.source, item.osm_id))
        patch.stats[placetype] = {"new": len(out), "parent_levels": levels, "ambiguous": ambiguous}
        return out

    trees: dict[str, tuple[list[NewRecord], STRtree]] = {}

    def retire(placetype, news) -> list[Rec]:
        """Plan the removal of the WOF records a patched placetype replaces and return the polygon records that stay.
        A WOF polygon goes only when a new polygon covers its inner point (it is replaced); one nothing covers stays,
        so a source that does not tile the whole country never leaves holes. A point goes only as a same-name duplicate."""
        areas = prep(unary_union([n.geom for n in news]))
        tree = STRtree([n.geom for n in news])
        trees[placetype] = (news, tree)
        removed_polygons = removed_points = kept_points = 0
        kept_polygons: list[Rec] = []
        for rec in wof_records(db, placetype):
            if rec.geom is not None:
                point = rec.geom.representative_point()
                if any(news[int(i)].geom.contains(point) for i in tree.query(point)):
                    patch.remove[rec.id] = "polygon"
                    removed_polygons += 1
                else:
                    kept_polygons.append(rec)
                    patch.unreplaced.append({"id": rec.id, "name": rec.name, "placetype": placetype, "lat": round(point.y, 5), "lon": round(point.x, 5)})
            elif rec.lat or rec.lon:
                point = Point(rec.lon, rec.lat)
                if areas.contains(point) and any(news[int(i)].geom.contains(point) and news[int(i)].name.casefold() == rec.name.casefold()
                                                 for i in tree.query(point)):
                    patch.remove[rec.id] = "point-inside"  # the same place as a patched polygon: a duplicate search result
                    removed_points += 1
                else:
                    kept_points += 1
            else:
                kept_points += 1
        patch.stats[placetype].update(removed_polygons=removed_polygons, removed_points=removed_points, kept_points=kept_points,
                                      kept_polygons=len(kept_polygons))
        return kept_polygons

    new_admin = make(localadmin, "localadmin", base_pools) if localadmin else []
    if new_admin:
        kept_admin = retire("localadmin", new_admin)
        admin_pool = Pool("localadmin", [Rec(n.id, n.name, "localadmin", n.geom, n.hierarchy) for n in new_admin] + kept_admin)
    else:
        admin_pool = Pool("localadmin", [r for r in wof_records(db, "localadmin") if r.geom is not None])
    new_locality = make(locality, "locality", [admin_pool, *base_pools]) if locality else []
    if new_locality:
        retire("locality", new_locality)
    patch.new = new_admin + new_locality
    rehome(db, patch, trees)
    return patch


REHOME_TYPES = ("localadmin", "locality", "neighbourhood", "borough", "macrohood", "microhood", "campus")


def rehome(db: sqlite3.Connection, patch: Patch, trees: dict) -> None:
    """Records that stay can reference a removed record in their hierarchy (a kept locality polygon its replaced
    municipality, every neighbourhood its replaced locality). The lookup resolves a hit's hierarchy by id, so a removed id
    would silently drop that level from the result. Such an id is replaced by the new polygon covering the record's
    location, or dropped when none does."""
    removed = set(patch.remove)
    if not removed or not trees:
        return
    keys = {f"{pt}_id": pt for pt in trees}
    for placetype in REHOME_TYPES:
        for rec in wof_records(db, placetype):
            if rec.id in removed:
                continue
            stale = [k for k in keys if rec.hierarchy.get(k) in removed]
            if not stale:
                continue
            hierarchy = dict(rec.hierarchy)
            where = rec.geom.representative_point() if rec.geom is not None else Point(rec.lon, rec.lat)
            for key in stale:
                news, tree = trees[keys[key]]
                hit = next((news[int(i)] for i in tree.query(where) if news[int(i)].geom.contains(where)), None)
                if hit is not None:
                    hierarchy[key] = hit.id
                else:
                    del hierarchy[key]
            patch.rehome[rec.id] = hierarchy
    for pt in trees:
        patch.stats[pt]["rehomed"] = len(patch.rehome)


def write_patch(db: sqlite3.Connection, patch: Patch) -> None:
    """Apply a patch to the (copied) bundle: delete the removed records, insert the new ones."""
    now = int(time.time())
    for wof_id in patch.remove:
        for table in ("spr", "geojson", "names", "concordances", "ancestors"):
            db.execute(f"DELETE FROM {table} WHERE id = ?", (wof_id,))
    for wof_id, hierarchy in patch.rehome.items():
        row = db.execute("SELECT body FROM geojson WHERE id = ? AND is_alt = 0", (wof_id,)).fetchone()
        if row is None:
            continue
        feature = json.loads(row[0])
        feature["properties"]["wof:hierarchy"] = [hierarchy]
        db.execute("UPDATE geojson SET body = ?, lastmodified = ? WHERE id = ? AND is_alt = 0", (json.dumps(feature), now, wof_id))
        db.execute("DELETE FROM ancestors WHERE id = ?", (wof_id,))
        placetype = feature["properties"].get("wof:placetype")
        for key, ancestor in hierarchy.items():
            if key != f"{placetype}_id":
                db.execute("INSERT INTO ancestors (id, ancestor_id, ancestor_placetype, lastmodified) VALUES (?,?,?,?)", (wof_id, ancestor, key[:-3], now))
    for rec in patch.new:
        minx, miny, maxx, maxy = rec.geom.bounds
        centre = rec.geom.centroid
        props = {
            "wof:id": rec.id, "wof:name": rec.name, "wof:placetype": rec.placetype, "wof:country": rec.parent.country or "",
            "wof:parent_id": rec.parent.id, "wof:hierarchy": [rec.hierarchy], "wof:repo": REPO, "src:geom": rec.source,
            "mz:is_current": 1, "geom:latitude": centre.y, "geom:longitude": centre.x, "geom:bbox": f"{minx},{miny},{maxx},{maxy}",
            "swayrider:source_id": rec.osm_id,
        }
        body = json.dumps({"id": rec.id, "type": "Feature", "properties": props, "geometry": mapping(rec.geom), "bbox": [minx, miny, maxx, maxy]})
        db.execute(
            "INSERT INTO spr (id, parent_id, name, placetype, country, repo, latitude, longitude, min_latitude, min_longitude, "
            "max_latitude, max_longitude, is_current, is_deprecated, is_ceased, is_superseded, is_superseding, superseded_by, supersedes, lastmodified) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1,0,0,0,0,'[]','[]',?)",
            (rec.id, rec.parent.id, rec.name, rec.placetype, rec.parent.country, REPO, centre.y, centre.x, miny, minx, maxy, maxx, now))
        db.execute("INSERT INTO geojson (id, body, source, alt_label, is_alt, lastmodified) VALUES (?,?,?,?,0,?)", (rec.id, body, "swayrider", "", now))
        for key, ancestor in rec.hierarchy.items():
            if key != f"{rec.placetype}_id":
                db.execute("INSERT INTO ancestors (id, ancestor_id, ancestor_placetype, lastmodified) VALUES (?,?,?,?)", (rec.id, ancestor, key[:-3], now))
    db.commit()


def patch_country(wof_file: Path, locality: list[Boundary], localadmin: list[Boundary], out_file: Path) -> Patch:
    """Copy the unpacked bundle to `out_file`, patch the copy and return the plan with its statistics."""
    out_file.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(wof_file, out_file)
    db = sqlite3.connect(out_file)
    try:
        patch = build_patch(db, locality, localadmin)
        write_patch(db, patch)
        db.execute("VACUUM")
    finally:
        db.close()
    return patch


# ---- detecting the levels ------------------------------------------------------------------------------------

def detect_levels(wof_file: Path, boundaries: list[Boundary]) -> dict:
    """Per admin level: polygon count, how much of the country it covers and how many names also occur among the WOF
    `localadmin` / `locality` names, plus a suggested municipality (`localadmin`) and locality level.

    Municipality = the deepest level that covers the country (>= 90 %) and whose names match WOF localadmins (>= 35 %, WOF lacks many municipalities as records: Germany's level 8 matches 38 %);
    locality = a deeper level with at least 100 polygons, half the country covered and >= 80 % of the names among WOF
    localities. A suggestion only: a human decides (levels differ per country and a wrong one replaces real data)."""
    db = sqlite3.connect(wof_file)
    try:
        names = {pt: {r.name.strip().casefold() for r in wof_records(db, pt, geometry=False)} for pt in ("localadmin", "locality")}
        countries = [c.geom for c in wof_records(db, "country") if c.geom is not None]
    finally:
        db.close()
    country = unary_union(countries) if countries else None
    area = country.area if country is not None and country.area else 0.0
    inside = near_country(country)
    rows: dict[int, dict] = {}
    for b in boundaries:
        if b.level is None or not inside(b.geom):
            continue
        row = rows.setdefault(b.level, {"level": b.level, "polygons": 0, "area": 0.0, "localadmin": 0, "locality": 0})
        row["polygons"] += 1
        row["area"] += b.geom.area
        key = b.name.strip().casefold()
        row["localadmin"] += key in names["localadmin"]
        row["locality"] += key in names["locality"]
    table = []
    for level in sorted(rows):
        row = rows[level]
        table.append({"level": level, "polygons": row["polygons"], "coverage": round(row["area"] / area, 2) if area else None,
                      "match_localadmin": round(row["localadmin"] / row["polygons"], 2), "match_locality": round(row["locality"] / row["polygons"], 2)})
    municipality = next((r["level"] for r in reversed(table)
                         if (r["coverage"] or 0) >= 0.9 and r["match_localadmin"] >= 0.35 and r["polygons"] >= 20), None)
    locality = next((r["level"] for r in reversed(table)
                     if municipality is not None and r["level"] > municipality and r["polygons"] >= 100
                     and (r["coverage"] or 0) >= MIN_COVER and r["match_locality"] >= 0.8), None)
    return {"levels": table, "suggest": {"localadmin_levels": [municipality] if municipality is not None else [],
                                         "levels": [locality] if locality is not None else []},
            "wof": {pt: len(v) for pt, v in names.items()}}
