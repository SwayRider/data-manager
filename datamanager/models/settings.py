import datetime

from sqlalchemy import DateTime, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from datamanager.models.base import Base


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


class GlobalSetting(Base):
    """A saved override of a global setting (`services/settings.py` holds the registry and defaults).

    Rows exist only for values that differ from the default; resetting deletes the row.
    Keys `toolpath.<tool>` hold a custom binary path for a required tool."""

    __tablename__ = "global_setting"

    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value_json: Mapped[object] = mapped_column(JSON, nullable=False)
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=_utcnow, onupdate=_utcnow, nullable=False
    )
