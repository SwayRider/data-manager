"""Country catalog seed pipeline: seeds every Natural Earth country (geometry,
bbox, srtm bbox) and overlays the hand-curated countries.yml
(geofabrik_path/wof_code/openaddresses_files) where available, and creates
default address-source rows (OpenAddresses from the yml, Overture where covered) and
boundary-source rows (OSM locality levels from the yml; everything else off). Countries
without curated data get geofabrik_path=None and are not yet buildable.
Idempotent — safe to re-run as Natural Earth or the curated file changes.

The DB is the source of truth for curated fields: they are only applied on
create, or to a still-unconfigured country (geofabrik_path is None). Values
edited in the UI are never overwritten by a re-seed.
"""

import json
import logging
from pathlib import Path

import yaml
from sqlalchemy.orm import Session

from datamanager.address_sources import SOURCES
from datamanager.config import config
from datamanager.countries.natural_earth import (
    ensure_natural_earth,
    load_ne_countries,
    srtm_bbox_from_bounds,
)
from datamanager.errors import ValidationError
from datamanager.services import address_sources as address_source_service
from datamanager.services import boundary_sources as boundary_source_service
from datamanager.models.country import Country

logger = logging.getLogger(__name__)

CURATED_PATH = Path(__file__).parent / "curated" / "countries.yml"


def load_curated() -> dict[str, dict]:
    return yaml.safe_load(CURATED_PATH.read_text()) or {}


def _write_geometry_geojson(iso2: str, geometry) -> str:
    library_dir = config.library_dir / "country-geometry"
    library_dir.mkdir(parents=True, exist_ok=True)
    path = library_dir / f"{iso2}.geojson"
    path.write_text(json.dumps(geometry.__geo_interface__))
    return str(path.relative_to(Path(config.DATA_ROOT)))


def seed_countries(session: Session) -> list[str]:
    """Returns the list of iso2 codes that were created or updated."""
    ensure_natural_earth(config.NATURAL_EARTH_URL, config.NATURAL_EARTH_DIR)
    ne_countries = load_ne_countries(config.NATURAL_EARTH_DIR)
    if ne_countries is None:
        raise ValidationError("Natural Earth countries archive could not be loaded")

    curated = load_curated()
    unknown = sorted(set(curated) - set(ne_countries))
    if unknown:
        raise ValidationError(
            f"curated countries not found in Natural Earth data: {', '.join(unknown)}",
            iso2=unknown,
        )
    changed: list[str] = []

    for iso2, ne_entry in sorted(ne_countries.items()):
        curated_entry = curated.get(iso2, {})

        bbox = list(ne_entry["bounds"])
        srtm_bbox = srtm_bbox_from_bounds(ne_entry["bounds"])
        geometry_ref = _write_geometry_geojson(iso2, ne_entry["geometry"])

        existing = session.get(Country, iso2)
        derived = {
            "name": ne_entry["name"],
            "ne_geometry_ref": geometry_ref,
            "bbox_json": bbox,
            "srtm_bbox_json": srtm_bbox,
        }
        curated_values = {
            "geofabrik_path": curated_entry.get("geofabrik_path"),
            "wof_code": curated_entry.get("wof_code", iso2),
        }

        if existing is None:
            country = Country(iso2=iso2, **derived, **curated_values)
            address_source_service.ensure_defaults(country, curated_entry)
            boundary_source_service.ensure_defaults(country, curated_entry)
            session.add(country)
            changed.append(iso2)
            logger.info("created country", extra={"extra_fields": {"iso2": iso2}})
            continue

        unconfigured = existing.geofabrik_path is None
        new_values = dict(derived)
        if unconfigured:  # not configured yet: yml may fill it in
            new_values.update(curated_values)
        diffs = {k: v for k, v in new_values.items() if getattr(existing, k) != v}
        created_rows = address_source_service.ensure_defaults(existing, curated_entry)
        created_rows |= boundary_source_service.ensure_defaults(existing, curated_entry)
        if unconfigured and not address_source_service.openaddresses_files(existing):
            # unconfigured country: the yml may supply OpenAddresses files for an empty row
            files = SOURCES["openaddresses"].default(iso2, curated_entry).config["files"]
            if files:
                address_source_service.set_openaddresses_files(existing, files)
                created_rows = True
        if diffs or created_rows:
            for k, v in diffs.items():
                setattr(existing, k, v)
            changed.append(iso2)
            logger.info(
                "updated country",
                extra={"extra_fields": {"iso2": iso2, "changed_fields": list(diffs)}},
            )

    session.commit()
    return changed
