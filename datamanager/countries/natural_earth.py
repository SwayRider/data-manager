"""Natural Earth admin-0 country loading.

Adapted from data-pipeline/pipeline/generate_polygons.py::load_ne_countries,
extended to also expose each country's own geometry.bounds (the original
only needed unioned *region* geometry for buffering, not per-country bounds).
This is Phase 0's own "fetch-once-and-pin" download, deliberately separate
from data-pipeline's copy and flagged to be replaced by the real
DownloadManager/download_source machinery in Phase 2.
"""

import math
from pathlib import Path

import geopandas as gpd
import requests
from shapely.ops import unary_union

from datamanager.errors import DownloadError
from datamanager.services import overlap


def ensure_natural_earth(url: str, target_dir: str) -> Path:
    """Downloads and caches the Natural Earth admin-0 countries archive.
    Fetch-once-and-pin: does nothing if already present.
    """
    target_dir_path = Path(target_dir)
    target_dir_path.mkdir(parents=True, exist_ok=True)
    zip_path = target_dir_path / "ne_10m_admin_0_countries.zip"
    if zip_path.exists():
        return zip_path

    try:
        response = requests.get(url, timeout=60)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise DownloadError(f"failed to download Natural Earth data: {exc}", url=url) from exc

    zip_path.write_bytes(response.content)
    return zip_path


def load_ne_countries(ne_dir: str) -> dict[str, dict] | None:
    """Returns {iso2: {"geometry": <shapely geom>, "bounds": (minx, miny, maxx, maxy)}}
    keyed by lowercased ISO_A2 (falling back to ISO_A2_EH), matching
    generate_polygons.py's convention. Duplicate ISO codes are unioned.
    """
    zip_path = Path(ne_dir) / "ne_10m_admin_0_countries.zip"
    shp_path = Path(ne_dir) / "ne_10m_admin_0_countries.shp"

    if zip_path.exists():
        path = f"zip://{zip_path}"
    elif shp_path.exists():
        path = str(shp_path)
    else:
        return None

    gdf = gpd.read_file(path)
    geometries: dict[str, object] = {}
    names: dict[str, str] = {}
    for _, row in gdf.iterrows():
        iso = str(row.get("ISO_A2", "") or "").strip().lower()
        if not iso or iso == "-99":
            iso = str(row.get("ISO_A2_EH", "") or "").strip().lower()
        if iso and iso != "-99":
            existing = geometries.get(iso)
            geometries[iso] = (
                unary_union([existing, row.geometry]) if existing is not None else row.geometry
            )
            names.setdefault(iso, str(row.get("NAME", "") or iso.upper()))

    return {
        iso: {
            "geometry": geom,
            "bounds": mainland_bounds(geom),
            "name": names[iso],
        }
        for iso, geom in geometries.items()
    }


def mainland_bounds(geometry) -> tuple[float, float, float, float]:
    """Bounds of the mainland: the largest sub-polygon plus parts near it
    (`services.overlap.mainland`), not the full multi-part geometry.

    Several countries' Natural Earth polygon includes distant overseas
    territories — e.g. France includes Clipperton Island (Pacific, near
    Mexico) and the Netherlands includes Aruba/Curaçao (Caribbean) — which
    would otherwise blow the naive bounding box out to span most of the
    globe, while nearby islands (Corsica, Sicily, the Balearics) must stay in
    so their SRTM tiles are downloaded. bbox_json/srtm_bbox_json should
    describe the land the OSM/SRTM/tiles stages actually need; the full
    geometry (all territories) is still what's written to
    library/country-geometry/<iso2>.geojson for the map UI.
    """
    return overlap.mainland(geometry).bounds


def srtm_bbox_from_bounds(bounds: tuple[float, float, float, float]) -> list:
    """Rounds Natural Earth bounds outward to whole degrees, reshaped into
    the [min_lat, max_lat, min_lon, max_lon] list format
    pipeline/config/region.py::Region.srtm uses today.
    """
    min_lon, min_lat, max_lon, max_lat = bounds
    return [
        math.floor(min_lat),
        math.ceil(max_lat),
        math.floor(min_lon),
        math.ceil(max_lon),
    ]
