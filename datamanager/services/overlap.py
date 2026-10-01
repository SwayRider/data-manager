"""Derived geometry for regions: overlap countries and border zones.

Follows data-pipeline/pipeline/generate_polygons.py: a region's *overlap* is every
other country within 100 km of the region's core, and two regions form a *border
region* when their 10 km buffers intersect. Unlike the legacy script (EPSG:3857, where
100 km is only ~64 km at 50°N) the buffers are true distances (`buffer_m`).
Detection works on simplified geometry (it only needs to know "does it touch"),
cached per file mtime.
"""

import json
from functools import lru_cache
from pathlib import Path

from pyproj import CRS, Transformer
from shapely.geometry import mapping, shape
from shapely.ops import transform, unary_union

from datamanager.config import config

OVERLAP_BUFFER_M = 100_000  # data-pipeline OVERLAP_BUFFER_M
BORDER_BUFFER_M = 10_000  # data-pipeline BORDER_BUFFER_M
MAINLAND_GAP_DEG = 5  # ~500 km; parts further from the largest part are ignored for the core
DETECT_TOLERANCE = 0.01  # degrees, ~1 km
DISPLAY_TOLERANCE = 0.02
COORD_PRECISION = 3


@lru_cache(maxsize=512)
def _load(path: str, mtime_ns: int):
    return shape(json.loads(Path(path).read_text())).simplify(DETECT_TOLERANCE, preserve_topology=True)


def geometry_key(iso2: str, geometry_ref: str, carve: str | None = None) -> tuple | None:
    """Hashable, mtime-stamped handle for a country's geometry file (None if missing).
    `carve` is the canonical JSON of the polygon the configuration keeps, if any."""
    path = Path(config.DATA_ROOT) / geometry_ref
    if not path.is_file():
        return None
    return (iso2, str(path), path.stat().st_mtime_ns, carve)


@lru_cache(maxsize=512)
def _carved(path: str, mtime_ns: int, carve: str):
    return _load(path, mtime_ns).intersection(shape(json.loads(carve)))


def raw_geometry(key: tuple):
    """The country's own (uncarved) simplified geometry."""
    return _load(key[1], key[2])


def _geometry(key: tuple):
    return _carved(key[1], key[2], key[3]) if key[3] else _load(key[1], key[2])


geometry_of = _geometry


def display_geojson(geometry) -> dict:
    simplified = geometry.simplify(DISPLAY_TOLERANCE, preserve_topology=True)
    result = json.loads(json.dumps(mapping(simplified)))
    return {**result, "coordinates": _round(result["coordinates"])}


@lru_cache(maxsize=64)
def _aeqd(lon: float, lat: float) -> tuple[Transformer, Transformer]:
    local = CRS.from_proj4(f"+proj=aeqd +lat_0={lat} +lon_0={lon} +datum=WGS84 +units=m +no_defs")
    return (
        Transformer.from_crs("EPSG:4326", local, always_xy=True),
        Transformer.from_crs(local, "EPSG:4326", always_xy=True),
    )


def buffer_m(geometry, metres: int):
    """`geometry` (lon/lat) grown by `metres` of true distance.

    Buffers in an azimuthal equidistant projection centred on the geometry, where distances from
    the centre are exact, so the result is accurate at any latitude for regional-sized geometry
    (error ~1.5% at 2000 km across). The centre is rounded so nearby geometries share transformers.
    """
    centre = geometry.centroid
    forward, back = _aeqd(round(centre.x, 1), round(centre.y, 1))
    return transform(back.transform, transform(forward.transform, geometry).buffer(metres))


def core_union(geometries: list):
    geoms = [g for g in geometries if g is not None]
    return unary_union(geoms) if geoms else None


def mainland(geometry):
    """The largest part of a multi-part country plus parts within MAINLAND_GAP_DEG of it.

    Natural Earth countries include distant overseas territories (France: French Guiana,
    Netherlands: Curaçao...) whose neighbours must not count as overlap of a European core;
    nearby islands (Corsica, Sicily...) are kept.
    """
    if geometry.geom_type != "MultiPolygon":
        return geometry
    largest = max(geometry.geoms, key=lambda part: part.area)
    return unary_union([p for p in geometry.geoms if p is largest or p.distance(largest) <= MAINLAND_GAP_DEG])


def _core(core_keys: tuple):
    return core_union([mainland(_geometry(k)) for k in core_keys])


@lru_cache(maxsize=128)
def overlap_candidates(core_keys: tuple, universe_keys: tuple) -> tuple[str, ...]:
    """ISOs (from `universe_keys`) touching the core buffered by 100 km, core excluded.
    Keys come from `geometry_key`; cached because it is hit on every map interaction."""
    core = _core(core_keys)
    if core is None:
        return ()
    zone = buffer_m(core, OVERLAP_BUFFER_M)
    core_isos = {k[0] for k in core_keys}
    return tuple(sorted(k[0] for k in universe_keys if k[0] not in core_isos and zone.intersects(_geometry(k))))


@lru_cache(maxsize=128)
def bordering(core_keys_a: tuple, core_keys_b: tuple) -> bool:
    core_a, core_b = _core(core_keys_a), _core(core_keys_b)
    if core_a is None or core_b is None:
        return False
    return buffer_m(core_a, BORDER_BUFFER_M).intersects(buffer_m(core_b, BORDER_BUFFER_M))


def _round(obj):
    if isinstance(obj, (list, tuple)):
        return [_round(o) for o in obj]
    return round(obj, COORD_PRECISION) if isinstance(obj, float) else obj


@lru_cache(maxsize=32)
def buffer_geojson(core_keys: tuple) -> dict | None:
    """The 100 km buffer of the core as display GeoJSON geometry (simplified, rounded)."""
    core = _core(core_keys)
    if core is None:
        return None
    shape_ = buffer_m(core, OVERLAP_BUFFER_M).simplify(DISPLAY_TOLERANCE, preserve_topology=True)
    geometry = json.loads(json.dumps(mapping(shape_)))
    return {**geometry, "coordinates": _round(geometry["coordinates"])}


