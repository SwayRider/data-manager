"""Render the country catalog as a Markdown review sheet (countries.md)."""

import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from datamanager.countries.autofill import OpenAddressesPick, lookup_code, load_indexes
from datamanager.address_sources import SOURCES
from datamanager.models import Country
from datamanager.boundary_sources import SOURCES as BOUNDARY_SOURCES
from datamanager.services import address_sources as address_source_service
from datamanager.services import boundary_sources as boundary_source_service
from datamanager.services import geofabrik


def _cell(text: str) -> str:
    return text.replace("|", "\\|")


def _boundaries(country: Country) -> str:
    parts = [
        source.describe(boundary_source_service.get_state(country, key)) for key, source in BOUNDARY_SOURCES.items()
    ]
    return " + ".join(p for p in parts if p != "—") or "—"


def _row(country: Country, gf: dict, oa: dict[str, OpenAddressesPick]) -> tuple[str, list[str]]:
    code = lookup_code(country.iso2)
    notes: list[str] = []

    if country.geofabrik_path:
        geo = f"[{country.geofabrik_path}]({geofabrik.pbf_url(country.geofabrik_path)})"
        shared = gf.get(code, (None, []))[1] if gf.get(code, (None,))[0] == country.geofabrik_path else []
        if shared:
            notes.append(f"Geofabrik extract shared with {', '.join(c.upper() for c in shared)}")
    else:
        geo = "—"
        notes.append("**Geofabrik: manual**")

    files = address_source_service.openaddresses_files(country)
    pick = oa.get(code, OpenAddressesPick())
    if files:
        oa_cell = "<br>".join(f"`{f}`" for f in files)
        if files == pick.sources and pick.level in ("state", "country-multi"):
            notes.append(
                f"OpenAddresses: {pick.level.replace('-multi', '')}-level, "
                f"{len(files)} source{'s' if len(files) != 1 else ''} — review"
            )
    else:
        oa_cell = "—"
        detail = ", ".join(f"{n} {level}" for level, n in sorted(pick.available.items(), key=lambda kv: -kv[1]))
        notes.append(
            "OpenAddresses: none" + (f" (only {detail} sources available)" if detail else " (nothing published)")
        )
    overture = SOURCES["overture"].describe(address_source_service.get_state(country, "overture"))
    return (
        f"| {country.iso2.upper()} | {_cell(country.name)} | {geo} | {oa_cell} | {overture} | {_boundaries(country)} "
        f"| {'; '.join(notes)} |",
        notes,
    )


def render_countries_md(session: Session) -> str:
    gf, oa = load_indexes()
    countries = sorted(session.scalars(select(Country)), key=lambda c: c.name.casefold())
    rows = [_row(c, gf, oa) for c in countries]

    no_geo = sum(1 for c in countries if not c.geofabrik_path)
    no_oa = sum(1 for c in countries if not address_source_service.openaddresses_files(c))
    both = sum(1 for c in countries if c.geofabrik_path and address_source_service.openaddresses_files(c))
    with_boundaries = sum(1 for c in countries if _boundaries(c) != "—")
    with_overture = sum(
        1 for c in countries if address_source_service.get_state(c, "overture").enabled
    )

    lines = [
        "# Countries",
        "",
        f"_Generated {datetime.date.today().isoformat()} by `flask export-countries`. "
        "Do not edit by hand — edit countries on the Configure map (right-click) instead._",
        "",
        f"- **{len(countries)}** countries · **{both}** with both Geofabrik and OpenAddresses",
        f"- **{no_geo}** without a Geofabrik path (cannot be built until set manually)",
        f"- **{no_oa}** without OpenAddresses sources (OSM addresses only, unless Overture is on)",
        f"- **{with_overture}** with Overture addresses enabled",
        f"- **{with_boundaries}** with a locality-boundary source (others use Who's On First unchanged)",
        "",
        "OpenAddresses sources are stored without file extension, as the Pelias importer expects. "
        "OpenStreetMap addresses come from the Geofabrik extract for every country with a Geofabrik path.",
        "",
        "| ISO | Country | Geofabrik | OpenAddresses | Overture | Boundaries | Notes |",
        "|---|---|---|---|---|---|---|",
        *(row for row, _ in rows),
        "",
    ]
    return "\n".join(lines)


def write_countries_md(session: Session, path: Path) -> Path:
    path.write_text(render_countries_md(session))
    return path
