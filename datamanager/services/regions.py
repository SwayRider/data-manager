import datetime
import re
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from datamanager.errors import ValidationError
from datamanager.models import (
    Country,
    Region,
    RegionCountry,
    RegionGtfsFeed,
    RegionOpenAddressesExclusion,
    RegionOverlap,
)
from datamanager.services import address_sources as address_source_service
from datamanager.services import carve as carve_service
from datamanager.services import overlap

NAME_MAX = 100
FEED_URL_MAX = 500
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
# Distinct, non-grey (grey means "unassigned" on the map).
PALETTE = (
    "#e6194b", "#3cb44b", "#4363d8", "#f58231", "#911eb4", "#008080",
    "#f032e6", "#9a6324", "#800000", "#808000", "#000075", "#00a3a3",
)


def list_regions(session: Session, config_id: int) -> list[Region]:
    return list(session.scalars(select(Region).where(Region.config_profile_id == config_id).order_by(Region.name)))


def get_region(session: Session, config_id: int, region_id: int) -> Region | None:
    region = session.get(Region, region_id)
    return region if region is not None and region.config_profile_id == config_id else None


def _name_taken(session: Session, config_id: int, name: str, exclude_id: int | None = None) -> bool:
    stmt = select(Region.id).where(Region.config_profile_id == config_id, Region.name == name)  # NOCASE
    if exclude_id is not None:
        stmt = stmt.where(Region.id != exclude_id)
    return session.scalar(stmt) is not None


def _clean_name(session: Session, config_id: int, name: str, exclude_id: int | None = None) -> str:
    name = (name or "").strip()
    if not name:
        raise ValidationError("Name is required", field="name")
    if len(name) > NAME_MAX:
        raise ValidationError(f"Name must be at most {NAME_MAX} characters", field="name")
    if _name_taken(session, config_id, name, exclude_id):
        raise ValidationError("A region with this name already exists", field="name")
    return name


def _clean_color(color: str) -> str:
    color = (color or "").strip()
    if not COLOR_RE.match(color):
        raise ValidationError("Color must look like #rrggbb", field="color")
    return color.lower()


def next_color(session: Session, config_id: int) -> str:
    used = {r.color.lower() for r in list_regions(session, config_id)}
    for color in PALETTE:
        if color not in used:
            return color
    return PALETTE[len(used) % len(PALETTE)]


def _commit(session: Session) -> None:
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise ValidationError("A region with this name already exists", field="name")


def create_region(session: Session, config_id: int, name: str, color: str | None = None) -> Region:
    region = Region(
        config_profile_id=config_id,
        name=_clean_name(session, config_id, name),
        color=_clean_color(color) if color else next_color(session, config_id),
    )
    session.add(region)
    _commit(session)
    return region


def update_region(session: Session, region: Region, name: str, color: str) -> Region:
    region.name = _clean_name(session, region.config_profile_id, name, exclude_id=region.id)
    region.color = _clean_color(color)
    _commit(session)
    return region


def delete_region(session: Session, region: Region) -> None:
    session.delete(region)
    session.commit()


def assign_country(session: Session, region: Region, iso2: str) -> RegionCountry:
    iso2 = iso2.lower()
    country = session.get(Country, iso2)
    if country is None:
        raise ValidationError("Unknown country", field="country")
    if not country.is_curated:
        raise ValidationError(f"{country.name} is not configured yet", field="country")
    existing = session.scalar(
        select(RegionCountry).where(
            RegionCountry.config_profile_id == region.config_profile_id, RegionCountry.country_iso == iso2
        )
    )
    if existing is not None:
        if existing.region_id == region.id:
            return existing
        raise ValidationError(
            f"{country.name} is already in region {existing.region.name}; remove it there first",
            field="country",
        )
    link = RegionCountry(region_id=region.id, config_profile_id=region.config_profile_id, country_iso=iso2)
    session.add(link)
    session.commit()
    evaluate_overlap(session, region)  # the core changed
    return link


def remove_country(session: Session, region: Region, iso2: str) -> None:
    link = session.scalar(
        select(RegionCountry).where(RegionCountry.region_id == region.id, RegionCountry.country_iso == iso2.lower())
    )
    if link is not None:
        session.delete(link)
        session.commit()
        evaluate_overlap(session, region)  # the core changed


def country_assignments(session: Session, config_id: int) -> dict[str, dict]:
    rows = session.execute(
        select(RegionCountry.country_iso, Region.id, Region.name, Region.color)
        .join(Region, Region.id == RegionCountry.region_id)
        .where(RegionCountry.config_profile_id == config_id)
    )
    return {iso: {"id": rid, "name": name, "color": color} for iso, rid, name, color in rows}


# --- Overlap (stored per region; auto rows recomputed when the core changes) ----

OVERRIDE_MODES = ("include", "exclude")


def _core_keys(region: Region, countries: dict[str, Country], carves: dict[str, str]) -> tuple:
    keys = (
        overlap.geometry_key(link.country_iso, countries[link.country_iso].ne_geometry_ref, carves.get(link.country_iso))
        for link in region.countries
        if link.country_iso in countries
    )
    return tuple(sorted(k for k in keys if k is not None))


def core_keys(session: Session, region: Region) -> tuple:
    """Geometry keys of the region's core countries, carve-aware (what overlap detection uses)."""
    countries = {c.iso2: c for c in session.scalars(select(Country))}
    return _core_keys(region, countries, carve_service.carve_map(session, region.config_profile_id))


def _universe_keys(countries: dict[str, Country], carves: dict[str, str]) -> tuple:
    keys = (overlap.geometry_key(iso, c.ne_geometry_ref, carves.get(iso)) for iso, c in countries.items())
    return tuple(sorted(k for k in keys if k is not None))


def evaluate_overlap(session: Session, region: Region) -> None:
    """Re-detect the region's overlap from its core and sync the stored rows.

    New detections become `auto` rows; `auto` rows no longer detected are removed; user
    overrides are kept unless they became meaningless (a stale `exclude`, an `include` that
    is now detected -> `auto`, or a country that became core).
    """
    countries = {c.iso2: c for c in session.scalars(select(Country))}
    carves = carve_service.carve_map(session, region.config_profile_id)
    core_isos = {link.country_iso for link in region.countries}
    detected = set(
        overlap.overlap_candidates(_core_keys(region, countries, carves), _universe_keys(countries, carves))
    ) - core_isos

    kept: set[str] = set()
    for row in list(region.overlap_rows):
        stale = (
            row.country_iso in core_isos
            or (row.mode in ("auto", "exclude") and row.country_iso not in detected)
        )
        if stale:
            session.delete(row)
            continue
        if row.mode == "include" and row.country_iso in detected:
            row.mode = "auto"
        kept.add(row.country_iso)
    for iso in sorted(detected - kept):
        session.add(RegionOverlap(region_id=region.id, country_iso=iso, mode="auto"))
    region.overlap_evaluated_at = datetime.datetime.now(datetime.UTC)
    session.commit()
    session.expire(region)


def evaluate_config_overlap(session: Session, config_id: int) -> None:
    """Re-evaluate every region of a configuration (e.g. after a country was carved)."""
    for region in list_regions(session, config_id):
        evaluate_overlap(session, region)


def evaluate_all_overlap(session: Session, only_missing: bool = False) -> int:
    regions = list(session.scalars(select(Region)))
    todo = [r for r in regions if not only_missing or r.overlap_evaluated_at is None]
    for region in todo:
        evaluate_overlap(session, region)
    return len(todo)


def set_overlap_override(session: Session, region: Region, iso2: str, mode: str) -> None:
    """`exclude` drops an overlap country (auto-detected or forced); `include` forces one in
    (or restores an excluded one)."""
    iso2 = iso2.lower()
    if mode not in OVERRIDE_MODES:
        raise ValidationError("mode must be include or exclude", field="mode")
    country = session.get(Country, iso2)
    if country is None:
        raise ValidationError("Unknown country", field="country")
    if any(link.country_iso == iso2 for link in region.countries):
        raise ValidationError(f"{country.name} is a core country of {region.name}", field="country")
    row = next((o for o in region.overlap_rows if o.country_iso == iso2), None)
    if mode == "exclude":
        if row is None or row.mode == "exclude":
            raise ValidationError(f"{country.name} is not in the overlap of {region.name}", field="country")
        if row.mode == "auto":
            row.mode = "exclude"
        else:  # forced -> simply not in the overlap any more
            session.delete(row)
    else:
        if row is None:
            if not country.is_curated:
                raise ValidationError(f"{country.name} is not configured yet", field="country")
            session.add(RegionOverlap(region_id=region.id, country_iso=iso2, mode="include"))
        elif row.mode == "exclude":
            row.mode = "auto"
    session.commit()
    session.expire(region)


def clear_overlap_override(session: Session, region: Region, iso2: str) -> None:
    """Back to automatic: an excluded country is auto again, a forced one is dropped."""
    for row in [o for o in region.overlap_rows if o.country_iso == iso2.lower()]:
        if row.mode == "exclude":
            row.mode = "auto"
        elif row.mode == "include":
            session.delete(row)
    session.commit()
    session.expire(region)


def effective_overlap(region: Region) -> list[str]:
    """ISO codes in the region's overlap (auto + forced), as stored."""
    return sorted(o.country_iso for o in region.overlap_rows if o.mode in ("auto", "include"))


def overlap_summary(session: Session, config_id: int) -> dict:
    """Stored overlap per region plus the border pairs (cores' 10 km buffers intersect).
    Regions never evaluated (e.g. created before overlap was stored) are evaluated first."""
    countries = {c.iso2: c for c in session.scalars(select(Country))}
    regions = list_regions(session, config_id)
    for region in regions:
        if region.overlap_evaluated_at is None:
            evaluate_overlap(session, region)
    carves = carve_service.carve_map(session, config_id)
    cores = {r.id: _core_keys(r, countries, carves) for r in regions}

    result: dict[str, dict] = {}
    for region in regions:
        rows = {o.country_iso: o.mode for o in region.overlap_rows}
        effective = sorted(iso for iso, mode in rows.items() if mode in ("auto", "include"))
        result[str(region.id)] = {
            "effective": [
                {
                    "iso2": iso,
                    "name": countries[iso].name,
                    "configured": countries[iso].is_curated,
                    "source": "forced" if rows[iso] == "include" else "auto",
                }
                for iso in effective
            ],
            "excluded": sorted(iso for iso, mode in rows.items() if mode == "exclude"),
            "needs_config": sorted(iso for iso in effective if not countries[iso].is_curated),
        }

    borders = [
        [a.name, b.name]
        for i, a in enumerate(regions)
        for b in regions[i + 1 :]
        if overlap.bordering(cores[a.id], cores[b.id])
    ]
    return {"regions": result, "borders": borders}


def region_buffer(session: Session, region: Region) -> dict | None:
    countries = {c.iso2: c for c in session.scalars(select(Country))}
    carves = carve_service.carve_map(session, region.config_profile_id)
    return overlap.buffer_geojson(_core_keys(region, countries, carves))


# --- Per-region narrowing of OpenAddresses files ---------------------------------


def set_openaddresses_file(session: Session, region: Region, iso2: str, file: str, included: bool) -> None:
    """Include/exclude one OpenAddresses file of a core or overlap country for this region."""
    iso2 = iso2.lower()
    country = session.get(Country, iso2)
    in_region = iso2 in {l.country_iso for l in region.countries} or iso2 in effective_overlap(region)
    if country is None or not in_region:
        raise ValidationError("Country is not part of this region", field="country")
    if file not in address_source_service.openaddresses_files(country):
        raise ValidationError(f"Unknown OpenAddresses file for {country.name}", field="file")
    row = next(
        (r for r in region.openaddresses_exclusions if r.country_iso == iso2 and r.file == file), None
    )
    if included and row is not None:
        session.delete(row)
    elif not included and row is None:
        session.add(RegionOpenAddressesExclusion(region_id=region.id, country_iso=iso2, file=file))
    session.commit()
    session.expire(region)


# --- GTFS feeds -------------------------------------------------------------------


def add_gtfs_feed(session: Session, region: Region, url: str, label: str = "") -> RegionGtfsFeed:
    url, label = (url or "").strip(), (label or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValidationError("Feed URL must start with http:// or https://", field="url")
    if len(url) > FEED_URL_MAX:
        raise ValidationError(f"Feed URL must be at most {FEED_URL_MAX} characters", field="url")
    if len(label) > NAME_MAX:
        raise ValidationError(f"Label must be at most {NAME_MAX} characters", field="label")
    if any(f.url == url for f in region.gtfs_feeds):
        raise ValidationError("This feed is already in the region", field="url")
    feed = RegionGtfsFeed(region_id=region.id, url=url, label=label or None)
    session.add(feed)
    session.commit()
    session.expire(region)
    return feed


def remove_gtfs_feed(session: Session, region: Region, feed_id: int) -> None:
    feed = next((f for f in region.gtfs_feeds if f.id == feed_id), None)
    if feed is None:
        raise ValidationError("Unknown feed for this region", field="feed")
    session.delete(feed)
    session.commit()
    session.expire(region)
