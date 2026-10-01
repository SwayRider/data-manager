import datetime

from sqlalchemy import DateTime, ForeignKey, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from datamanager.models.base import Base


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


class StyleSettings(Base):
    """Map style choice of a configuration; rows exist only once something was saved.

    `labels_json` holds only the label min-zoom *overrides* per place class (e.g.
    {"town": 7}); classes not listed keep the base style's own value."""

    __tablename__ = "style_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    config_profile_id: Mapped[int] = mapped_column(
        ForeignKey("config_profile.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    light_style: Mapped[str] = mapped_column(String(40), nullable=False)
    dark_style: Mapped[str] = mapped_column(String(40), nullable=False)
    labels_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=_utcnow, onupdate=_utcnow, nullable=False
    )
