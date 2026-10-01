import datetime

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from datamanager.models.base import Base


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


class ConfigProfile(Base):
    """A named configuration (e.g. "dev-mini") that regions/borders hang off.

    Realises DESIGN.md's `config_document` as a simple mutable row for now;
    immutable revisions come once a stage consumes a resolved config.
    """

    __tablename__ = "config_profile"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100, collation="NOCASE"), unique=True, nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)

    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=_utcnow, onupdate=_utcnow, nullable=False
    )

    def __repr__(self) -> str:
        return f"<ConfigProfile {self.id} {self.name!r}>"
