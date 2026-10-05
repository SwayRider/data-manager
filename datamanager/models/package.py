import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, JSON, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from datamanager.models.base import Base


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC).replace(tzinfo=None)


class Package(Base):
    """One immutable, tagged copy of the approved build output in the package repository
    (`services/packages.py`, `RELEASE-CONTRACT.md` §2). The folder's `package.json` is the source of
    truth; these rows are an index that `reindex` can rebuild."""

    __tablename__ = "package"

    id: Mapped[int] = mapped_column(primary_key=True)
    tag: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)  # r-20261012-1
    config_profile_id: Mapped[int | None] = mapped_column(
        ForeignKey("config_profile.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(20), default="building", nullable=False)  # building|complete|failed
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    path: Mapped[str] = mapped_column(String(500), nullable=False)  # absolute, inside PACKAGE_ROOT
    note: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    protected: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_by: Mapped[str] = mapped_column(String(80), default="operator", nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    build_run_id: Mapped[int | None] = mapped_column(ForeignKey("build_run.id", ondelete="SET NULL"), nullable=True)
    package_json_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    verified_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)  # last clean `verify_package`

    items: Mapped[list["PackageItem"]] = relationship(
        cascade="all, delete-orphan", order_by="PackageItem.id", back_populates="package"
    )
    labels: Mapped[list["PackageLabel"]] = relationship(
        cascade="all, delete-orphan", order_by="PackageLabel.id", back_populates="package"
    )


class PackageItem(Base):
    __tablename__ = "package_item"

    id: Mapped[int] = mapped_column(primary_key=True)
    package_id: Mapped[int] = mapped_column(ForeignKey("package.id", ondelete="CASCADE"), nullable=False, index=True)
    class_: Mapped[str] = mapped_column("class_", String(20), nullable=False)  # tiles|valhalla|pelias|geodata
    region: Mapped[str | None] = mapped_column(String(100), nullable=True)
    path: Mapped[str] = mapped_column(String(500), nullable=False)  # relative to the package folder
    kind: Mapped[str] = mapped_column(String(20), default="file", nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    asset_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    download_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    meta_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    package: Mapped[Package] = relationship(back_populates="items")


class PackageLabel(Base):
    """`origin` auto = frozen tag written at packaging time, user = editable label."""

    __tablename__ = "package_label"

    id: Mapped[int] = mapped_column(primary_key=True)
    package_id: Mapped[int] = mapped_column(ForeignKey("package.id", ondelete="CASCADE"), nullable=False, index=True)
    key: Mapped[str] = mapped_column(String(100), nullable=False)
    value: Mapped[str] = mapped_column(String(300), default="", nullable=False)
    origin: Mapped[str] = mapped_column(String(10), default="user", nullable=False)

    package: Mapped[Package] = relationship(back_populates="labels")
