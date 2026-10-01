import json

from flask import Blueprint, Response, abort, jsonify, render_template, request

from datamanager.db import SessionLocal
from datamanager.errors import ValidationError
from datamanager.models import Country
from datamanager.address_sources import SOURCES
from datamanager.boundary_sources import SOURCES as BOUNDARY_SOURCES
from datamanager.boundary_sources import geonames_postal
from datamanager.services import boundary_sources as boundary_source_service
from datamanager.services import address_sources as address_source_service
from datamanager.services import countries as service
from datamanager.address_sources import overture
from datamanager.services import geofabrik

bp = Blueprint("countries", __name__, url_prefix="/countries", template_folder="templates")


def _get_country_or_404(iso2: str) -> Country:
    country = service.get_country(SessionLocal(), iso2.lower())
    if country is None:
        abort(404)
    return country


def _render_form(country: Country, values: dict, error: str | None = None, field: str | None = None):
    html = render_template(
        "countries/_details.html",
        country=country,
        values=values,
        sources=list(SOURCES.values()),
        boundary_sources=list(BOUNDARY_SOURCES.values()),
        geonames_has_postal=geonames_postal.has_postal_file(country.iso2),
        overture_covered=overture.is_covered(country.iso2),
        error=error,
        error_field=field,
        pbf_url=geofabrik.pbf_url(country.geofabrik_path) if country.geofabrik_path else None,
    )
    return html, (422 if error else 200)


def _values(country: Country) -> dict:
    values = {"geofabrik_path": country.geofabrik_path or "", "wof_code": country.wof_code}
    for key, source in SOURCES.items():
        values.update(source.form_values(address_source_service.get_state(country, key)))
    for key, source in BOUNDARY_SOURCES.items():
        values.update(source.form_values(boundary_source_service.get_state(country, key)))
    return values


@bp.get("/api/geojson")
def geojson():
    return jsonify(service.countries_feature_collection(SessionLocal()))


@bp.get("/<iso2>")
def details(iso2: str):
    country = _get_country_or_404(iso2)
    return _render_form(country, _values(country))[0]


@bp.post("/<iso2>")
def save(iso2: str):
    country = _get_country_or_404(iso2)
    sources = {key: source.from_form(request.form) for key, source in SOURCES.items()}
    values = {"geofabrik_path": request.form.get("geofabrik_path", ""), "wof_code": request.form.get("wof_code", "")}
    for key, source in SOURCES.items():
        values.update(source.form_values(sources[key]))
    boundaries = {key: source.from_form(request.form) for key, source in BOUNDARY_SOURCES.items()}
    for key, source in BOUNDARY_SOURCES.items():
        values.update(source.form_values(boundaries[key]))
    try:
        service.update_country(
            SessionLocal(),
            country,
            geofabrik_path=values["geofabrik_path"],
            wof_code=values["wof_code"],
            sources=sources,
            boundaries=boundaries,
            verify_url=not request.form.get("skip_verify"),
        )
    except ValidationError as exc:
        return _render_form(country, values, exc.message, exc.details.get("field"))
    # Empty body clears the modal; the event lets the map restyle the country.
    event = {"country-updated": {"iso2": country.iso2, "name": country.name, "curated": country.is_curated}}
    return Response("", headers={"HX-Trigger": json.dumps(event)})
