from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import ClassVar


@dataclass
class SourceState:
    """A source's settings for one country (what a country_*_source row holds)."""

    enabled: bool = False
    config: dict = field(default_factory=dict)


class CountrySource(ABC):
    """A per-country configurable data source (address source, boundary source, ...).
    Implementations live in a registry package and are stored in a country_*_source table."""

    key: ClassVar[str]
    label: ClassVar[str]
    form_template: ClassVar[str]  # partial included in the country modal

    @abstractmethod
    def default(self, iso2: str, curated: Mapping) -> SourceState:
        """Initial state for a country that has no row yet (curated = its countries.yml entry)."""

    @abstractmethod
    def from_form(self, form: Mapping) -> SourceState:
        """Raw, not yet validated, state from the submitted modal form."""

    @abstractmethod
    def validate(self, state: SourceState, verify: bool) -> SourceState:
        """Return the cleaned state or raise ValidationError(field=...). `verify` allows live checks."""

    @abstractmethod
    def form_values(self, state: SourceState) -> dict:
        """Template values used to (pre)fill this source's form fields."""

    @abstractmethod
    def describe(self, state: SourceState) -> str:
        """Short human summary for countries.md."""
