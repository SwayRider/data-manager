import datetime

from sqlalchemy import JSON, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from datamanager.models.base import Base


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


class Country(Base):
    """One row per Natural Earth country.

    bbox_json/srtm_bbox_json are auto-derived from Natural Earth geometry.
    geofabrik_path/wof_code are curated (see
    datamanager/countries/curated/); a country without a geofabrik_path is
    selectable for display but not yet buildable.
    """

    __tablename__ = "country"

    iso2: Mapped[str] = mapped_column(String(2), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)

    ne_geometry_ref: Mapped[str] = mapped_column(String(500), nullable=False)
    bbox_json: Mapped[list] = mapped_column(JSON, nullable=False)

    geofabrik_path: Mapped[str | None] = mapped_column(String(200), nullable=True)
    wof_code: Mapped[str] = mapped_column(String(10), nullable=False)
    srtm_bbox_json: Mapped[list] = mapped_column(JSON, nullable=False)

    address_sources: Mapped[list["CountryAddressSource"]] = relationship(  # noqa: F821
        back_populates="country", cascade="all, delete-orphan", order_by="CountryAddressSource.source_key"
    )

    boundary_sources: Mapped[list["CountryBoundarySource"]] = relationship(  # noqa: F821
        back_populates="country", cascade="all, delete-orphan", order_by="CountryBoundarySource.source_key"
    )

    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=_utcnow, onupdate=_utcnow, nullable=False
    )

    @property
    def is_curated(self) -> bool:
        """True once the Geofabrik path is known, i.e. the country can be built."""
        return self.geofabrik_path is not None

    def __repr__(self) -> str:
        return f"<Country {self.iso2}>"
