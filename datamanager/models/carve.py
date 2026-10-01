import datetime

from sqlalchemy import DateTime, ForeignKey, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from datamanager.models.base import Base


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


class CountryCarve(Base):
    """The part of a country a configuration keeps (a drawn polygon), e.g. only European
    France without French Guiana. Everything derived from geometry (overlap, borders, tile
    coverage) uses the country clipped to this polygon."""

    __tablename__ = "country_carve"
    __table_args__ = (UniqueConstraint("config_profile_id", "country_iso"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    config_profile_id: Mapped[int] = mapped_column(
        ForeignKey("config_profile.id", ondelete="CASCADE"), nullable=False
    )
    country_iso: Mapped[str] = mapped_column(
        String(10), ForeignKey("country.iso2", ondelete="CASCADE"), nullable=False
    )
    keep_geojson: Mapped[dict] = mapped_column(JSON, nullable=False)  # Polygon / MultiPolygon geometry
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=_utcnow, onupdate=_utcnow, nullable=False
    )
