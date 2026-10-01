import json
import re
from functools import lru_cache
from pathlib import Path

import requests

from shapely.geometry import mapping, shape
from sqlalchemy import select
from sqlalchemy.orm import Session

from datamanager.config import config
from datamanager.errors import ValidationError
from datamanager.models import Country
from datamanager.address_sources import SOURCES, SourceState
from datamanager.boundary_sources import SOURCES as BOUNDARY_SOURCES
from datamanager.services import boundary_sources as boundary_source_service
from datamanager.services import address_sources as address_source_service
from datamanager.services import geofabrik

WOF_CODE_RE = re.compile(r"^[a-z0-9-]{1,10}$")

# Display-only simplification (degrees, ~1 km); the stored geometry stays full detail.
SIMPLIFY_TOLERANCE = 0.01
COORD_PRECISION = 3


@lru_cache(maxsize=512)
def _display_geometry(path: str, mtime_ns: int) -> dict:
    geometry = shape(json.loads(Path(path).read_text()))
    simplified = geometry.simplify(SIMPLIFY_TOLERANCE, preserve_topology=True)
    return json.loads(json.dumps(mapping(simplified)))  # tuples -> lists


def _round_coords(obj):
    if isinstance(obj, (list, tuple)):
        return [_round_coords(o) for o in obj]
    if isinstance(obj, float):
        return round(obj, COORD_PRECISION)
    return obj


def countries_feature_collection(session: Session) -> dict:
    """GeoJSON FeatureCollection of all catalog countries, simplified for map display
    (skips countries whose geometry file is missing)."""
    features = []
    for country in session.scalars(select(Country).order_by(Country.iso2)):
        path = Path(config.DATA_ROOT) / country.ne_geometry_ref
        if not path.is_file():
            continue
        geometry = _display_geometry(str(path), path.stat().st_mtime_ns)
        features.append(
            {
                "type": "Feature",
                "id": country.iso2,
                "properties": {
                    "iso2": country.iso2,
                    "name": country.name,
                    "curated": country.is_curated,
                },
                "geometry": {**geometry, "coordinates": _round_coords(geometry["coordinates"])},
            }
        )
    return {"type": "FeatureCollection", "features": features}


def get_country(session: Session, iso2: str) -> Country | None:
    return session.get(Country, iso2)


def update_country(
    session: Session,
    country: Country,
    geofabrik_path: str,
    wof_code: str,
    sources: dict[str, SourceState],
    boundaries: dict[str, SourceState] | None = None,
    verify_url: bool = True,
) -> Country:
    """Edit a country's curated build data and address sources. A blank Geofabrik path un-configures it."""
    path = (geofabrik_path or "").strip().strip("/")
    if path:
        if not geofabrik.is_valid_path(path):
            raise ValidationError(
                "Geofabrik path must look like 'europe/belgium' (lowercase, hyphens, slashes)",
                field="geofabrik_path",
            )
        if verify_url:
            try:
                exists = geofabrik.extract_exists(path)
            except requests.RequestException:
                raise ValidationError(
                    "Could not reach Geofabrik to verify the URL (tick 'Skip URL check' to save anyway)",
                    field="geofabrik_path",
                )
            if not exists:
                raise ValidationError(
                    f"Not found on Geofabrik: {geofabrik.poly_url(path)}", field="geofabrik_path"
                )

    wof = (wof_code or "").strip().lower() or country.iso2
    if not WOF_CODE_RE.match(wof):
        raise ValidationError("WOF code must be 1-10 characters: a-z, 0-9, '-'", field="wof_code")

    validated = {
        key: source.validate(sources.get(key, address_source_service.get_state(country, key)), verify_url)
        for key, source in SOURCES.items()
    }
    validated_boundaries = {
        key: source.validate(
            (boundaries or {}).get(key, boundary_source_service.get_state(country, key)), verify_url
        )
        for key, source in BOUNDARY_SOURCES.items()
    }

    country.geofabrik_path = path or None
    country.wof_code = wof
    for key, state in validated.items():
        address_source_service.set_state(country, key, state)
    for key, state in validated_boundaries.items():
        boundary_source_service.set_state(country, key, state)
    session.commit()
    return country
