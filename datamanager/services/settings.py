"""Global settings: a registry in code (defaults, validation) plus saved overrides in `global_setting`.

Same pattern as the rest of the app: defaults live in code, the DB stores only what differs, and
clearing a field (or saving the default) deletes the row. Custom binary paths of the required tools
are stored as `toolpath.<tool>` and handled by `services/tools.py` through `get_raw`/`set_raw`."""
import re
from dataclasses import dataclass
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from datamanager.errors import ValidationError
from datamanager.services.cleanup_categories import CATEGORIES
from datamanager.models import GlobalSetting
from datamanager.services.map_styles import PUBLIC_GLYPHS, PUBLIC_TILES_URL

TOOLPATH_PREFIX = "toolpath."


@dataclass(frozen=True)
class SettingDef:
    key: str
    group: str
    label: str
    help: str
    kind: str  # "str" | "url" | "int"
    default: str | int
    min: int | None = None
    max: int | None = None
    schemes: tuple[str, ...] = ("http", "https")
    requires: tuple[str, ...] = ()  # substrings a URL template must contain
    pattern: str | None = None  # full-match regex for strings
    secret: bool = False  # never shown again after saving; blank on save keeps it, a clear checkbox removes it


GROUPS = {
    "public": "Public URLs",
    "styles": "Map style in the tiles release",
    "download": "Download sources",
    "run": "Resources",
    "tool": "Tool versions",
    "pelias": "Pelias",
    "package": "Package repository",
    "cleanup": "Cleanup after packaging (ticked by default = delete)",
}

SETTINGS: tuple[SettingDef, ...] = (
    SettingDef("public.tiles_url", "public", "Tiles URL", "PMTiles file (pmtiles://https://host/path/tiles.pmtiles) or TileJSON URL written into the exported map styles.",
               "url", PUBLIC_TILES_URL, schemes=("http", "https", "pmtiles")),
    SettingDef("public.glyphs_url", "public", "Glyphs URL", "Font glyph template, must contain {fontstack} and {range}.",
               "url", PUBLIC_GLYPHS, requires=("{fontstack}", "{range}")),
    SettingDef("public.sprite_url", "public", "Sprite URL", "Empty keeps the sprite of the vendored base style.",
               "url", ""),
    SettingDef("styles.id", "styles", "Style id", "Id of the style in the tiles release (lowercase letters, digits, dashes); part of its URL.",
               "str", "swayrider", pattern=r"[a-z0-9][a-z0-9-]{0,30}"),
    SettingDef("styles.label", "styles", "Style name", "Name apps show when the user picks a style.", "str", "SwayRider", pattern=r".{1,60}"),
    SettingDef("download.osm", "download", "OSM extracts", "Base URL for Geofabrik downloads.",
               "url", "https://download.geofabrik.de/"),
    SettingDef("download.planet", "download", "OSM planet",
               "Full planet PBF the country extracts are cut from (any URL serving a planet PBF with Range and ETag/Last-Modified; "
               "an optional <url>.md5 file is verified). Default is the FAU mirror; the official alternative is "
               "https://planet.openstreetmap.org/pbf/planet-latest.osm.pbf.",
               "url", "https://ftp.fau.de/osm-planet/pbf/planet-latest.osm.pbf"),
    SettingDef("download.planet_keep", "download", "Planet versions kept", "How many planet versions are kept on disk (current + previous).",
               "int", 2, min=1, max=5),
    SettingDef("download.tiles_builds", "download", "Protomaps build list", "JSON list of the daily Protomaps basemap builds (newest is used).",
               "url", "https://build-metadata.protomaps.dev/builds.json"),
    SettingDef("download.tiles_build", "download", "Protomaps builds", "Base URL of the daily Protomaps planet builds (<yyyymmdd>.pmtiles, about 140 GB each). "
               "Protomaps asks not to hotlink: the build is fetched once per release and kept.",
               "url", "https://build.protomaps.com/"),
    SettingDef("download.tiles_keep", "download", "Tile builds kept", "How many full Protomaps planet builds are kept on disk.",
               "int", 1, min=1, max=3),
    SettingDef("package.keep", "package", "Packages kept", "How many unprotected packages `packages-prune` keeps (newest first).",
               "int", 3, min=1, max=50),
    SettingDef("download.connections", "download", "Parallel connections", "Parallel Range segments used for large downloads.",
               "int", 4, min=1, max=8),
    SettingDef("download.country_polys", "download", "Country polygons", "Base URL of the per-country .poly files (a few kB each) used to cut countries from the planet.",
               "url", "https://download.geofabrik.de/"),
    SettingDef("osm.source", "download", "Country PBF source",
               "Where the OSM extract stage gets country PBFs: 'planet' (cut from the planet) or 'geofabrik' (per-country downloads).",
               "str", "planet", pattern=r"planet|geofabrik"),
    SettingDef("download.srtm", "download", "SRTM elevation", "Base URL of the Skadi elevation tiles.",
               "url", "s3://elevation-tiles-prod/skadi/", schemes=("http", "https", "s3")),
    SettingDef("download.srtm_keep", "download", "SRTM versions kept", "How many versions of each SRTM tile are kept (they almost never change).",
               "int", 1, min=1, max=3),
    SettingDef("download.natural_earth", "download", "Natural Earth", "Base URL for Natural Earth zips.",
               "url", "https://naturalearth.s3.amazonaws.com/"),
    SettingDef("download.land_polygons", "download", "OSM land polygons", "Split land polygons zip (EPSG:4326).",
               "url", "https://osmdata.openstreetmap.de/download/land-polygons-split-4326.zip"),
    SettingDef("download.wof", "download", "Who's On First", "Base URL of the WOF distribution (sqlite/inventory.json and the per-country bundles).",
               "url", "https://data.geocode.earth/wof/dist"),
    SettingDef("download.geonames", "download", "GeoNames", "Base URL of the GeoNames exports (dump/allCountries.zip, zip/<CC>.zip).",
               "url", "https://download.geonames.org/export"),
    SettingDef("download.openaddresses", "download", "OpenAddresses", "Base URL of the OpenAddresses batch API.",
               "url", "https://batch.openaddresses.io"),
    SettingDef("download.placeholder", "download", "Pelias placeholder store", "Pinned store.sqlite3.gz of the Placeholder service.",
               "url", "https://data.geocode.earth/placeholder/2021-08-01/store.sqlite3.gz"),
    SettingDef("run.max_workers", "run", "Max parallel workers", "Upper bound for parallel per-region/tile jobs.",
               "int", 2, min=1, max=64),
    SettingDef("run.extract_memory_gb", "run", "Country extraction memory (GB)",
               "Memory budget for cutting countries out of the planet. osmium needs about 3 GB per country cut in the same run, "
               "so countries are cut in batches of budget / 3 (each batch reads the planet again).",
               "int", 16, min=4, max=512),
    SettingDef("run.extract_min_free_gb", "run", "Stop extraction below free memory (GB)",
               "osmium is stopped and the run fails when the machine's available memory drops below this.",
               "int", 4, min=1, max=64),
    SettingDef("run.valhalla_concurrency", "run", "Valhalla tile build threads", "Threads of valhalla_build_tiles.",
               "int", 4, min=1, max=64),
    SettingDef("run.java_xmx", "run", "Java heap (-Xmx)", "Heap for planetiler, e.g. 16g or 8192m.",
               "str", "16g", pattern=r"\d+[gGmM]"),
    SettingDef("tool.valhalla_tag", "tool", "Valhalla version", "Git tag or branch built by the Valhalla stage.",
               "str", "latest", pattern=r"[A-Za-z0-9._/-]+"),
    SettingDef("tool.valhalla_jobs", "tool", "Valhalla build jobs", "Parallel make jobs when compiling Valhalla.",
               "int", 4, min=1, max=64),
    SettingDef("tool.elasticsearch_version", "tool", "Elasticsearch version", "Version used for Pelias.",
               "str", "7.17.28", pattern=r"\d+(\.\d+){1,2}"),
    SettingDef("tool.pelias_ref", "tool", "Pelias importers version",
               "Git branch or tag cloned for every Pelias repository (schema, whosonfirst, ...). The resulting commits are recorded with the build.",
               "str", "master", pattern=r"[A-Za-z0-9._/-]+"),
    SettingDef("pelias.es_work_dir", "pelias", "Elasticsearch work directory",
               "Absolute path where the temporary Elasticsearch of a Pelias run keeps its data and snapshots (bind mounts, removed when the "
               "container stops). Put it on a fast disk. Empty uses work/<run id>/es in the data root.",
               "str", "", pattern=r"/[^\s]*"),
    SettingDef("pelias.openaddresses_token", "pelias", "OpenAddresses token",
               "API token of batch.openaddresses.io, used by the Pelias downloads. Stored in the database as plain text, never shown again "
               "and not part of any configuration hash. Without it the OpenAddresses downloads are skipped.",
               "str", "", pattern=r"\S+", secret=True),
    SettingDef("pelias.es_heap", "pelias", "Elasticsearch heap", "JVM heap of that Elasticsearch, e.g. 4g.",
               "str", "4g", pattern=r"\d+[gGmM]"),
)

SETTINGS = SETTINGS + tuple(
    SettingDef(f"cleanup.default.{c.key}", "cleanup", c.label, c.help + " Default of the cleanup dialog: delete or keep.",
               "str", c.default, pattern="delete|keep")
    for c in CATEGORIES
)
BY_KEY = {s.key: s for s in SETTINGS}


def _row(session: Session, key: str) -> GlobalSetting | None:
    return session.get(GlobalSetting, key)


def get_raw(session: Session, key: str):
    """Saved value or None (no default applied); also used for `toolpath.*` keys."""
    row = _row(session, key)
    return row.value_json if row else None


def set_raw(session: Session, key: str, value) -> None:
    """Store `value`, or delete the row for None / ''. Does not commit."""
    row = _row(session, key)
    if value is None or value == "":
        if row:
            session.delete(row)
        return
    if row:
        row.value_json = value
    else:
        session.add(GlobalSetting(key=key, value_json=value))


def get(session: Session, key: str):
    definition = BY_KEY.get(key)
    if definition is None:
        raise KeyError(key)
    saved = get_raw(session, key)
    return definition.default if saved is None else saved


def _clean(definition: SettingDef, raw) -> str | int | None:
    """Normalise one submitted value; None = reset to default."""
    text = "" if raw is None else str(raw).strip()
    if text == "":
        return None
    if definition.kind == "int":
        try:
            number = int(text)
        except ValueError:
            raise ValidationError(f"{definition.label} must be a whole number.") from None
        if (definition.min is not None and number < definition.min) or (definition.max is not None and number > definition.max):
            raise ValidationError(f"{definition.label} must be between {definition.min} and {definition.max}.")
        return number
    if definition.kind == "url":
        parsed = urlparse(text)
        if parsed.scheme not in definition.schemes or not parsed.netloc:
            raise ValidationError(f"{definition.label} must be a URL starting with {' or '.join(s + '://' for s in definition.schemes)}.")
        for token in definition.requires:
            if token not in text:
                raise ValidationError(f"{definition.label} must contain {token}.")
        return text
    if definition.pattern and not re.fullmatch(definition.pattern, text):
        raise ValidationError(f"{definition.label} has an invalid format.")
    return text


def save(session: Session, values: dict[str, str], clear: frozenset[str] = frozenset()) -> None:
    """Validate and store submitted values (only the given keys); blank or default resets, except secrets: blank keeps
    the saved secret and only a key in `clear` removes it. One commit."""
    cleaned: dict[str, str | int | None] = {}
    for key, raw in values.items():
        definition = BY_KEY.get(key)
        if definition is None:
            raise ValidationError(f"Unknown setting: {key}")
        if definition.secret and key not in clear and not str(raw or "").strip():
            continue
        cleaned[key] = _clean(definition, raw)
    for key in clear:
        if key in BY_KEY and BY_KEY[key].secret:
            cleaned[key] = None
    for key, value in cleaned.items():
        set_raw(session, key, None if value == BY_KEY[key].default else value)
    session.commit()


def all_values(session: Session) -> list[dict]:
    """Settings grouped for the form: [{key, label, settings: [{def, value, is_default}]}]."""
    saved = {r.key: r.value_json for r in session.query(GlobalSetting).all()}
    groups = []
    for group_key, label in GROUPS.items():
        entries = []
        for definition in SETTINGS:
            if definition.group != group_key:
                continue
            is_default = definition.key not in saved
            entries.append({"def": definition, "value": definition.default if is_default else saved[definition.key],
                            "is_default": is_default})
        groups.append({"key": group_key, "label": label, "settings": entries})
    return groups


def public_urls(session: Session) -> dict[str, str]:
    """URLs for the exported styles, in `map_styles.build_style` argument names; empty ones are left out."""
    urls = {"tiles_url": get(session, "public.tiles_url"), "glyphs": get(session, "public.glyphs_url"),
            "sprite": get(session, "public.sprite_url")}
    return {k: v for k, v in urls.items() if v}
