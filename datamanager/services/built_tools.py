"""The tools data-manager builds itself (`ToolDef.kind == "built"`), each by its own module with the same interface:
`request`, `build`, `detect`, `read_state`, `_write_state` and `log_tail`."""
import time

from datamanager.services import pelias_build, valhalla_build

BUILDERS = {"valhalla": valhalla_build, "pelias": pelias_build}


def _live_build(key: str) -> bool:
    """True when the queue holds or runs a build job for this tool. Raises when the broker cannot be asked."""
    from datamanager.jobs.queue import queue

    job_ids = [*queue.job_ids, *queue.started_job_registry.get_job_ids()]
    for job_id in job_ids:
        job = queue.fetch_job(job_id)
        if job is not None and job.func_name.endswith("build_tool") and job.args and job.args[0] == key:
            return True
    return False


STALE_AFTER_S = 60  # a job a worker just took is briefly in neither the queue nor the started registry


def recover_stale(key: str) -> bool:
    """A build left `queued`/`building` by a job that no longer exists (worker killed, queue flushed) would block every
    new build. Marks it failed so it can be started again; leaves it alone when the state changed a moment ago or the
    broker cannot be asked."""
    builder = BUILDERS[key]
    state = builder.read_state()
    if state.get("status") not in builder.ACTIVE:
        return False
    try:
        if time.time() - builder.state_file().stat().st_mtime < STALE_AFTER_S:
            return False
    except OSError:
        return False
    try:
        if _live_build(key):
            return False
    except Exception:
        return False
    builder._write_state({**state, "status": "failed", "message": "The build was interrupted (no job is running): press Build again."})
    return True
