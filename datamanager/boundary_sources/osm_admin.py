import re
from collections.abc import Mapping

from datamanager.country_sources import CountrySource, SourceState
from datamanager.errors import ValidationError

LEVEL_MIN, LEVEL_MAX = 5, 12


def _parse_levels(text: str, field: str) -> list[int]:
    levels = []
    for token in re.split(r"[,\s]+", text.strip()):
        if not token:
            continue
        if not token.isdigit() or not LEVEL_MIN <= int(token) <= LEVEL_MAX:
            raise ValidationError(
                f"Admin levels must be whole numbers between {LEVEL_MIN} and {LEVEL_MAX} (got {token!r})", field=field)
        levels.append(int(token))
    return sorted(set(levels))


class OsmAdminSource(CountrySource):
    """Locality and municipality polygons from OSM administrative boundaries in the country's extract.

    Config: {"levels": [9, ...], "localadmin_levels": [8, ...]}. `levels` hold the sub-municipal "locality"
    polygons (BE 9, NL 10, DE 9+10, ...), `localadmin_levels` the municipalities (BE 8, NL 8, ...) that WOF
    often lacks as polygons. Which level is which differs per country, so it is configured, never guessed
    (the wof-patch stage can suggest them). Enabled iff either list is set.
    """

    key = "osm_admin"
    label = "OSM administrative boundaries"
    form_template = "countries/sources/osm_admin.html"

    def default(self, iso2: str, curated: Mapping) -> SourceState:
        levels = [int(level) for level in curated.get("osm_locality_levels", [])]
        admin = [int(level) for level in curated.get("osm_localadmin_levels", [])]
        return SourceState(enabled=bool(levels or admin), config={"levels": levels, "localadmin_levels": admin})

    def from_form(self, form: Mapping) -> SourceState:
        text, admin = form.get("osm_admin_levels", ""), form.get("osm_localadmin_levels", "")
        return SourceState(enabled=bool(text.strip() or admin.strip()), config={"text": text, "admin_text": admin})

    def validate(self, state: SourceState, verify: bool) -> SourceState:
        if "text" in state.config:
            levels = _parse_levels(state.config["text"], "osm_admin_levels")
            admin = _parse_levels(state.config.get("admin_text", ""), "osm_localadmin_levels")
        else:
            levels, admin = state.config.get("levels", []), state.config.get("localadmin_levels", [])
        return SourceState(enabled=bool(levels or admin), config={"levels": levels, "localadmin_levels": admin})

    def form_values(self, state: SourceState) -> dict:
        if "text" in state.config:
            return {"osm_admin_levels": state.config["text"], "osm_localadmin_levels": state.config.get("admin_text", "")}
        join = lambda key: ", ".join(str(level) for level in state.config.get(key, []))  # noqa: E731
        return {"osm_admin_levels": join("levels"), "osm_localadmin_levels": join("localadmin_levels")}

    def describe(self, state: SourceState) -> str:
        levels, admin = state.config.get("levels", []), state.config.get("localadmin_levels", [])
        if not state.enabled or not (levels or admin):
            return "—"
        parts = (["OSM L" + ",".join(map(str, levels))] if levels else []) + (["municipalities L" + ",".join(map(str, admin))] if admin else [])
        return " + ".join(parts)
