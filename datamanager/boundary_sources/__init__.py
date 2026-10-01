"""Registry of locality-boundary sources a country can be configured with.

With none enabled for a country, Who's On First boundaries are used unchanged
(see DESIGN.md, "Locality boundaries"). To add a source: implement CountrySource
in a new module here, add a modal partial under
blueprints/countries/templates/countries/sources/, and list it below.
"""

from datamanager.boundary_sources.geonames_postal import GeonamesPostalSource
from datamanager.boundary_sources.official_polygons import OfficialPolygonsSource
from datamanager.boundary_sources.osm_admin import OsmAdminSource
from datamanager.country_sources import CountrySource, SourceState

SOURCES: dict[str, CountrySource] = {
    s.key: s for s in (OsmAdminSource(), OfficialPolygonsSource(), GeonamesPostalSource())
}

__all__ = ["CountrySource", "SourceState", "SOURCES"]
