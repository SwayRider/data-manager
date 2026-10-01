import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from datamanager.models.base import Base


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


class Region(Base):
    """A named group of countries within a configuration, shaded in `color` on the map."""

    __tablename__ = "region"
    __table_args__ = (UniqueConstraint("config_profile_id", "name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    config_profile_id: Mapped[int] = mapped_column(
        ForeignKey("config_profile.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(100, collation="NOCASE"), nullable=False)
    color: Mapped[str] = mapped_column(String(7), nullable=False)

    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=_utcnow, onupdate=_utcnow, nullable=False
    )
    # Set whenever the overlap rows were last recomputed; NULL = never (evaluated lazily).
    overlap_evaluated_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)

    countries: Mapped[list["RegionCountry"]] = relationship(
        back_populates="region", cascade="all, delete-orphan", order_by="RegionCountry.country_iso"
    )
    overlap_rows: Mapped[list["RegionOverlap"]] = relationship(
        back_populates="region", cascade="all, delete-orphan"
    )
    openaddresses_exclusions: Mapped[list["RegionOpenAddressesExclusion"]] = relationship(
        back_populates="region", cascade="all, delete-orphan"
    )
    gtfs_feeds: Mapped[list["RegionGtfsFeed"]] = relationship(
        back_populates="region", cascade="all, delete-orphan", order_by="RegionGtfsFeed.id"
    )

    def __repr__(self) -> str:
        return f"<Region {self.id} {self.name!r}>"


class RegionCountry(Base):
    """Membership of a country in a region.

    `config_profile_id` is denormalised so the unique constraint can enforce
    "a country belongs to at most one region per configuration".
    """

    __tablename__ = "region_country"
    __table_args__ = (UniqueConstraint("config_profile_id", "country_iso"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    region_id: Mapped[int] = mapped_column(ForeignKey("region.id", ondelete="CASCADE"), nullable=False)
    config_profile_id: Mapped[int] = mapped_column(
        ForeignKey("config_profile.id", ondelete="CASCADE"), nullable=False
    )
    country_iso: Mapped[str] = mapped_column(
        String(10), ForeignKey("country.iso2", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(10), default="core", nullable=False)

    region: Mapped[Region] = relationship(back_populates="countries")

    def __repr__(self) -> str:
        return f"<RegionCountry {self.region_id}/{self.country_iso} {self.role}>"


class RegionOverlap(Base):
    """A country in the overlap of a region (within 100 km of its core).

    mode: `auto` = detected from the core geometry (recomputed by
    `services.regions.evaluate_overlap` whenever the core changes), `include` = forced in
    by the user, `exclude` = detected but dropped by the user. Effective overlap = auto + include.
    """

    __tablename__ = "region_overlap"
    __table_args__ = (UniqueConstraint("region_id", "country_iso"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    region_id: Mapped[int] = mapped_column(ForeignKey("region.id", ondelete="CASCADE"), nullable=False)
    country_iso: Mapped[str] = mapped_column(
        String(10), ForeignKey("country.iso2", ondelete="CASCADE"), nullable=False
    )
    mode: Mapped[str] = mapped_column(String(10), nullable=False)  # "auto" | "include" | "exclude"

    region: Mapped[Region] = relationship(back_populates="overlap_rows")

    def __repr__(self) -> str:
        return f"<RegionOverlap {self.region_id}/{self.country_iso} {self.mode}>"


class RegionOpenAddressesExclusion(Base):
    """An OpenAddresses file of one of the region's countries that is deselected for this
    region. Stores the *exclusions* so files added to the catalog later default to included."""

    __tablename__ = "region_openaddresses_exclusion"
    __table_args__ = (UniqueConstraint("region_id", "country_iso", "file"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    region_id: Mapped[int] = mapped_column(ForeignKey("region.id", ondelete="CASCADE"), nullable=False)
    country_iso: Mapped[str] = mapped_column(
        String(10), ForeignKey("country.iso2", ondelete="CASCADE"), nullable=False
    )
    file: Mapped[str] = mapped_column(String(300), nullable=False)

    region: Mapped[Region] = relationship(back_populates="openaddresses_exclusions")


class RegionGtfsFeed(Base):
    """A GTFS feed URL of a region (downloaded by the future Pelias stage in id order)."""

    __tablename__ = "region_gtfs_feed"
    __table_args__ = (UniqueConstraint("region_id", "url"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    region_id: Mapped[int] = mapped_column(ForeignKey("region.id", ondelete="CASCADE"), nullable=False)
    url: Mapped[str] = mapped_column(String(500), nullable=False)
    label: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)

    region: Mapped[Region] = relationship(back_populates="gtfs_feeds")
