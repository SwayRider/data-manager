from collections.abc import Mapping
from urllib.parse import urlparse

import requests

from datamanager.country_sources import CountrySource, SourceState
from datamanager.errors import ValidationError

FORMATS = ("geojson", "ogc-api", "shapefile-zip")
CHECK_TIMEOUT = 10


def url_reachable(url: str) -> bool:
    """HEAD, falling back to a 1-byte ranged GET for servers that reject HEAD.
    Raises requests.RequestException if the server cannot be reached at all."""
    response = requests.head(url, allow_redirects=True, timeout=CHECK_TIMEOUT)
    if response.status_code < 400:
        return True
    response = requests.get(
        url, headers={"Range": "bytes=0-0"}, stream=True, allow_redirects=True, timeout=CHECK_TIMEOUT
    )
    response.close()
    return response.status_code < 400


class OfficialPolygonsSource(CountrySource):
    """Locality polygons downloaded from an official national source.

    Config: {"url", "format": geojson|ogc-api|shapefile-zip, "name_field"}; enabled iff url set.
    """

    key = "official_polygons"
    label = "Official polygons"
    form_template = "countries/sources/official_polygons.html"

    def default(self, iso2: str, curated: Mapping) -> SourceState:
        return SourceState(enabled=False, config={"url": "", "format": FORMATS[0], "name_field": ""})

    def from_form(self, form: Mapping) -> SourceState:
        return SourceState(
            enabled=bool(form.get("official_url", "").strip()),
            config={
                "url": form.get("official_url", "").strip(),
                "format": form.get("official_format", FORMATS[0]),
                "name_field": form.get("official_name_field", "").strip(),
            },
        )

    def validate(self, state: SourceState, verify: bool) -> SourceState:
        config = state.config
        if not config.get("url"):
            return self.default("", {})
        parsed = urlparse(config["url"])
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValidationError("Official polygons URL must be an http(s) URL", field="official_url")
        if config.get("format") not in FORMATS:
            raise ValidationError(f"Format must be one of: {', '.join(FORMATS)}", field="official_format")
        if not config.get("name_field"):
            raise ValidationError("Name field is required (the attribute holding the place name)", field="official_name_field")
        if verify:
            try:
                ok = url_reachable(config["url"])
            except requests.RequestException:
                raise ValidationError(
                    "Could not reach the official polygons URL (tick 'Skip download checks' to save anyway)",
                    field="official_url",
                )
            if not ok:
                raise ValidationError(f"Official polygons URL is not downloadable: {config['url']}", field="official_url")
        return SourceState(enabled=True, config=dict(config))

    def form_values(self, state: SourceState) -> dict:
        config = state.config
        return {
            "official_url": config.get("url", ""),
            "official_format": config.get("format", FORMATS[0]),
            "official_name_field": config.get("name_field", ""),
        }

    def describe(self, state: SourceState) -> str:
        host = urlparse(state.config.get("url", "")).netloc
        return f"official ({host})" if state.enabled and host else "—"
