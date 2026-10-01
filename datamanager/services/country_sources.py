"""Generic read/write of a country's per-source rows.

Address sources and boundary sources share the same shape (a row per country
and source: enabled flag + JSON config); a SourceKind says which table and
registry to use.
"""

from collections.abc import Mapping
from dataclasses import dataclass

from datamanager.address_sources import SOURCES as ADDRESS_SOURCES
from datamanager.boundary_sources import SOURCES as BOUNDARY_SOURCES
from datamanager.country_sources import CountrySource, SourceState
from datamanager.models import Country, CountryAddressSource, CountryBoundarySource


@dataclass(frozen=True)
class SourceKind:
    relation: str  # attribute on Country holding the rows
    row_cls: type
    registry: dict[str, CountrySource]


ADDRESS = SourceKind("address_sources", CountryAddressSource, ADDRESS_SOURCES)
BOUNDARY = SourceKind("boundary_sources", CountryBoundarySource, BOUNDARY_SOURCES)


def get_row(country: Country, kind: SourceKind, key: str):
    return next((r for r in getattr(country, kind.relation) if r.source_key == key), None)


def get_state(country: Country, kind: SourceKind, key: str) -> SourceState:
    """Stored state, or the source's default when the country has no row yet."""
    row = get_row(country, kind, key)
    if row is None:
        return kind.registry[key].default(country.iso2, {})
    return SourceState(enabled=row.enabled, config=dict(row.config_json or {}))


def set_state(country: Country, kind: SourceKind, key: str, state: SourceState) -> None:
    row = get_row(country, kind, key)
    if row is None:
        getattr(country, kind.relation).append(
            kind.row_cls(source_key=key, enabled=state.enabled, config_json=state.config)
        )
    else:
        row.enabled = state.enabled
        row.config_json = state.config


def ensure_defaults(country: Country, kind: SourceKind, curated: Mapping) -> bool:
    """Create rows for sources the country has none for (never touches existing rows)."""
    created = False
    for key, source in kind.registry.items():
        if get_row(country, kind, key) is None:
            set_state(country, kind, key, source.default(country.iso2, curated))
            created = True
    return created
