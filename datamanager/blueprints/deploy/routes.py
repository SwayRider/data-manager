import json
from pathlib import Path

from flask import Blueprint, abort, redirect, render_template, request, url_for

from datamanager.db import SessionLocal
from datamanager.deploy import orchestrator
from datamanager.errors import DataManagerError, DeployError
from datamanager.models import BuildRun, DeployConfig, Deployment, Package
from datamanager.services import runs

bp = Blueprint(
    "deploy", __name__, url_prefix="/deploy", template_folder="templates"
)

EXAMPLE = Path(__file__).resolve().parents[3] / "deploy-configs" / "dev-mini.example.json"


def _config_or_404(key: str) -> DeployConfig:
    row = SessionLocal().query(DeployConfig).filter_by(key=key).first()
    if row is None:
        abort(404)
    return row


def _packages(session) -> list[dict]:
    """Complete packages, newest first, for the picker; the newest verified one is the default."""
    rows = session.query(Package).filter_by(status="complete").order_by(Package.id.desc()).all()
    default = next((p.tag for p in rows if p.verified_at), None)
    return [{"p": p, "default": p.tag == default,
             "labels": [l for l in p.labels if l.origin == "user"],
             "live": sorted(l.value for l in p.labels if l.key == "live")} for p in rows]


def _history(session, config: DeployConfig | None = None, limit: int = 20) -> list[Deployment]:
    query = session.query(Deployment).order_by(Deployment.id.desc())
    if config is not None:
        query = query.filter(Deployment.deploy_config_id == config.id)
    return query.limit(limit).all()


@bp.get("/")
def index():
    session = SessionLocal()
    configs = session.query(DeployConfig).order_by(DeployConfig.key).all()
    return render_template("deploy/index.html", configs=configs, history=_history(session), keys={c.id: c.key for c in configs})


# ---- configurations -----------------------------------------------------------------------------------------------

def _form(row: DeployConfig | None, **extra):
    text = json.dumps(row.config_json, indent=2) if row else (EXAMPLE.read_text() if EXAMPLE.exists() else "{}")
    return render_template("deploy/config_form.html", row=row, config_text=extra.pop("config_text", text), **extra)


def _save(key: str, description: str, text: str, row: DeployConfig | None):
    try:
        config = json.loads(text)
        if not isinstance(config, dict):
            raise ValueError("the configuration must be a JSON object")
        saved = orchestrator.save_config(SessionLocal(), key, config, description)
    except ValueError as exc:
        return _form(row, error=f"Not valid JSON: {exc}", config_text=text, key=key, description=description), 422
    except DeployError as exc:
        SessionLocal().rollback()
        return _form(row, error=exc.message, config_text=text, key=key, description=description), 422
    return redirect(url_for("deploy.workspace", key=saved.key), code=303)


@bp.get("/new")
def config_new():
    return _form(None, key="", description="")


@bp.post("/new")
def config_create():
    key = request.form.get("key", "").strip()
    if SessionLocal().query(DeployConfig).filter_by(key=key).first():
        return _form(None, error=f"A configuration named {key!r} exists already.", config_text=request.form.get("config", ""),
                     key=key, description=request.form.get("description", "")), 422
    return _save(key, request.form.get("description", ""), request.form.get("config", ""), None)


@bp.get("/<key>/edit")
def config_edit(key: str):
    row = _config_or_404(key)
    return _form(row, key=row.key, description=row.description)


@bp.post("/<key>/edit")
def config_update(key: str):
    row = _config_or_404(key)
    return _save(row.key, request.form.get("description", ""), request.form.get("config", ""), row)


@bp.post("/<key>/delete")
def config_delete(key: str):
    session = SessionLocal()
    row = _config_or_404(key)
    if row.deployments:
        return _form(row, error="This configuration has deployments in its history; it cannot be deleted.", key=row.key,
                     description=row.description), 422
    session.delete(row)
    session.commit()
    return redirect(url_for("deploy.index"), code=303)


# ---- deploying ---------------------------------------------------------------------------------------------------

def _wanted_classes(row: DeployConfig) -> list[str]:
    configured = orchestrator.class_order(row)
    chosen = [c for c in request.form.getlist("classes") if c in configured]
    return chosen or configured


@bp.get("/<key>")
def workspace(key: str):
    session = SessionLocal()
    row = _config_or_404(key)
    return render_template("deploy/workspace.html", row=row, classes=orchestrator.class_order(row), packages=_packages(session),
                           history=_history(session, row), active=orchestrator.active_deployment(session, row), error=request.args.get("error"))


@bp.get("/<key>/state")
def state(key: str):
    row = _config_or_404(key)
    try:
        current = orchestrator.state(SessionLocal(), key)
        error = None
    except DataManagerError as exc:
        current, error = {}, exc.message
    except Exception as exc:  # an unreachable object store or a missing directory must not break the page
        current, error = {}, f"{type(exc).__name__}: {exc}"
    return render_template("deploy/_state.html", row=row, state=current, error=error)


@bp.post("/<key>/plan")
def plan(key: str):
    row = _config_or_404(key)
    try:
        the_plan = orchestrator.plan(SessionLocal(), key, request.form.get("package") or None, _wanted_classes(row),
                                     allow_unverified=False, drop_previous=bool(request.form.get("drop_previous")))
        error = None
    except DataManagerError as exc:
        the_plan, error = None, exc.message
    except Exception as exc:
        the_plan, error = None, f"{type(exc).__name__}: {exc}"
    active = orchestrator.active_deployment(SessionLocal(), row)
    return render_template("deploy/_plan.html", row=row, plan=the_plan, error=error, active=active)


def _start(row: DeployConfig, params: dict, config_id: int | None):
    from datamanager.blueprints.build.routes import enqueue_run

    session = SessionLocal()
    run = runs.create_run(session, "deploy", config_id, params=params, triggered_by="ui")
    runs.set_job_id(session, run, enqueue_run(run.id))
    return redirect(url_for("build.run_detail", run_id=run.id), code=303)


@bp.post("/<key>/start")
def start(key: str):
    session = SessionLocal()
    row = _config_or_404(key)
    wanted = _wanted_classes(row)
    try:
        if orchestrator.active_deployment(session, row) is not None:
            raise DeployError("A deploy to this configuration is still running.")
        drop = bool(request.form.get("drop_previous"))
        the_plan = orchestrator.plan(session, key, request.form.get("package") or None, wanted, drop_previous=drop)  # never trust the form: plan again
        if the_plan["problems"]:
            raise DeployError("; ".join(the_plan["problems"]))
        package = orchestrator.resolve_package(session, request.form.get("package") or None)
    except DataManagerError as exc:
        return redirect(url_for("deploy.workspace", key=key, error=exc.message), code=303)
    return _start(row, {"deploy_config": key, "tag": package.tag, "classes": wanted, "triggered_by": "ui",
                         "drop_previous": drop}, package.config_profile_id)


@bp.post("/<key>/rollback")
def rollback(key: str):
    session = SessionLocal()
    row = _config_or_404(key)
    wanted = _wanted_classes(row)
    try:
        if orchestrator.active_deployment(session, row) is not None:
            raise DeployError("A deploy to this configuration is still running.")
        current = orchestrator.state(session, key)
        missing = [c for c in wanted if not current.get(c, {}).get("previous")]
        if missing:
            raise DeployError(f"No previous release to roll back to for: {', '.join(missing)}")
    except DataManagerError as exc:
        return redirect(url_for("deploy.workspace", key=key, error=exc.message), code=303)
    return _start(row, {"deploy_config": key, "classes": wanted, "rollback": True, "triggered_by": "ui"}, None)
