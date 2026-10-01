import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from datamanager.models.base import Base

# queued -> running -> awaiting_review -> approved | rejected ; running -> failed
RUN_STATUSES = ("queued", "running", "awaiting_review", "approved", "rejected", "failed")


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC).replace(tzinfo=None)


class BuildRun(Base):
    """One execution of one stage. Successful runs end in `awaiting_review`; only an approved run
    counts as input for later stages (`services/runs.py`)."""

    __tablename__ = "build_run"

    id: Mapped[int] = mapped_column(primary_key=True)
    stage_key: Mapped[str] = mapped_column(String(50), nullable=False)
    config_profile_id: Mapped[int | None] = mapped_column(
        ForeignKey("config_profile.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(20), default="queued", nullable=False)
    params_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    # source_key -> download_record.id the user pinned for this run (empty = latest approved)
    input_versions_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    report_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # validation report shown for review
    rq_job_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    triggered_by: Mapped[str] = mapped_column(String(80), default="operator", nullable=False)
    error_type: Mapped[str | None] = mapped_column(String(80), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    review_note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    started_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    reviewed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)

    steps: Mapped[list["BuildStep"]] = relationship(
        cascade="all, delete-orphan", order_by="BuildStep.sequence_index", back_populates="run"
    )


class BuildStep(Base):
    __tablename__ = "build_step"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("build_run.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    sequence_index: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="running", nullable=False)  # running|success|failed|skipped
    started_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    finished_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    progress_current: Mapped[int | None] = mapped_column(Integer, nullable=True)
    progress_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    progress_message: Mapped[str | None] = mapped_column(String(300), nullable=True)
    error_type: Mapped[str | None] = mapped_column(String(80), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    run: Mapped[BuildRun] = relationship(back_populates="steps")
