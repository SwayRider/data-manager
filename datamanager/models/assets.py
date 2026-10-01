import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from datamanager.models.base import Base


class Asset(Base):
    """A file a stage produced (`services/assets.py`). Like download versions, assets start `produced`
    and only become usable input for later stages when their run is approved; the current asset for a
    (configuration, type, name) is the newest approved one."""

    __tablename__ = "asset"

    id: Mapped[int] = mapped_column(primary_key=True)
    asset_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)  # e.g. overlap-polygon
    name: Mapped[str] = mapped_column(String(150), nullable=False)  # e.g. benelux-overlap
    config_profile_id: Mapped[int | None] = mapped_column(
        ForeignKey("config_profile.id", ondelete="SET NULL"), nullable=True
    )
    produced_by_run_id: Mapped[int | None] = mapped_column(ForeignKey("build_run.id", ondelete="SET NULL"), nullable=True)
    path: Mapped[str] = mapped_column(String(500), nullable=False)  # relative to DATA_ROOT
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)  # sha256
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="produced", nullable=False)  # produced|approved|rejected
    meta_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    source_download_ids: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=lambda: datetime.datetime.now(datetime.UTC).replace(tzinfo=None), nullable=False
    )
