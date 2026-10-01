import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from datamanager.models.base import Base


class DownloadRecord(Base):
    """One immutable fetched version of a source (`services/downloads.py`).

    `source_key` names the thing (e.g. `osm:europe/belgium`), `version_label` is the UTC fetch time.
    Versions coexist; later stages use the newest *approved* one unless a version is pinned or the
    run overrides it. Records whose bytes are identical share one file (same `local_path`)."""

    __tablename__ = "download_record"
    __table_args__ = (UniqueConstraint("source_key", "version_label"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    source_key: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    version_label: Mapped[str] = mapped_column(String(20), nullable=False)  # 20260930T101500Z
    url: Mapped[str] = mapped_column(String(500), nullable=False)
    filename: Mapped[str] = mapped_column(String(200), nullable=False)
    local_path: Mapped[str] = mapped_column(String(500), nullable=False)  # relative to DATA_ROOT
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)  # sha256
    etag: Mapped[str | None] = mapped_column(String(200), nullable=True)
    upstream_modified: Mapped[str | None] = mapped_column(String(100), nullable=True)
    fetched_at: Mapped[datetime.datetime] = mapped_column(DateTime, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="fetched", nullable=False)  # fetched|approved|rejected
    pinned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    data_timestamp: Mapped[str | None] = mapped_column(String(40), nullable=True)  # OSM data date from the PBF header (planet)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("build_run.id", ondelete="SET NULL"), nullable=True)
