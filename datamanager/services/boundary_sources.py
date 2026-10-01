"""Boundary-source rows for a country (see datamanager/boundary_sources/)."""

from collections.abc import Mapping

from datamanager.boundary_sources import SourceState  # noqa: F401  (re-exported)
from datamanager.models import Country
from datamanager.services import country_sources as generic
from datamanager.services.country_sources import BOUNDARY


def get_state(country: Country, key: str) -> SourceState:
    return generic.get_state(country, BOUNDARY, key)


def set_state(country: Country, key: str, state: SourceState) -> None:
    generic.set_state(country, BOUNDARY, key, state)


def ensure_defaults(country: Country, curated: Mapping) -> bool:
    return generic.ensure_defaults(country, BOUNDARY, curated)
