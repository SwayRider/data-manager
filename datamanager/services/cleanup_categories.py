"""The file categories of the post-packaging cleanup (`RELEASE-CONTRACT.md` §8) with the user's default per
category. Plain data, so `services/settings.py` can register `cleanup.default.<key>` without importing the cleanup."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Category:
    key: str
    label: str
    help: str
    default: str  # "delete" | "keep": whether the dialog ticks it


CATEGORIES: tuple[Category, ...] = (
    Category("planet", "Planet OSM download", "downloads/planet; fetched again by Download planet.", "keep"),
    Category("tiles", "Protomaps tiles download", "downloads/tiles; only removable when a verified package holds the file.", "keep"),
    Category("country_pbf", "Per-country PBFs", "Extracted from the planet by Extract countries.", "delete"),
    Category("region_pbf", "Region PBFs", "Core and full PBF per region; made by OSM extract.", "delete"),
    Category("valhalla", "Valhalla outputs", "Tiles, admin and timezone databases, edge polylines.", "delete"),
    Category("pelias_snapshots", "Pelias index snapshots", "Current (when packaged) and superseded Elasticsearch snapshots.", "delete"),
    Category("pelias_wof", "Pelias WOF data", "Patched WOF directories and databases, also unreviewed ones.", "delete"),
    Category("interpolation", "Interpolation databases", "street.db and address.db per region.", "delete"),
    Category("small_assets", "Small derived assets", "Polygons, borders, crossings, outlines, pelias.json, styles. Never the carve-out polygons.", "delete"),
    Category("srtm_unpacked", "Unpacked SRTM tiles", "library/srtm/<run>; made by Download elevation.", "delete"),
    Category("srtm_downloads", "SRTM downloads", "downloads/srtm.", "keep"),
    Category("pelias_sources", "Pelias source downloads", "Who's On First, GeoNames, OpenAddresses, Placeholder.", "delete"),
    Category("overture_gtfs", "Overture and GTFS downloads", "Overture CSVs and GTFS zips.", "delete"),
    Category("old_versions", "Older download versions", "Versions beyond the newest two of every source (pinned and in-use ones stay).", "delete"),
    Category("leftovers", "Leftovers of failed or rejected runs", "Unapproved assets of finished runs and work directories.", "delete"),
)
BY_KEY = {c.key: c for c in CATEGORIES}
