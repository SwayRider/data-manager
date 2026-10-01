from collections.abc import Mapping

from datamanager.address_sources.base import AddressSource, SourceState

# Countries in Overture Maps' address theme, per https://docs.overturemaps.org/guides/addresses/
# (checked 2026-09-30; coverage inside a country can be partial, e.g. DE, US, TW).
OVERTURE_ADDRESS_COUNTRIES = frozenset(
    "at au be br ca ch cl co cz de dk ee es fi fo fr gl hk hr is it jp li lt lu lv mx nc nl no nz "
    "pf pl pt rs sg si sk tw us uy".split()
)


def is_covered(iso2: str) -> bool:
    return iso2.split("-")[-1] in OVERTURE_ADDRESS_COUNTRIES


class OvertureSource(AddressSource):
    """Overture Maps addresses, downloaded by country bbox in the Pelias stage. Config: {}."""

    key = "overture"
    label = "Overture Maps"
    form_template = "countries/sources/overture.html"

    def default(self, iso2: str, curated: Mapping) -> SourceState:
        return SourceState(enabled=is_covered(iso2))

    def from_form(self, form: Mapping) -> SourceState:
        return SourceState(enabled=bool(form.get("overture_enabled")))

    def validate(self, state: SourceState, verify: bool) -> SourceState:
        return SourceState(enabled=state.enabled)

    def form_values(self, state: SourceState) -> dict:
        return {"overture_enabled": state.enabled}

    def describe(self, state: SourceState) -> str:
        return "✓" if state.enabled else "—"
