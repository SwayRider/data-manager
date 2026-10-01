"""Resolve a configuration into the exact per-region lists the build stages consume.

Pure derivation over regions -> countries (core + stored overlap) -> catalog data; the shape
mirrors data-pipeline's config-mini.yml (`Region.core_*`/`overlap_*`, `srtm`,
`border-regions`). The per-region hash covers everything a build depends on (not colors or
names of other regions), so identical config gives an identical hash.
"""

import hashlib
import json
from dataclasses import asdict, dataclass, field

from sqlalchemy.orm import Session

from datamanager.address_sources import SOURCES
from datamanager.boundary_sources import SOURCES as BOUNDARY_SOURCES
from datamanager.models import Country, Region
from datamanager.services import address_sources as address_source_service
from datamanager.services import boundary_sources as boundary_source_service
from datamanager.services import map_styles as style_service
from datamanager.services import regions as region_service


@dataclass
class ResolvedCountry:
    iso2: str
    name: str
    geofabrik_path: str | None
    wof_code: str
    openaddresses: list[str]  # after per-region exclusions
    openaddresses_all: list[str]  # everything the catalog knows
    overture: bool
    boundaries: str
    srtm_bbox: list[int]
    carve: dict | None = None  # keep-polygon (GeoJSON) if the country is carved; stages must clip to it

    @property
    def srtm_name(self) -> str:
        return (self.geofabrik_path or self.iso2).rsplit("/", 1)[-1]


@dataclass
class ResolvedRegion:
    region_id: int
    name: str
    color: str
    core: list[ResolvedCountry]
    overlap: list[ResolvedCountry]
    srtm: dict[str, list[int]] = field(default_factory=dict)
    srtm_tiles: int = 0
    gtfs_feeds: list[dict] = field(default_factory=list)  # [{id, url, label}] in download order
    warnings: list[str] = field(default_factory=list)
    hash: str = ""


@dataclass
class ResolvedConfig:
    regions: list[ResolvedRegion]
    border_regions: list[list[str]]
    style: dict = field(default_factory=dict)  # light_style, dark_style, labels (overrides), hash


def _country(country: Country, excluded: set[str], carve: dict | None = None) -> ResolvedCountry:
    all_files = address_source_service.openaddresses_files(country)
    return ResolvedCountry(
        iso2=country.iso2,
        name=country.name,
        geofabrik_path=country.geofabrik_path,
        wof_code=country.wof_code,
        openaddresses=[f for f in all_files if f not in excluded],
        openaddresses_all=all_files,
        overture=address_source_service.get_state(country, "overture").enabled,
        boundaries=" + ".join(
            d
            for key, source in BOUNDARY_SOURCES.items()
            if (d := source.describe(boundary_source_service.get_state(country, key))) != "—"
        )
        or "—",
        srtm_bbox=list(country.srtm_bbox_json),
        carve=carve,
    )


def count_srtm_tiles(boxes) -> int:
    """Distinct 1° tiles over [min_lat, max_lat, min_lon, max_lon] boxes (inclusive, as
    data-pipeline's Region._tile_list_for_box)."""
    tiles = set()
    for min_lat, max_lat, min_lon, max_lon in boxes:
        tiles.update((lat, lon) for lat in range(min_lat, max_lat + 1) for lon in range(min_lon, max_lon + 1))
    return len(tiles)


def _content(resolved: ResolvedRegion) -> dict:
    def part(countries):
        return [
            {
                "iso2": c.iso2,
                "geofabrik_path": c.geofabrik_path,
                "wof_code": c.wof_code,
                "openaddresses": c.openaddresses,
                "overture": c.overture,
                "boundaries": c.boundaries,
                "carve": c.carve,
            }
            for c in countries
        ]

    content = {"core": part(resolved.core), "overlap": part(resolved.overlap), "srtm": resolved.srtm}
    if resolved.gtfs_feeds:  # only when set, so regions without feeds keep their hash
        content["gtfs"] = sorted(f["url"] for f in resolved.gtfs_feeds)
    return content


def _hash(resolved: ResolvedRegion) -> str:
    return hashlib.sha256(json.dumps(_content(resolved), sort_keys=True).encode()).hexdigest()


def resolve_region(session: Session, region: Region, countries: dict[str, Country]) -> ResolvedRegion:
    from datamanager.models import CountryCarve

    carves = {
        c.country_iso: c.keep_geojson
        for c in session.query(CountryCarve).filter_by(config_profile_id=region.config_profile_id)
    }
    excluded: dict[str, set[str]] = {}
    for row in region.openaddresses_exclusions:
        excluded.setdefault(row.country_iso, set()).add(row.file)

    def build(isos):
        return [_country(countries[iso], excluded.get(iso, set()), carves.get(iso)) for iso in sorted(isos) if iso in countries]

    core = build(link.country_iso for link in region.countries)
    overlap = build(region_service.effective_overlap(region))
    resolved = ResolvedRegion(region_id=region.id, name=region.name, color=region.color, core=core, overlap=overlap,
        gtfs_feeds=[{"id": f.id, "url": f.url, "label": f.label} for f in region.gtfs_feeds],
    )

    buildable = [c for c in core + overlap if c.geofabrik_path]
    resolved.srtm = {c.srtm_name: c.srtm_bbox for c in buildable}
    resolved.srtm_tiles = count_srtm_tiles(resolved.srtm.values())

    if not core:
        resolved.warnings.append("No core countries")
    for c in overlap:
        if not c.geofabrik_path:
            resolved.warnings.append(f"Overlap country {c.name} is not configured (no Geofabrik path): skipped")
    for c in core + overlap:
        if c.geofabrik_path and not c.openaddresses and not c.overture:
            resolved.warnings.append(f"{c.name} has no address source besides OSM")
    resolved.hash = _hash(resolved)
    return resolved


def resolve_config(session: Session, config_id: int) -> ResolvedConfig:
    countries = {c.iso2: c for c in session.query(Country)}
    regions = region_service.list_regions(session, config_id)
    borders = region_service.overlap_summary(session, config_id)["borders"]
    return ResolvedConfig(
        regions=[resolve_region(session, r, countries) for r in regions],
        border_regions=borders,
        style=resolve_style(session, config_id),
    )


def resolve_style(session: Session, config_id: int) -> dict:
    settings = style_service.get_settings(session, config_id)
    digest = hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest()
    return {**settings, "hash": digest}


def to_dict(resolved: ResolvedConfig) -> dict:
    return asdict(resolved)


def to_legacy_dict(resolved: ResolvedConfig) -> dict:
    """The config-mini.yml shape. OpenAddresses entries get `.csv` back (the catalog stores
    them without extension)."""

    def block(countries):
        buildable = [c for c in countries if c.geofabrik_path]
        return {
            "osm": sorted(c.geofabrik_path for c in buildable),
            "wof": sorted(c.wof_code for c in buildable),
            "openaddresses": [f"{f}.csv" for c in buildable for f in c.openaddresses],
        }

    def entry(r):
        body = {
            "core": block(r.core),
            "overlap": block(r.overlap),
            "srtm": [{name: box} for name, box in sorted(r.srtm.items())],
        }
        if r.gtfs_feeds:
            body["gtfs_feeds"] = [f["url"] for f in r.gtfs_feeds]
        return {r.name: body}

    regions = [entry(r) for r in resolved.regions]
    return {"regions": regions, "border-regions": resolved.border_regions}
