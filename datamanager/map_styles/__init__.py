"""Vendored MapLibre base styles (Protomaps basemap schema), selectable per configuration.

Files live in `datamanager/styles/` (see NOTICE there); a style is added by dropping its
JSON there and registering it below.
"""

import copy
import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

STYLES_DIR = Path(__file__).resolve().parent.parent / "styles"


@dataclass(frozen=True)
class MapStyle:
    key: str
    label: str
    mode: str  # "light" | "dark"
    file: str


STYLES: dict[str, MapStyle] = {
    s.key: s
    for s in (
        MapStyle("light", "Light (default)", "light", "protomaps-light.json"),
        MapStyle("classic", "Classic (orange / yellow roads)", "light", "protomaps-classic.json"),
        MapStyle("white", "White (minimal)", "light", "protomaps-white.json"),
        MapStyle("grayscale", "Grayscale", "light", "protomaps-grayscale.json"),
        MapStyle("dark", "Dark (default)", "dark", "protomaps-dark.json"),
        MapStyle("vivid-dark", "Vivid dark (coloured roads)", "dark", "protomaps-vivid-dark.json"),
        MapStyle("black", "Black (OLED)", "dark", "protomaps-black.json"),
    )
}
DEFAULTS = {"light": "light", "dark": "dark"}


@lru_cache(maxsize=None)
def _raw(key: str) -> str:
    return (STYLES_DIR / STYLES[key].file).read_text()


def load(key: str) -> dict:
    """A fresh copy of the vendored style (safe to mutate)."""
    return copy.deepcopy(json.loads(_raw(key)))


def for_mode(mode: str) -> list[MapStyle]:
    return [s for s in STYLES.values() if s.mode == mode]
