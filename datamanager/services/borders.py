"""Region outlines and border crossings, ported from data-pipeline's `osm_funcs.outline` and `border_crossing`.

Both work on the region PBFs through osmium-tool (no pyosmium): the outline is the union of the admin_level=2
boundary multipolygons, the crossings are the motorway..secondary roads that cross a core outline, found on the
linestrings `osmium export` writes for the roads of a border-area extract."""
import csv
import json
from dataclasses import dataclass
from pathlib import Path

from shapely.geometry import LineString, MultiPolygon, Point, Polygon, mapping, shape
from shapely.ops import unary_union
from shapely.strtree import STRtree

from datamanager.services import osmium

ROAD_TYPES = ("motorway", "motorway_link", "trunk", "trunk_link", "primary", "primary_link", "secondary", "secondary_link")
EPSILON = 1e-5  # degrees either side of a crossing used to tell which side of the outline the road continues on
CSV_HEADER = ["osm_id", "osm_type", "from_region", "to_region", "lon", "lat"]


@dataclass
class Crossing:
    osm_id: int
    osm_type: str  # the highway tag value
    from_region: str
    to_region: str
    location: Point


def _geometries(file: Path) -> list[dict]:
    """Features of an `osmium export -f geojson` / `geojsonseq` file."""
    text = Path(file).read_text(encoding="utf-8")
    if text.lstrip().startswith("{") and '"FeatureCollection"' in text[:200]:
        return json.loads(text).get("features", [])
    return [json.loads(line.lstrip("\x1e")) for line in text.splitlines() if line.strip()]


def outline(exe: str, pbf: Path, out_geojson: Path, work: Path) -> dict:
    """The region's country outline: union of its admin_level=2 boundary multipolygons, one feature per part.
    Returns facts: parts, vertices, bbox."""
    work.mkdir(parents=True, exist_ok=True)
    raw, sorted_file, exported = work / "outline-raw.osm.pbf", work / "outline.osm.pbf", work / "outline.geojson"
    osmium._run([exe, "tags-filter", str(pbf), "r/boundary=administrative", "r/admin_level=2", "-o", str(raw), "--overwrite"])
    osmium._run([exe, "sort", str(raw), "-o", str(sorted_file), "--strategy=multipass", "--overwrite"])
    osmium._run([exe, "export", str(sorted_file), "-o", str(exported), "--geometry-types=multipolygon", "--overwrite"])
    shapes = [shape(f["geometry"]) for f in _geometries(exported) if f.get("geometry")]
    for tmp in (raw, sorted_file, exported):
        tmp.unlink(missing_ok=True)
    merged = unary_union(shapes) if shapes else MultiPolygon()
    parts = list(merged.geoms) if hasattr(merged, "geoms") else [merged]
    parts = [p for p in parts if not p.is_empty]
    out_geojson.parent.mkdir(parents=True, exist_ok=True)
    collection = {"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {}, "geometry": mapping(p)} for p in parts]}
    out_geojson.write_text(json.dumps(collection), encoding="utf-8")
    return {
        "parts": len(parts),
        "vertices": sum(len(p.exterior.coords) + sum(len(i.coords) for i in p.interiors) for p in parts if isinstance(p, Polygon)),
        "bbox": [round(v, 5) for v in unary_union(parts).bounds] if parts else None,
    }


def load_outline(path: Path) -> list[Polygon]:
    return [shape(f["geometry"]) for f in json.loads(Path(path).read_text(encoding="utf-8"))["features"]]


def roads(exe: str, pbf: Path, work: Path) -> list[dict]:
    """The crossing-relevant roads of a PBF as GeoJSON features (`properties`: @id, highway, oneway)."""
    work.mkdir(parents=True, exist_ok=True)
    filtered, exported = work / "roads.osm.pbf", work / "roads.geojsonseq"
    osmium._run([exe, "tags-filter", str(pbf), *(f"w/highway={t}" for t in ROAD_TYPES), "-o", str(filtered), "--overwrite"])
    osmium._run([exe, "export", str(filtered), "-f", "geojsonseq", "--geometry-types=linestring", "-a", "type,id",
                 "-o", str(exported), "--overwrite"])
    features = _geometries(exported)
    filtered.unlink(missing_ok=True)
    exported.unlink(missing_ok=True)
    return features


def _check(before: Point, after: Point, poly: Polygon, polygons: list[Polygon], name_1: str, name_2: str):
    before_region = name_1 if poly.contains(before) else ""
    after_region = name_1 if poly.contains(after) else ""
    if before_region == after_region:
        return None
    search = after if after_region == "" else before
    if not any(p.contains(search) for p in polygons):
        before_region = before_region or name_2
        after_region = after_region or name_2
    if before_region and after_region:
        return before_region, after_region
    return None


def detect(features: list[dict], polygons: list[Polygon], name_1: str, name_2: str) -> list[Crossing]:
    """Crossings of `features` (road linestrings) over the boundary of region 1's outline polygons. A two-way road
    gives one crossing per direction, `oneway=yes|1|true` only forward, `oneway=-1|reverse` only backward."""
    if not polygons:
        return []
    index = STRtree(polygons)
    found: list[Crossing] = []
    for feature in features:
        props = feature.get("properties") or {}
        highway = props.get("highway")
        if highway not in ROAD_TYPES or not feature.get("geometry"):
            continue
        line = shape(feature["geometry"])
        if not isinstance(line, LineString) or len(line.coords) < 2:
            continue
        oneway = props.get("oneway")
        orientation = "forward" if oneway in ("yes", "1", "true") else "reverse" if oneway in ("-1", "reverse") else None
        osm_id = int(props.get("@id", 0))
        for i in index.query(line):
            poly = polygons[i]
            hit = poly.boundary.intersection(line)
            if hit.is_empty:
                continue
            points = [hit] if hit.geom_type == "Point" else list(hit.geoms) if hit.geom_type == "MultiPoint" else []
            for point in points:
                distance = line.project(point)
                before = line.interpolate(max(distance - EPSILON, 0))
                after = line.interpolate(min(distance + EPSILON, line.length))
                sides = _check(before, after, poly, polygons, name_1, name_2)
                if sides is None:
                    continue
                if orientation in (None, "forward"):
                    found.append(Crossing(osm_id, highway, sides[0], sides[1], point))
                if orientation in (None, "reverse"):
                    found.append(Crossing(osm_id, highway, sides[1], sides[0], point))
    return found


def write_csv(path: Path, crossings: list[Crossing]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(CSV_HEADER)
        for c in crossings:
            writer.writerow([c.osm_id, c.osm_type, c.from_region, c.to_region, c.location.x, c.location.y])
    return path


def crossings_geojson(crossings: list[Crossing]) -> dict:
    return {"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": mapping(c.location),
         "properties": {"osm_id": c.osm_id, "osm_type": c.osm_type, "from": c.from_region, "to": c.to_region}}
        for c in crossings]}
