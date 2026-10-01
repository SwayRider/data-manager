"""Autofill unconfigured countries' Geofabrik path and OpenAddresses sources.

Both providers publish an index we can match deterministically:
  * Geofabrik `index-v1-nogeom.json`: every extract with its ISO 3166-1 codes.
  * OpenAddresses batch API `/api/data`: every source with its level
    (country / state / county / city ...).

The DB is the source of truth: only *unset* fields are filled (geofabrik_path
None, OpenAddresses list empty), and every pick is verified live with the same
checks the edit modal uses before it is saved.
"""

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

import requests
from sqlalchemy import select
from sqlalchemy.orm import Session

from datamanager.config import config
from datamanager.models import Country
from datamanager.services import address_sources as address_source_service
from datamanager.services import geofabrik, openaddresses

logger = logging.getLogger(__name__)

GEOFABRIK_INDEX_URL = f"{geofabrik.BASE_URL}/index-v1-nogeom.json"
OPENADDRESSES_INDEX_URL = f"{openaddresses.BATCH_HOST}/api/data"
STATE_LEVELS = {"state", "region"}


@dataclass
class OpenAddressesPick:
    sources: list[str] = field(default_factory=list)
    level: str = "none"  # country | country-multi | state | none
    available: dict[str, int] = field(default_factory=dict)  # level -> count, for the report


@dataclass
class CountryResult:
    iso2: str
    geofabrik_path: str | None = None
    geofabrik_shared_with: list[str] = field(default_factory=list)
    geofabrik_error: str | None = None
    openaddresses: OpenAddressesPick = field(default_factory=OpenAddressesPick)
    openaddresses_error: str | None = None
    changed: bool = False


def _cached_json(name: str, url: str, params: dict | None = None, refresh: bool = False):
    cache = Path(config.DATA_ROOT) / "downloads" / "autofill" / name
    if cache.is_file() and not refresh:
        return json.loads(cache.read_text())
    response = requests.get(url, params=params, timeout=60)
    response.raise_for_status()
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(response.text)
    return response.json()


def lookup_code(iso2: str) -> str:
    """Natural Earth sometimes keys a country like 'cn-tw'; providers use the last part."""
    return iso2.split("-")[-1]


def geofabrik_paths(index: dict) -> dict[str, tuple[str, list[str]]]:
    """iso2 (lowercase) -> (extract path, other countries sharing that extract).

    Prefers the extract covering the fewest countries, then the shallowest path.
    """
    base = f"{geofabrik.BASE_URL}/"
    candidates: dict[str, list[tuple[int, int, str, list[str]]]] = {}
    for feature in index["features"]:
        props = feature["properties"]
        codes = [c.lower() for c in props.get("iso3166-1:alpha2") or []]
        pbf = props.get("urls", {}).get("pbf", "")
        if not codes or not pbf.startswith(base) or not pbf.endswith("-latest.osm.pbf"):
            continue
        path = pbf[len(base): -len("-latest.osm.pbf")]
        for code in codes:
            candidates.setdefault(code, []).append(
                (len(codes), path.count("/"), path, [c for c in codes if c != code])
            )
    return {code: (best[2], best[3]) for code, options in candidates.items() for best in [min(options)]}


def openaddresses_picks(index: list[dict]) -> dict[str, OpenAddressesPick]:
    """Country code -> chosen sources.

    countrywide source if present; else any country-level sources; else all
    state-level sources (only `statewide` ones if the country has any); else nothing (only county/city data is available).
    """
    by_country: dict[str, list[dict]] = {}
    for entry in index:
        if entry.get("job"):
            by_country.setdefault(entry["source"].split("/")[0], []).append(entry)

    picks = {}
    for code, entries in by_country.items():
        available: dict[str, int] = {}
        for e in entries:
            available[e["name"]] = available.get(e["name"], 0) + 1
        sources = sorted(e["source"] for e in entries)
        countrywide = f"{code}/countrywide"
        country_level = sorted(e["source"] for e in entries if e["name"] == "country")
        state_level = sorted(e["source"] for e in entries if e["name"] in STATE_LEVELS)
        # OpenAddresses sometimes labels city/county sources "state"; a real
        # statewide source has the basename "statewide", so prefer those.
        state_level = [s for s in state_level if s.rsplit("/", 1)[-1] == "statewide"] or state_level
        if countrywide in sources:
            pick = OpenAddressesPick([countrywide], "country")
        elif country_level:
            pick = OpenAddressesPick(country_level, "country-multi")
        elif state_level:
            pick = OpenAddressesPick(state_level, "state")
        else:
            pick = OpenAddressesPick()
        pick.available = available
        picks[code] = pick
    return picks


def load_indexes(refresh: bool = False):
    """(geofabrik iso->extract map, openaddresses country->pick map), cached under downloads/autofill/."""
    gf = geofabrik_paths(_cached_json("geofabrik-index.json", GEOFABRIK_INDEX_URL, refresh=refresh))
    oa = openaddresses_picks(
        _cached_json("openaddresses-index.json", OPENADDRESSES_INDEX_URL,
                     params={"layer": "addresses", "validated": "false"}, refresh=refresh)
    )
    return gf, oa


def autofill_countries(session: Session, verify: bool = True, refresh: bool = False) -> list[CountryResult]:
    """Fill unset fields on all countries; returns one result per country."""
    gf, oa = load_indexes(refresh)

    results = []
    for country in session.scalars(select(Country).order_by(Country.iso2)):
        code = lookup_code(country.iso2)
        result = CountryResult(iso2=country.iso2)

        if country.geofabrik_path is None:
            match = gf.get(code)
            if match is None:
                result.geofabrik_error = "no Geofabrik extract lists this country"
            else:
                path, shared = match
                result.geofabrik_shared_with = shared
                try:
                    if verify and not geofabrik.extract_exists(path):
                        result.geofabrik_error = f"{path}: not found on Geofabrik"
                except requests.RequestException as exc:
                    result.geofabrik_error = f"{path}: could not verify ({exc.__class__.__name__})"
                if result.geofabrik_error is None:
                    country.geofabrik_path = path
                    result.changed = True

        if not address_source_service.openaddresses_files(country):
            pick = oa.get(code, OpenAddressesPick())
            result.openaddresses = pick
            if pick.sources:
                try:
                    missing = openaddresses.find_missing(pick.sources) if verify else []
                except requests.RequestException as exc:
                    result.openaddresses_error = f"could not verify ({exc.__class__.__name__})"
                else:
                    if missing:
                        result.openaddresses_error = f"not found: {', '.join(missing)}"
                    else:
                        address_source_service.set_openaddresses_files(country, pick.sources)
                        result.changed = True

        results.append(result)
        logger.info("autofilled country", extra={"extra_fields": {"iso2": country.iso2, "changed": result.changed}})

    session.commit()
    return results
