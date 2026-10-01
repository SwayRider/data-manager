import re
from collections.abc import Mapping

from datamanager.country_sources import CountrySource, SourceState
from datamanager.errors import ValidationError

LEVEL_MIN, LEVEL_MAX = 5, 12


class OsmAdminSource(CountrySource):
    """Locality polygons from OSM administrative boundaries in the country's Geofabrik extract.

    Config: {"levels": [9, ...]}. Which admin_level is the sub-municipal "locality"
    differs per country (BE 9, NL 10, DE 9+10, ...), so it is configured, never guessed.
    """

    key = "osm_admin"
    label = "OSM administrative boundaries"
    form_template = "countries/sources/osm_admin.html"

    def default(self, iso2: str, curated: Mapping) -> SourceState:
        levels = [int(level) for level in curated.get("osm_locality_levels", [])]
        return SourceState(enabled=bool(levels), config={"levels": levels})

    def from_form(self, form: Mapping) -> SourceState:
        text = form.get("osm_admin_levels", "")
        return SourceState(enabled=bool(text.strip()), config={"text": text})

    def validate(self, state: SourceState, verify: bool) -> SourceState:
        if "text" in state.config:
            levels = []
            for token in re.split(r"[,\s]+", state.config["text"].strip()):
                if not token:
                    continue
                if not token.isdigit() or not LEVEL_MIN <= int(token) <= LEVEL_MAX:
                    raise ValidationError(
                        f"Admin levels must be whole numbers between {LEVEL_MIN} and {LEVEL_MAX} "
                        f"(got {token!r})",
                        field="osm_admin_levels",
                    )
                levels.append(int(token))
            levels = sorted(set(levels))
        else:
            levels = state.config.get("levels", [])
        return SourceState(enabled=bool(levels), config={"levels": levels})

    def form_values(self, state: SourceState) -> dict:
        if "text" in state.config:
            return {"osm_admin_levels": state.config["text"]}
        return {"osm_admin_levels": ", ".join(str(level) for level in state.config.get("levels", []))}

    def describe(self, state: SourceState) -> str:
        levels = state.config.get("levels", [])
        return "OSM L" + ",".join(str(level) for level in levels) if state.enabled and levels else "—"
