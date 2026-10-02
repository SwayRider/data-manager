from flask import Blueprint, abort, render_template, request

from datamanager.config import config
from datamanager.db import SessionLocal
from datamanager.errors import ValidationError
from datamanager.services import settings as settings_service
from datamanager.services import tools as tools_service
from datamanager.services import valhalla_build
from datamanager.tools import BY_KEY

bp = Blueprint("settings", __name__, url_prefix="/settings", template_folder="templates")


def tools_warning() -> str | None:
    """Sidebar warning text while a required tool is missing (uses the detection cache)."""
    try:
        missing = tools_service.problems(SessionLocal())["required"]
    except Exception:  # a broken badge must never break page rendering
        return None
    return f"Required tools not found: {', '.join(missing)}" if missing else None


def _global_context(values: dict | None = None, error: str | None = None, saved: bool = False) -> dict:
    groups = settings_service.all_values(SessionLocal())
    if values:  # keep what was typed after a validation error
        for group in groups:
            for entry in group["settings"]:
                if entry["def"].key in values:
                    entry["value"] = values[entry["def"].key]
    return {"groups": groups, "error": error, "saved": saved}


def _tools_context(error: str | None = None) -> dict:
    rows = tools_service.detect_all(SessionLocal())
    required_bad = [t.label for t, s in rows if t.required and not s.ok]
    optional_bad = [t.label for t, s in rows if not t.required and not s.ok]
    builds = {"valhalla": {**valhalla_build.read_state(), "log": valhalla_build.log_tail()}}
    building = any(s.status == "building" for _, s in rows)
    return {"rows": rows, "required_bad": required_bad, "optional_bad": optional_bad, "tools_error": error,
            "builds": builds, "building": building}


@bp.get("/")
def index():
    bootstrap = {
        "Data root": config.DATA_ROOT,
        "Database": config.DATABASE_PATH,
        "Redis": config.REDIS_URL,
    }
    return render_template("settings/index.html", bootstrap=bootstrap, **_global_context(), **_tools_context())


@bp.post("/global")
def save_global():
    values = {k: v for k, v in request.form.items() if k in settings_service.BY_KEY}
    try:
        settings_service.save(SessionLocal(), values)
    except ValidationError as exc:
        SessionLocal().rollback()
        return render_template("settings/_form.html", **_global_context(values, exc.message)), 422
    return render_template("settings/_form.html", **_global_context(saved=True))


def _tools_block(error: str | None = None, status: int = 200):
    return render_template("settings/_tools.html", **_tools_context(error)), status


@bp.post("/tools/detect")
def tools_detect_all():
    tools_service.detect_all(SessionLocal(), force=True)
    return _tools_block()


@bp.post("/tools/<key>/detect")
def tool_detect(key: str):
    if key not in BY_KEY:
        abort(404)
    tools_service.detect(SessionLocal(), key, force=True)
    return _tools_block()


@bp.post("/tools/<key>/path")
def tool_path(key: str):
    if key not in BY_KEY:
        abort(404)
    try:
        tools_service.set_path(SessionLocal(), key, request.form.get("path", ""))
    except ValidationError as exc:
        SessionLocal().rollback()
        return _tools_block(exc.message, 422)
    return _tools_block()


def enqueue_build(key: str) -> str:
    from datamanager.jobs.queue import queue

    return queue.enqueue("datamanager.jobs.tasks.build_tool", key, job_timeout=6 * 3600).id


@bp.get("/tools")
def tools_block():
    return _tools_block()


@bp.post("/tools/<key>/build")
def tool_build(key: str):
    if key not in BY_KEY or BY_KEY[key].kind != "built":
        abort(404)
    session = SessionLocal()
    try:
        valhalla_build.request(session)
    except ValidationError as exc:
        return _tools_block(exc.message, 422)
    try:
        enqueue_build(key)
    except Exception as exc:  # broker down: say so instead of leaving the row "queued"
        valhalla_build._write_state({**valhalla_build.read_state(), "status": "failed", "message": f"could not queue the build: {exc}"})
    return _tools_block()
