import os
import shutil
from pathlib import Path

from flask import Blueprint, abort, redirect, render_template, request, url_for

from datamanager.config import config
from datamanager.db import SessionLocal
from datamanager.errors import DataManagerError, PackageError
from datamanager.models import ConfigProfile, Package
from datamanager.services import cleanup, packages, runs
from datamanager.services.cleanup_categories import CATEGORIES

bp = Blueprint(
    "repo", __name__, url_prefix="/repo", template_folder="templates"
)


def _filesystem(path: Path) -> tuple[str, str]:
    """(mount point, filesystem type) of the longest mount prefix of `path`; blank when unknown."""
    best = ("", "")
    try:
        target = str(path.resolve())
        for line in Path("/proc/mounts").read_text().splitlines():
            _, mount, fstype = line.split()[:3]
            mount = mount.replace("\\040", " ")
            if (target == mount or target.startswith(mount.rstrip("/") + "/")) and len(mount) >= len(best[0]):
                best = (mount, fstype)
    except OSError:
        pass
    return best


def _repo_status() -> dict:
    path = config.package_root
    exists = path.exists()
    probe = path if exists else path.parent
    status = {"path": str(path), "exists": exists, "warnings": []}
    if not probe.exists():
        status["warnings"].append("The folder and its parent do not exist.")
        return status
    usage = shutil.disk_usage(probe)
    status.update(free=usage.free, total=usage.total)
    status["mount"], status["fstype"] = _filesystem(probe)
    if not os.access(probe, os.W_OK | os.X_OK):
        status["warnings"].append("data-manager cannot write here: check the owner, group and ACLs of the folder.")
    try:
        if os.stat(probe).st_dev == os.stat(config.DATA_ROOT).st_dev:
            status["warnings"].append("The repository is on the same filesystem as DATA_ROOT: it does not save any SSD space.")
    except OSError:
        pass
    return status


def _parse_labels(text: str) -> dict[str, str]:
    labels = {}
    for part in text.replace(",", "\n").splitlines():
        part = part.strip()
        if part:
            key, _, value = part.partition("=")
            labels[key.strip()[:100]] = value.strip()[:300]
    return labels


def _form_plan():
    """The package the new-package form describes: (config id, classes, labels, note, force) and its plan."""
    session = SessionLocal()
    config_id = request.form.get("config", type=int)
    if config_id is None or session.get(ConfigProfile, config_id) is None:
        return None, None, "Choose a configuration."
    classes = [c for c in request.form.getlist("classes") if c in packages.CLASSES] or list(packages.CLASSES)
    force = bool(request.form.get("force"))
    try:
        the_plan = packages.plan(session, config_id, classes, check_status=not force)
    except DataManagerError as exc:
        return None, None, exc.message
    return (config_id, classes, force), the_plan, None


def _index_context(**extra):
    session = SessionLocal()
    config_id = request.args.get("config", type=int)
    label = (request.args.get("label") or "").strip().lower()
    klass = request.args.get("class") or ""
    rows = []
    for package in session.query(Package).order_by(Package.id.desc()):
        if config_id and package.config_profile_id != config_id:
            continue
        if label and not any(label in f"{l.key}={l.value}".lower() for l in package.labels):
            continue
        classes = sorted({i.class_ for i in package.items})
        if klass and klass not in classes:
            continue
        cfg = session.get(ConfigProfile, package.config_profile_id) if package.config_profile_id else None
        rows.append({"p": package, "classes": classes, "config": cfg.name if cfg else "-",
                     "user_labels": [l for l in package.labels if l.origin == "user"]})
    return {"repo": _repo_status(), "rows": rows, "configs": session.query(ConfigProfile).order_by(ConfigProfile.name).all(),
            "classes": packages.CLASSES, "filters": {"config": config_id, "label": request.args.get("label", ""), "class": klass},
            "keep": packages.settings_keep(session), **extra}


@bp.get("/")
def index():
    return render_template("repo/index.html", **_index_context())


@bp.post("/plan")
def plan_preview():
    chosen, the_plan, error = _form_plan()
    return render_template("repo/_plan.html", plan=the_plan, error=error, free=_repo_status().get("free"))


@bp.post("/create")
def create():
    chosen, the_plan, error = _form_plan()
    if error or the_plan.problems:
        return render_template("repo/_plan.html", plan=the_plan, error=error, free=_repo_status().get("free")), 422
    config_id, classes, force = chosen
    from datamanager.blueprints.build.routes import enqueue_run

    session = SessionLocal()
    params = {"classes": classes, "labels": _parse_labels(request.form.get("labels", "")), "note": request.form.get("note", "")[:500],
              "force": force, "created_by": "ui"}
    run = runs.create_run(session, "package", config_id, params=params, triggered_by="ui")
    runs.set_job_id(session, run, enqueue_run(run.id))
    return redirect(url_for("build.run_detail", run_id=run.id), code=303)


def _package_or_404(tag: str) -> Package:
    package = SessionLocal().query(Package).filter_by(tag=tag).first()
    if package is None:
        abort(404)
    return package


def _detail_context(package: Package, **extra):
    session = SessionLocal()
    by_class: dict[str, list] = {}
    for item in package.items:
        by_class.setdefault(item.class_, []).append(item)
    cfg = session.get(ConfigProfile, package.config_profile_id) if package.config_profile_id else None
    return {"package": package, "by_class": by_class, "config": cfg,
            "auto": [l for l in package.labels if l.origin == "auto"], "user": [l for l in package.labels if l.origin == "user"],
            **extra}


@bp.get("/<tag>")
def detail(tag: str):
    package = _package_or_404(tag)
    return render_template("repo/detail.html", **_detail_context(package, **_cleanup_context(package, None)))


@bp.post("/<tag>/labels")
def labels(tag: str):
    package = _package_or_404(tag)
    packages.edit_labels(SessionLocal(), tag, labels=_parse_labels(request.form.get("labels", "")),
                         note=request.form.get("note", ""), protected=bool(request.form.get("protected")))
    return redirect(url_for("repo.detail", tag=package.tag), code=303)


@bp.post("/<tag>/verify")
def verify(tag: str):
    package = _package_or_404(tag)
    from datamanager.blueprints.build.routes import enqueue_run

    session = SessionLocal()
    run = runs.create_run(session, "package-verify", package.config_profile_id, params={"tag": tag}, triggered_by="ui")
    runs.set_job_id(session, run, enqueue_run(run.id))
    return redirect(url_for("build.run_detail", run_id=run.id), code=303)


@bp.post("/<tag>/delete")
def delete(tag: str):
    package = _package_or_404(tag)
    try:
        packages.delete(SessionLocal(), tag)
    except PackageError as exc:
        return render_template("repo/detail.html", **_detail_context(package, error=exc.message, **_cleanup_context(package, None))), 422
    return redirect(url_for("repo.index"), code=303)


# ---- cleanup dialog ------------------------------------------------------------------------------------------------

def _cleanup_context(package: Package, checked: set[str] | None, result=None, error: str | None = None) -> dict:
    """Everything the cleanup box shows; the box is only offered after a clean verify."""
    context = {"cleanup_result": result, "cleanup_error": error, "categories": CATEGORIES, "cleanup_ready": package.verified_at is not None}
    if not context["cleanup_ready"]:
        return context
    session = SessionLocal()
    try:
        full = cleanup.plan(session, package.tag)
    except PackageError as exc:
        return {**context, "cleanup_ready": False, "cleanup_error": exc.message}
    ticked = cleanup.defaults(session)
    if checked is None:
        checked = {k for k, on in ticked.items() if on}
    per_category = full.by_category()
    context.update(per_category=per_category, checked=checked, busy=full.busy,
                   selected_bytes=sum(per_category[k]["bytes"] for k in checked), selected_count=sum(per_category[k]["count"] for k in checked))
    return context


def _checked() -> set[str]:
    return {c for c in request.form.getlist("category") if c in {x.key for x in CATEGORIES}}


@bp.post("/<tag>/cleanup/preview")
def cleanup_preview(tag: str):
    package = _package_or_404(tag)
    return render_template("repo/_cleanup.html", package=package, **_cleanup_context(package, _checked()))


@bp.post("/<tag>/cleanup/apply")
def cleanup_apply(tag: str):
    package = _package_or_404(tag)
    chosen = _checked()
    session = SessionLocal()
    try:
        result = cleanup.apply(session, tag, sorted(chosen))
        error = None
    except PackageError as exc:
        session.rollback()
        result, error = None, exc.message
    return render_template("repo/_cleanup.html", package=package, **_cleanup_context(package, chosen, result, error))
