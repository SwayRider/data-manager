"""Registry of address sources a country can be configured with.

To add a source: implement AddressSource in a new module here, add a modal
partial under blueprints/countries/templates/countries/sources/, and list it below.
"""

from datamanager.address_sources.base import AddressSource, SourceState
from datamanager.address_sources.openaddresses import OpenAddressesSource
from datamanager.address_sources.overture import OvertureSource

SOURCES: dict[str, AddressSource] = {s.key: s for s in (OpenAddressesSource(), OvertureSource())}

__all__ = ["AddressSource", "SourceState", "SOURCES"]
