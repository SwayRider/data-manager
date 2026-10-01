"""Carving: keep only a drawn part of a country (per configuration)."""

import json

from shapely.geometry import mapping, shape
from sqlalchemy import select
from sqlalchemy.orm import Session

from datamanager.config import config
from datamanager.errors import ValidationError
from datamanager.models import Country, CountryCarve
from datamanager.services import overlap


def carve_map(session: Session, config_id: int) -> dict[str, str]:
    """iso2 -> canonical JSON of the keep polygon (hashable, part of the geometry cache key)."""
    rows = session.scalars(select(CountryCarve).where(CountryCarve.config_profile_id == config_id))
    return {r.country_iso: json.dumps(r.keep_geojson, sort_keys=True) for r in rows}


def _clean(geometry) -> dict:
    try:
        geom = shape(geometry)
    except Exception:
        raise ValidationError("Not a valid polygon", field="geometry") from None
    if geom.geom_type not in ("Polygon", "MultiPolygon"):
        raise ValidationError("Draw a polygon", field="geometry")
    if not geom.is_valid:
        geom = geom.buffer(0)
    if geom.is_empty or geom.area == 0:
        raise ValidationError("The polygon is empty", field="geometry")
    return json.loads(json.dumps(mapping(geom)))


def set_carve(session: Session, config_id: int, iso2: str, geometry) -> None:
    from datamanager.services import regions as region_service

    iso2 = iso2.lower()
    country = session.get(Country, iso2)
    if country is None:
        raise ValidationError("Unknown country", field="country")
    keep = _clean(geometry)
    key = overlap.geometry_key(iso2, country.ne_geometry_ref)
    if key is None:
        raise ValidationError("Country has no geometry", field="country")
    if not overlap.raw_geometry(key).intersects(shape(keep)):
        raise ValidationError("The polygon does not cover any part of this country", field="geometry")
    row = session.scalar(
        select(CountryCarve).where(CountryCarve.config_profile_id == config_id, CountryCarve.country_iso == iso2)
    )
    if row is None:
        session.add(CountryCarve(config_profile_id=config_id, country_iso=iso2, keep_geojson=keep))
    else:
        row.keep_geojson = keep
    session.commit()
    region_service.evaluate_config_overlap(session, config_id)


def clear_carve(session: Session, config_id: int, iso2: str) -> None:
    from datamanager.services import regions as region_service

    row = session.scalar(
        select(CountryCarve).where(CountryCarve.config_profile_id == config_id, CountryCarve.country_iso == iso2.lower())
    )
    if row is not None:
        session.delete(row)
        session.commit()
        region_service.evaluate_config_overlap(session, config_id)


def kept_geojson(session: Session, config_id: int) -> dict[str, dict]:
    """iso2 -> the kept part of each carved country as display GeoJSON (clipped to the country)."""
    countries = {c.iso2: c for c in session.scalars(select(Country))}
    result = {}
    for iso, carve_json in carve_map(session, config_id).items():
        if iso not in countries:
            continue
        key = overlap.geometry_key(iso, countries[iso].ne_geometry_ref, carve_json)
        if key is not None:
            result[iso] = overlap.display_geojson(overlap.geometry_of(key))
    return result
