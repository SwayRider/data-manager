"""Build runs: creation, step/progress bookkeeping for the worker, and the manual review gate.

A run that finished cleanly is `awaiting_review`; approving it is what makes its outputs eligible as
input for later stages (downloads: `services/downloads.py`)."""
import datetime

from sqlalchemy.orm import Session

from datamanager.errors import DataManagerError, ValidationError
from datamanager.models import BuildRun, BuildStep


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC).replace(tzinfo=None)


def create_run(
    session: Session,
    stage_key: str,
    config_id: int | None,
    params: dict | None = None,
    input_versions: dict | None = None,
    triggered_by: str = "operator",
) -> BuildRun:
    run = BuildRun(
        stage_key=stage_key,
        config_profile_id=config_id,
        params_json=params or {},
        input_versions_json=input_versions or {},
        triggered_by=triggered_by,
    )
    session.add(run)
    session.commit()
    return run


def get_run(session: Session, run_id: int) -> BuildRun | None:
    return session.get(BuildRun, run_id)


def list_runs(session: Session, config_id: int | None = None, limit: int = 50) -> list[BuildRun]:
    query = session.query(BuildRun)
    if config_id is not None:
        query = query.filter(BuildRun.config_profile_id == config_id)
    return query.order_by(BuildRun.id.desc()).limit(limit).all()


def set_job_id(session: Session, run: BuildRun, job_id: str) -> None:
    run.rq_job_id = job_id
    session.commit()


def mark_running(session: Session, run: BuildRun) -> None:
    run.status = "running"
    run.started_at = _now()
    session.commit()


def finish(session: Session, run: BuildRun, report: dict | None) -> None:
    run.status = "awaiting_review"
    run.report_json = report or {}
    run.finished_at = _now()
    session.commit()


def fail(session: Session, run: BuildRun, error: Exception, report: dict | None = None) -> None:
    session.rollback()
    run.status = "failed"
    run.error_type = error.error_type if isinstance(error, DataManagerError) else type(error).__name__
    run.error_message = str(error)
    if report is not None:
        run.report_json = report
    run.finished_at = _now()
    for step in run.steps:
        if step.status == "running":
            step.status, step.finished_at = "failed", run.finished_at
            step.error_type, step.error_message = run.error_type, run.error_message
    session.commit()


class Recorder:
    """Turns the runner's `step_cb`/`progress_cb` calls into `build_step` rows."""

    def __init__(self, session: Session, run: BuildRun):
        self.session, self.run = session, run
        self.current: BuildStep | None = None

    def step(self, name: str) -> None:
        self._close("success")
        self.current = BuildStep(run_id=self.run.id, name=name[:200], sequence_index=len(self.run.steps))
        self.run.steps.append(self.current)
        self.session.commit()

    def progress(self, current: int, total: int, message: str) -> None:
        if self.current is None:
            self.step(message or "run")
        self.current.progress_current, self.current.progress_total = current, total
        self.current.progress_message = (message or "")[:300]
        self.session.commit()

    def close(self) -> None:
        self._close("success")

    def _close(self, status: str) -> None:
        if self.current is not None and self.current.status == "running":
            self.current.status, self.current.finished_at = status, _now()
            self.session.commit()


def _review(session: Session, run: BuildRun, status: str, note: str) -> None:
    from datamanager.services import assets, downloads  # local: they do not know about runs

    if run.status != "awaiting_review":
        raise ValidationError(f"Run {run.id} is {run.status}, only runs awaiting review can be approved or rejected.")
    run.status, run.reviewed_at, run.review_note = status, _now(), note.strip()[:500] or None
    downloads.apply_review(session, run, approved=status == "approved")
    assets.apply_review(session, run, approved=status == "approved")
    session.commit()


def approve(session: Session, run: BuildRun, note: str = "") -> None:
    _review(session, run, "approved", note)


def reject(session: Session, run: BuildRun, note: str = "") -> None:
    _review(session, run, "rejected", note)
