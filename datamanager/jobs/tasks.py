"""Job bodies submitted to RQ. Kept as plain functions (RQ's convention) so
they can be enqueued by key path and imported directly by tests for
in-process comparison against the RQ-executed result.
"""

from pathlib import Path

from datamanager.config import config
from datamanager.db import SessionLocal
from datamanager.errors import StageFailedError
from datamanager.services import resolve, runs
from datamanager.stages import builtin  # noqa: F401  (registers the stages)
from datamanager.stages.contract import StageRunContext
from datamanager.stages.noop import NoOpStage
from datamanager.stages.registry import default_registry


def ping() -> str:
    return "pong"


def run_noop_stage(run_id: str) -> dict:
    stage = NoOpStage()
    context = StageRunContext(
        run_id=run_id,
        work_dir=Path(config.DATA_ROOT) / "work" / run_id,
    )
    result = stage.run(context)
    return {"status": result.status, "produced_paths": result.produced_paths}


def run_stage(run_id: int) -> dict:
    """Execute a `build_run`: records steps and progress, stores the validation report and leaves the
    run `awaiting_review` (or `failed`, re-raising so RQ's failed registry sees it too)."""
    session = SessionLocal()
    run = runs.get_run(session, run_id)
    recorder = runs.Recorder(session, run)
    try:
        runs.mark_running(session, run)
        runner = default_registry.get(run.stage_key)()
        resolved = resolve.to_dict(resolve.resolve_config(session, run.config_profile_id)) if run.config_profile_id else None
        context = StageRunContext(
            run_id=str(run.id),
            work_dir=Path(config.DATA_ROOT) / "work" / str(run.id),
            config_resolved=resolved,
            config_id=run.config_profile_id,
            params=dict(run.params_json),
            input_versions=dict(run.input_versions_json),
            step_cb=recorder.step,
            progress_cb=recorder.progress,
        )
        result = runner.run(context)
        recorder.close()
    except Exception as exc:
        runs.fail(session, run, exc)
        raise
    if result.status == "failed":
        runs.fail(session, run, StageFailedError(result.report.get("error", "Stage reported failure")), result.report)
        return {"status": "failed", "run_id": run.id}
    runs.finish(session, run, result.report)
    return {"status": run.status, "run_id": run.id}


def build_tool(key: str) -> dict:
    """Build a tool that data-manager builds itself (Settings → Tools): Valhalla or the Pelias importers."""
    from datamanager.services.built_tools import BUILDERS

    if key not in BUILDERS:
        raise ValueError(f"No build for tool {key}")
    builder = BUILDERS[key]
    try:
        return builder.build(SessionLocal())
    except Exception as exc:  # builders record BuildError themselves; anything else must not leave the state "building"
        if builder.read_state().get("status") in builder.ACTIVE:
            builder._write_state({**builder.read_state(), "status": "failed", "message": f"{type(exc).__name__}: {exc}"[:300]})
        raise
