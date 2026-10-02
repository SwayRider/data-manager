"""What the Pelias stage needs downloaded for a resolved configuration, as versioned `source_key`s.

    placeholder:store            the pinned Placeholder store (`download.placeholder`)
    geonames:<cc>                the country's GeoNames dump <CC>.zip (`download.geonames`), one per buildable country
    geonames-postal:<cc>         postal-code file of a country whose boundary source "GeoNames postal" is on
    wof:admin-<cc>               Who's On First admin bundle of every buildable country (core and overlap)
    wof:postalcode-<cc>          the same for postal codes (the legacy load imports them)
    official:<iso2>              official locality polygons of a country with that boundary source
    openaddresses:<source>       one per OpenAddresses source of the resolved regions (per-region exclusions applied)

Only the plan lives here; the fetching is `stages/download_pelias.py`. WOF bundle URLs come from the distribution's
`inventory.json`, so they are resolved at run time (`wof_bundles`)."""
import hashlib
import json
import re
from dataclasses import dataclass

import requests

from datamanager.boundary_sources.geonames_postal import has_postal_file
from datamanager.models import Country
from datamanager.services import boundary_sources as boundary_source_service

PLACEHOLDER_KEY = "placeholder:store"
KINDS = ("placeholder", "geonames", "geonames-postal", "wof", "official", "openaddresses")


@dataclass(frozen=True)
class Item:
    key: str
    kind: str  # one of KINDS
    label: str
    url: str | None = None  # None: resolved at run time (WOF bundles, OpenAddresses jobs)
    country: str = ""


def _buildable(resolved: dict) -> dict[str, dict]:
    """iso2 -> resolved country, over core and overlap of every region, for countries with a Geofabrik path."""
    found: dict[str, dict] = {}
    for region in resolved.get("regions", []):
        for country in [*region["core"], *region["overlap"]]:
            if country.get("geofabrik_path"):
                found.setdefault(country["iso2"], country)
    return found


def openaddresses_sources(resolved: dict) -> list[str]:
    seen: dict[str, None] = {}
    for region in resolved.get("regions", []):
        for country in [*region["core"], *region["overlap"]]:
            if country.get("geofabrik_path"):
                for source in country.get("openaddresses", []):
                    seen.setdefault(source)
    return sorted(seen)


def geonames_code(iso2: str) -> str:
    """The GeoNames country file name: the upper-case ISO2 code (`be` -> `BE`; a region-style `xx-yy` uses its last part)."""
    return iso2.split("-")[-1].upper()


def wof_code(country: dict) -> str:
    return str(country["wof_code"]).lower()


def plan(session, resolved: dict, base: dict[str, str]) -> list[Item]:
    """Every download of the configuration. `base` = {geonames, placeholder} URLs of the settings."""
    countries = _buildable(resolved)
    if not countries:
        return []
    items = [Item(PLACEHOLDER_KEY, "placeholder", "Placeholder store", base["placeholder"])]
    for iso2, country in sorted(countries.items()):
        code = wof_code(country)
        cc = geonames_code(iso2)
        items.append(Item(f"geonames:{cc.lower()}", "geonames", f"GeoNames {cc}", f"{base['geonames'].rstrip('/')}/dump/{cc}.zip", iso2))
        items.append(Item(f"wof:admin-{code}", "wof", f"WOF admin {code.upper()}", country=iso2))
        items.append(Item(f"wof:postalcode-{code}", "wof", f"WOF postal codes {code.upper()}", country=iso2))
        row = session.get(Country, iso2)
        if row is None:
            continue
        if boundary_source_service.get_state(row, "geonames_postal").enabled and has_postal_file(iso2):
            items.append(Item(f"geonames-postal:{cc.lower()}", "geonames-postal", f"GeoNames postal {cc}",
                              f"{base['geonames'].rstrip('/')}/zip/{cc}.zip", iso2))
        official = boundary_source_service.get_state(row, "official_polygons")
        if official.enabled and official.config.get("url"):
            items.append(Item(f"official:{iso2.lower()}", "official", f"Official polygons {iso2.upper()}", official.config["url"], iso2))
    items += [Item(f"openaddresses:{s}", "openaddresses", f"OpenAddresses {s}") for s in openaddresses_sources(resolved)]
    return items


def fingerprint(items: list[Item]) -> str:
    """Which sources the configuration needs (not their versions): the status compares it with the last approved run."""
    return hashlib.sha256(json.dumps(sorted((i.key, i.url or "") for i in items)).encode()).hexdigest()[:16]


def wof_bundles(base: str, wanted: set[str], timeout: float = 60) -> dict[str, str]:
    """{`wof:admin-be`: url, ...} for the wanted keys found in the WOF `sqlite/inventory.json`, taking the newest entry
    per file name and preferring `.db.bz2` over `.db.tar.bz2`, as the importer's own downloader does."""
    root = base.rstrip("/")
    response = requests.get(f"{root}/sqlite/inventory.json", timeout=timeout)
    response.raise_for_status()
    best: dict[str, dict] = {}
    for entry in response.json():
        compressed = entry.get("name_compressed", "")
        found = re.fullmatch(r"whosonfirst-data-(admin|postalcode)-([a-z]{2})-latest\.db(\.tar)?\.bz2", compressed)
        if not found:
            continue
        key = f"wof:{found.group(1)}-{found.group(2)}"
        if key not in wanted:
            continue
        rank = (0 if not found.group(3) else 1, entry.get("last_modified", ""))
        current = best.get(key)
        if current is None or rank[0] < current["rank"][0] or (rank[0] == current["rank"][0] and rank[1] > current["rank"][1]):
            best[key] = {"rank": rank, "url": f"{root}/sqlite/{compressed}"}
    return {k: v["url"] for k, v in best.items()}
