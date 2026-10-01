import re
from collections.abc import Mapping

import requests

from datamanager.address_sources.base import AddressSource, SourceState
from datamanager.errors import ValidationError
from datamanager.services import openaddresses as oa_service


def parse_files(text: str) -> list[str]:
    """One source per line; extensions stripped, blanks/duplicates dropped (order kept)."""
    seen: dict[str, None] = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if re.search(r"\s", line):
            raise ValidationError(f"OpenAddresses entry contains whitespace: {line!r}", field="openaddresses_files")
        seen.setdefault(oa_service.strip_extension(line))
    return list(seen)


class OpenAddressesSource(AddressSource):
    """Config: {"files": [<source path without extension>, ...]}; enabled iff there are files."""

    key = "openaddresses"
    label = "OpenAddresses"
    form_template = "countries/sources/openaddresses.html"

    def default(self, iso2: str, curated: Mapping) -> SourceState:
        files = [oa_service.strip_extension(f) for f in curated.get("openaddresses_files", [])]
        return SourceState(enabled=bool(files), config={"files": files})

    def from_form(self, form: Mapping) -> SourceState:
        text = form.get("openaddresses_files", "")
        return SourceState(enabled=bool(text.strip()), config={"text": text})

    def validate(self, state: SourceState, verify: bool) -> SourceState:
        files = parse_files(state.config["text"]) if "text" in state.config else state.config.get("files", [])
        if files and verify:
            try:
                missing = oa_service.find_missing(files)
            except requests.RequestException:
                raise ValidationError(
                    "Could not reach OpenAddresses to verify the files (tick 'Skip download checks' to save anyway)",
                    field="openaddresses_files",
                )
            if missing:
                raise ValidationError(
                    f"Not found on OpenAddresses: {', '.join(missing)}", field="openaddresses_files"
                )
        return SourceState(enabled=bool(files), config={"files": files})

    def form_values(self, state: SourceState) -> dict:
        if "text" in state.config:  # re-render of a failed submit: keep what the user typed
            return {"openaddresses_files": state.config["text"]}
        return {"openaddresses_files": "\n".join(state.config.get("files", []))}

    def describe(self, state: SourceState) -> str:
        files = state.config.get("files", [])
        return f"{len(files)} source{'s' if len(files) != 1 else ''}" if state.enabled and files else "—"
