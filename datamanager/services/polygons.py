"""Polygons a build needs, derived from a configuration: per region the core, the 100 km overlap zone
and, per bordering region pair, the 10 km border zone; plus the keep-polygon of every carved country.

Uses the full-detail country geometry (not the ~1 km simplified copy overlap *detection* works on),
carved where the configuration carves and reduced to the mainland like the core is everywhere else.
Buffers are true distances (`overlap.buffer_m`). Output is lightly simplified so `.poly` files stay small."""
import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from pyproj import Geod
from shapely import make_valid
from shapely.geometry import MultiPolygon, Polygon, mapping, shape
from shapely.ops import unary_union
from sqlalchemy.orm import Session

from datamanager.errors import ValidationError
from datamanager.services import carve as carve_service
from datamanager.services import overlap
from datamanager.services import regions as region_service

SIMPLIFY_DEG = 0.001  # ~100 m
PREVIEW_DEG = 0.005
GEOD = Geod(ellps="WGS84")


@dataclass
class PolygonSpec:
    name: str
    kind: str  # core | overlap | border | carve
    regions: list[str]
    countries: list[str]
    geometry: object = field(repr=False)

    @property
    def asset_type(self) -> str:
        return f"{self.kind}-polygon"


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "region"


@lru_cache(maxsize=256)
def _full(path: str, mtime_ns: int, carve: str | None):
    geometry = shape(json.loads(Path(path).read_text()))
    return geometry.intersection(shape(json.loads(carve))) if carve else geometry


def _core(keys: tuple):
    parts = [overlap.mainland(_full(k[1], k[2], k[3])) for k in keys]
    return unary_union(parts) if parts else None


def compute(session: Session, config_id: int) -> list[PolygonSpec]:
    regions = [r for r in region_service.list_regions(session, config_id)]
    keys = {r.id: region_service.core_keys(session, r) for r in regions}
    usable = [r for r in regions if keys[r.id]]
    if not usable:
        raise ValidationError("No region has a core country with geometry.")

    specs: list[PolygonSpec] = []
    cores, buffers = {}, {}
    for region in usable:
        core = _core(keys[region.id])
        cores[region.id] = core
        countries = sorted(k[0] for k in keys[region.id])
        base = slug(region.name)
        buffers[region.id] = overlap.buffer_m(core, overlap.BORDER_BUFFER_M)
        specs.append(PolygonSpec(f"{base}-core", "core", [region.name], countries, core))
        specs.append(PolygonSpec(f"{base}-overlap", "overlap", [region.name], countries,
                                 overlap.buffer_m(core, overlap.OVERLAP_BUFFER_M)))

    for i, a in enumerate(usable):
        for b in usable[i + 1:]:
            if overlap.bordering(keys[a.id], keys[b.id]):  # same rule the resolved config uses
                zone = buffers[a.id].intersection(buffers[b.id])
                names = sorted([a.name, b.name], key=slug)
                specs.append(PolygonSpec(f"{slug(names[0])}-{slug(names[1])}-border", "border", names,
                                         sorted({k[0] for k in keys[a.id] + keys[b.id]}), zone))

    # a carved country needs its keep-polygon wherever it is used: as core or as overlap of some region
    members = {r.id: {k[0] for k in keys[r.id]} | set(region_service.effective_overlap(r)) for r in usable}
    for iso, keep in sorted(carve_service.carve_map(session, config_id).items()):
        if any(iso in m for m in members.values()):
            regions_of = sorted(r.name for r in usable if iso in members[r.id])
            specs.append(PolygonSpec(f"carve-{iso}", "carve", regions_of, [iso], shape(json.loads(keep))))
    return specs


def _polygons_of(geometry) -> list[Polygon]:
    if geometry.geom_type == "Polygon":
        return [geometry]
    if geometry.geom_type in ("MultiPolygon", "GeometryCollection"):
        return [p for g in geometry.geoms for p in _polygons_of(g)]
    return []


def finalize(geometry) -> tuple[object, dict]:
    """Simplified, valid polygonal geometry plus its validation facts."""
    simplified = geometry.simplify(SIMPLIFY_DEG, preserve_topology=True)
    valid = simplified.is_valid
    if not valid:
        simplified = make_valid(simplified)
    parts = [p for p in _polygons_of(simplified) if not p.is_empty]
    result = parts[0] if len(parts) == 1 else MultiPolygon(parts) if parts else Polygon()
    facts = {
        "valid": bool(parts) and result.is_valid,
        "fixed": not valid,
        "empty": not parts,
        "parts": len(parts),
        "vertices": sum(len(p.exterior.coords) + sum(len(h.coords) for h in p.interiors) for p in parts),
        "area_km2": round(abs(GEOD.geometry_area_perimeter(result)[0]) / 1e6) if parts else 0,
        "bbox": [round(v, 3) for v in result.bounds] if parts else None,
    }
    return result, facts


def preview(geometry) -> dict:
    """Small GeoJSON for the review map (coarse, rounded)."""
    simple = geometry.simplify(PREVIEW_DEG, preserve_topology=True)
    result = json.loads(json.dumps(mapping(simple)))
    return {**result, "coordinates": overlap._round(result["coordinates"])}


def to_poly(name: str, geometry) -> str:
    """Osmosis polygon filter format (`.poly`): outer rings numbered, holes prefixed with `!`."""
    lines = [name]
    section = 0
    for polygon in _polygons_of(geometry):
        section += 1
        lines.append(str(section))
        lines += [f"   {x:.6f}   {y:.6f}" for x, y in polygon.exterior.coords]
        lines.append("END")
        for hole in polygon.interiors:
            lines.append(f"!{section}")
            lines += [f"   {x:.6f}   {y:.6f}" for x, y in hole.coords]
            lines.append("END")
    lines.append("END")
    return "\n".join(lines) + "\n"


def write_files(directory: Path, name: str, geometry) -> tuple[Path, Path]:
    poly, geojson = directory / f"{name}.poly", directory / f"{name}.geojson"
    poly.write_text(to_poly(name, geometry))
    geojson.write_text(json.dumps(mapping(geometry)))
    return poly, geojson
