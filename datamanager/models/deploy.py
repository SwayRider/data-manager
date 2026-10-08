import datetime

from sqlalchemy import DateTime, ForeignKey, JSON, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from datamanager.models.base import Base
from datamanager.models.package import Package


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC).replace(tzinfo=None)


class DeployConfig(Base):
    """A named target environment (e.g. `dev-mini`): which driver, where the per-class roots are, which services to
    restart. Secrets are never stored, only the names of the environment variables that hold them
    (`RELEASE-CONTRACT.md` §3)."""

    __tablename__ = "deploy_config"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(60), unique=True, nullable=False)
    driver: Mapped[str] = mapped_column(String(40), nullable=False)
    description: Mapped[str] = mapped_column(String(300), default="", nullable=False)
    config_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow, nullable=False)

    deployments: Mapped[list["Deployment"]] = relationship(
        cascade="all, delete-orphan", order_by="Deployment.id", back_populates="deploy_config"
    )


class Deployment(Base):
    """One deploy (or rollback) of a package to an environment. `detail_json` holds the per-class progress
    (`{class: {status, step, previous_tag, error}}`) so a half-failed deploy can be resumed."""

    __tablename__ = "deployment"

    id: Mapped[int] = mapped_column(primary_key=True)
    # The package may be deleted from the repository later; the history keeps its tag.
    package_id: Mapped[int | None] = mapped_column(ForeignKey("package.id", ondelete="SET NULL"), nullable=True, index=True)
    package_tag: Mapped[str] = mapped_column(String(40), nullable=False)
    deploy_config_id: Mapped[int] = mapped_column(
        ForeignKey("deploy_config.id", ondelete="CASCADE"), nullable=False, index=True
    )
    classes_json: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="running", nullable=False)  # running|succeeded|failed|rolled_back
    previous_package_id: Mapped[int | None] = mapped_column(ForeignKey("package.id", ondelete="SET NULL"), nullable=True)
    rolled_back_from_id: Mapped[int | None] = mapped_column(
        ForeignKey("deployment.id", ondelete="SET NULL"), nullable=True
    )
    started_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    finished_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    triggered_by: Mapped[str] = mapped_column(String(80), default="operator", nullable=False)
    build_run_id: Mapped[int | None] = mapped_column(ForeignKey("build_run.id", ondelete="SET NULL"), nullable=True)
    detail_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    package: Mapped[Package | None] = relationship(foreign_keys=[package_id])
    previous_package: Mapped[Package | None] = relationship(foreign_keys=[previous_package_id])
    deploy_config: Mapped[DeployConfig] = relationship(back_populates="deployments")
