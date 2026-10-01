"""Address-source rows for a country (see datamanager/address_sources/)."""

from collections.abc import Mapping

from datamanager.address_sources import SourceState
from datamanager.models import Country
from datamanager.services import country_sources as generic
from datamanager.services.country_sources import ADDRESS


def get_row(country: Country, key: str):
    return generic.get_row(country, ADDRESS, key)


def get_state(country: Country, key: str) -> SourceState:
    return generic.get_state(country, ADDRESS, key)


def set_state(country: Country, key: str, state: SourceState) -> None:
    generic.set_state(country, ADDRESS, key, state)


def ensure_defaults(country: Country, curated: Mapping) -> bool:
    return generic.ensure_defaults(country, ADDRESS, curated)


def openaddresses_files(country: Country) -> list[str]:
    """The OpenAddresses sources to use (empty when the source is off)."""
    state = get_state(country, "openaddresses")
    return list(state.config.get("files", [])) if state.enabled else []


def set_openaddresses_files(country: Country, files: list[str]) -> None:
    set_state(country, "openaddresses", SourceState(enabled=bool(files), config={"files": files}))
