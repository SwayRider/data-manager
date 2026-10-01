from sqlalchemy import JSON, Boolean, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from datamanager.models.base import Base


class CountryAddressSource(Base):
    """One address source (OpenAddresses, Overture, ...) configured for a country.

    `source_key` refers to the code-side registry in datamanager/address_sources/;
    `config_json` is that source's own configuration (e.g. OpenAddresses file list).
    """

    __tablename__ = "country_address_source"
    __table_args__ = (UniqueConstraint("country_iso", "source_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    country_iso: Mapped[str] = mapped_column(
        String(10), ForeignKey("country.iso2", ondelete="CASCADE"), nullable=False
    )
    source_key: Mapped[str] = mapped_column(String(50), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    config_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    country: Mapped["Country"] = relationship(back_populates="address_sources")  # noqa: F821

    def __repr__(self) -> str:
        return f"<CountryAddressSource {self.country_iso}/{self.source_key} enabled={self.enabled}>"
