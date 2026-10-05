"""Build MapLibre styles from the vendored Protomaps basemap styles.

A configuration picks a light and a dark base style and may override the minimum zoom at which each *kind of
place label* appears. The `places` source-layer has one layer per `kind` (country, region, neighbourhood/macrohood,
locality); the single locality layer is split into one layer per label kind (capital, city, town, village, other,
by `capital` / `kind_detail`) so each can get its own minimum zoom. A style can only show a feature at/after the
tile's own `min_zoom` (Protomaps decides per feature, mostly cities ~Z4-6, towns ~Z8, villages ~Z10-11).
"""

import copy
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from datamanager import map_styles
from datamanager.errors import ValidationError
from datamanager.models import StyleSettings

# Label kinds: key -> UI label.
LABEL_KEYS = {
    "country": "Countries",
    "state": "States / regions",
    "capital": "Capitals",
    "city": "Cities",
    "town": "Towns",
    "village": "Villages",
    "suburb": "Suburbs / neighbourhoods",
    "other": "Hamlets and other localities",
}
SOURCE = "protomaps"
TILES_PLACEHOLDER = "pmtiles://__TILES__"
MAX_ZOOM = 22
# Layer id -> label kind of the places layers (after the locality split).
PLACE_LAYERS = {
    "places_country": "country", "places_region": "state", "places_subplace": "suburb",
    "places_locality_capital": "capital", "places_locality_city": "city", "places_locality_town": "town",
    "places_locality_village": "village", "places_locality_other": "other",
}
LOCALITY_ID = "places_locality"
PUBLIC_GLYPHS = "https://protomaps.github.io/basemaps-assets/fonts/{fontstack}/{range}.pbf"
PUBLIC_TILES_URL = "pmtiles://https://tiles.example.com/planet.pmtiles"  # placeholder until the deploy URL is set
# Release styles are Go templates: tilesservice fills in its public URL (`TilesBaseURL`) and the tileset name per request.
TEMPLATE_BASE = "{{.TilesBaseURL}}"
RELEASE_TILES = TEMPLATE_BASE + "/{{.Tileset}}/{z}/{x}/{y}"
RELEASE_GLYPHS = TEMPLATE_BASE + "/fonts/{fontstack}/{range}.pbf"
RELEASE_MAXZOOM = 15  # the planet PMTiles holds Z0-15; clients over-zoom
MAP_ASSETS = Path(__file__).resolve().parent.parent / "blueprints" / "configure" / "static" / "map-assets"  # vendored glyphs + sprites


def _locality_filters() -> dict[str, list]:
    """Legacy filter syntax, like the vendored ones (the two syntaxes cannot be mixed inside one filter)."""
    not_capital = ["!=", "capital", "yes"]
    return {
        "capital": ["==", "capital", "yes"],
        "city": ["all", not_capital, ["==", "kind_detail", "city"]],
        "town": ["all", not_capital, ["==", "kind_detail", "town"]],
        "village": ["all", not_capital, ["==", "kind_detail", "village"]],
        "other": ["all", not_capital, ["!in", "kind_detail", "city", "town", "village"]],
    }


def load(key: str) -> dict:
    """The vendored style with the locality layer split into one layer per label kind."""
    style = map_styles.load(key)
    layers = []
    for layer in style["layers"]:
        if layer.get("id") != LOCALITY_ID:
            layers.append(layer)
            continue
        for kind, extra in _locality_filters().items():
            part = copy.deepcopy(layer)
            part["id"] = f"{LOCALITY_ID}_{kind}"
            part["filter"] = ["all", layer["filter"], extra]
            layers.append(part)
    style["layers"] = layers
    return style


def layer_keys(layer: dict) -> set[str] | None:
    """The label kind a `places` layer draws (None for every other layer)."""
    kind = PLACE_LAYERS.get(layer.get("id", ""))
    return {kind} if kind and layer.get("source-layer") == "places" else None


def place_layers(style: dict) -> dict[str, set[str]]:
    return {l["id"]: keys for l in style["layers"] if (keys := layer_keys(l))}


def style_defaults(style: dict) -> dict[str, int]:
    """Per label kind, the earliest zoom the base style shows it at (0 = whatever the tile carries)."""
    layers = {l["id"]: l for l in style["layers"]}
    defaults: dict[str, int] = {}
    for layer_id, keys in place_layers(style).items():
        zoom = int(layers[layer_id].get("minzoom") or 0)
        for key in keys:
            defaults[key] = min(defaults.get(key, zoom), zoom)
    return defaults


# --- Building ----------------------------------------------------------------------


def _effective_zoom(keys: set[str], overrides: dict, defaults: dict) -> int:
    return min(overrides.get(k, defaults.get(k, 0)) for k in keys)


def build_style(key: str, overrides: dict | None = None, tiles_url: str | None = None,
                glyphs: str | None = None, sprite: str | None = None) -> dict:
    """The base style with our source/glyph/sprite URLs and label zoom overrides applied."""
    overrides = overrides or {}
    style = load(key)
    defaults = style_defaults(style)
    style["sources"][SOURCE]["url"] = tiles_url or PUBLIC_TILES_URL
    if glyphs:
        style["glyphs"] = glyphs
    if sprite:
        style["sprite"] = sprite
    keys_by_layer = place_layers(style)
    for layer in style["layers"]:
        keys = keys_by_layer.get(layer["id"])
        if keys and any(k in overrides for k in keys):
            layer["minzoom"] = zoom = _effective_zoom(keys, overrides, defaults)
            if "maxzoom" in layer and layer["maxzoom"] <= zoom:
                del layer["maxzoom"]  # a raised minzoom must not hide the layer completely
    return style


def preview_info(key: str, tiles_url: str | None = None, assets_url: str | None = None) -> dict:
    """What the live preview needs: the style over our own approved tiles (`tiles_url`, None when there are none
    yet), which layers draw which label kinds, and the base style's defaults. `assets_url` is the (absolute) base of
    the vendored glyphs/sprites served by the app (`fonts/`, `sprites/` below it); without it the public ones are used."""
    if assets_url:
        sprite = f"{assets_url.rstrip('/')}/sprites/{load(key)['sprite'].rsplit('/', 1)[-1]}"
        style = build_style(key, tiles_url=tiles_url, glyphs=f"{assets_url.rstrip('/')}/fonts/{{fontstack}}/{{range}}.pbf", sprite=sprite)
    else:
        style = build_style(key, tiles_url=tiles_url, glyphs=PUBLIC_GLYPHS)
    return {
        "style": style,
        "tiles_available": bool(tiles_url),
        "layers": {lid: sorted(keys) for lid, keys in place_layers(style).items()},
        "defaults": style_defaults(style),
    }


# --- Settings ----------------------------------------------------------------------


def get_row(session: Session, config_id: int) -> StyleSettings | None:
    return session.scalar(select(StyleSettings).where(StyleSettings.config_profile_id == config_id))


def _known(key: str | None, mode: str) -> str:
    """The saved style key, or the default when it is gone (rows saved for the former OpenMapTiles styles)."""
    style = map_styles.STYLES.get(key or "")
    return key if style and style.mode == mode else map_styles.DEFAULTS[mode]


def get_settings(session: Session, config_id: int) -> dict:
    row = get_row(session, config_id)
    return {
        "light_style": _known(row.light_style if row else None, "light"),
        "dark_style": _known(row.dark_style if row else None, "dark"),
        "labels": dict(row.labels_json) if row else {},
    }


def _clean_labels(labels: dict, light: str, dark: str) -> dict[str, int]:
    cleaned: dict[str, int] = {}
    for key, value in labels.items():
        if key not in LABEL_KEYS:
            raise ValidationError(f"Unknown label kind '{key}'", field=key)
        if value in (None, ""):
            continue
        try:
            zoom = int(str(value).strip())
        except ValueError:
            raise ValidationError(f"{LABEL_KEYS[key]}: zoom must be a whole number", field=key) from None
        if not 0 <= zoom <= MAX_ZOOM:
            raise ValidationError(f"{LABEL_KEYS[key]}: zoom must be between 0 and {MAX_ZOOM}", field=key)
        cleaned[key] = zoom
    for style_key in {light, dark}:
        # A default of 0 means "whatever the tile carries" (no limit), so it is not compared.
        eff = {**{k: z for k, z in style_defaults(load(style_key)).items() if z > 0}, **cleaned}
        for smaller, larger in (("city", "town"), ("town", "village")):
            if smaller in eff and larger in eff and eff[smaller] > eff[larger]:
                raise ValidationError(
                    f"{LABEL_KEYS[larger]} cannot appear before {LABEL_KEYS[smaller].lower()} (zoom {eff[larger]} < {eff[smaller]})",
                    field=larger,
                )
    return cleaned


def save_settings(session: Session, config_id: int, light: str, dark: str, labels: dict) -> None:
    if light not in map_styles.STYLES or map_styles.STYLES[light].mode != "light":
        raise ValidationError("Choose a light style", field="light_style")
    if dark not in map_styles.STYLES or map_styles.STYLES[dark].mode != "dark":
        raise ValidationError("Choose a dark style", field="dark_style")
    cleaned = _clean_labels(labels, light, dark)
    row = get_row(session, config_id) or StyleSettings(config_profile_id=config_id)
    row.light_style, row.dark_style, row.labels_json = light, dark, cleaned
    session.add(row)
    session.commit()


def config_style(session: Session, config_id: int, mode: str, **urls) -> dict:
    """The patched style of a configuration for `mode` (light|dark)."""
    settings = get_settings(session, config_id)
    return build_style(settings[f"{mode}_style"], settings["labels"], **urls)


# --- Release styles (what the tiles package ships) ----------------------------------------------


def sprite_flavor(key: str) -> str:
    """The sprite sheet name of a base style (`light`, `dark`, `white`, ...)."""
    return load(key)["sprite"].rsplit("/", 1)[-1]


def release_style(session: Session, config_id: int, mode: str) -> dict:
    """The configuration's style for `mode` as a template: tiles, glyphs and sprite come from tilesservice."""
    settings = get_settings(session, config_id)
    key = settings[f"{mode}_style"]
    style = build_style(key, settings["labels"], glyphs=RELEASE_GLYPHS, sprite=f"{TEMPLATE_BASE}/sprites/{sprite_flavor(key)}")
    source = style["sources"][SOURCE]
    source.pop("url", None)
    source.update(tiles=[RELEASE_TILES], minzoom=0, maxzoom=RELEASE_MAXZOOM)
    return style


def font_stacks(style: dict) -> set[str]:
    """Every font name the style's text layers can ask for (plain lists and `literal` expressions)."""
    found: set[str] = set()

    def walk(value):
        if isinstance(value, str):
            found.add(value)
        elif isinstance(value, list):
            for v in value:
                walk(v)

    for layer in style.get("layers", []):
        walk(layer.get("layout", {}).get("text-font"))
    return {f for f in found if f not in ("case", "literal", "<=", "get", "min_zoom") and not f.isdigit()}


def vendored_fonts() -> set[str]:
    return {p.name for p in (MAP_ASSETS / "fonts").iterdir() if p.is_dir()} if (MAP_ASSETS / "fonts").is_dir() else set()


def vendored_sprites() -> set[str]:
    return {p.stem for p in (MAP_ASSETS / "sprites").glob("*.json") if "@" not in p.stem} if (MAP_ASSETS / "sprites").is_dir() else set()
