from collections.abc import Mapping

from datamanager.country_sources import CountrySource, SourceState

# Countries with a postal-code file at https://download.geonames.org/export/zip/
# (121 files, checked 2026-09-30). Postal codes carry the place name people write
# (e.g. BE 2491 = "Olmen"), which WOF's postalcode records lack (no coordinates).
GEONAMES_POSTAL_COUNTRIES = frozenset(
    "ad ae ai al ar as at au ax az bd be bg bm br by ca cc ch cl cn co cr cx cy cz de dk do dz ec "
    "ee es fi fk fm fo fr gb gf gg gi gl gp gs gt gu hk hm hn hr ht hu id ie im in io is it je jp "
    "ke kr li lk lt lu lv ma mc md mh mk mo mp mq mt mw mx my nc nf nl no nr nu nz pa pe pf ph pk "
    "pl pm pn pr pt pw re ro rs ru se sg si sj sk sm tc th tr ua us uy va vi wf ws yt za"
    .split()
)


def has_postal_file(iso2: str) -> bool:
    return iso2.split("-")[-1] in GEONAMES_POSTAL_COUNTRIES


class GeonamesPostalSource(CountrySource):
    """GeoNames postal codes as postcode -> place-name data. Config: {}. Off by default."""

    key = "geonames_postal"
    label = "GeoNames postal codes"
    form_template = "countries/sources/geonames_postal.html"

    def default(self, iso2: str, curated: Mapping) -> SourceState:
        return SourceState(enabled=False)

    def from_form(self, form: Mapping) -> SourceState:
        return SourceState(enabled=bool(form.get("geonames_postal_enabled")))

    def validate(self, state: SourceState, verify: bool) -> SourceState:
        return SourceState(enabled=state.enabled)

    def form_values(self, state: SourceState) -> dict:
        return {"geonames_postal_enabled": state.enabled}

    def describe(self, state: SourceState) -> str:
        return "postal" if state.enabled else "—"
