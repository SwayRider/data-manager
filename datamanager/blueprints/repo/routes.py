import os
import shutil
from pathlib import Path

from flask import Blueprint, abort, redirect, render_template, request, url_for

from datamanager.config import config
from datamanager.db import SessionLocal
from datamanager.errors import DataManagerError, PackageError
from datamanager.models import BuildRun, ConfigProfile, Package
from datamanager.services import packages, runs

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
    recent = (session.query(BuildRun).filter(BuildRun.stage_key.in_(("package", "package-verify", "cleanup")))
              .order_by(BuildRun.id.desc()).limit(10).all())
    return {"repo": _repo_status(), "recent_runs": recent, "rows": rows, "configs": session.query(ConfigProfile).order_by(ConfigProfile.name).all(),
            "classes": packages.CLASSES, "filters": {"config": config_id, "label": request.args.get("label", ""), "class": klass},
            "keep": packages.settings_keep(session), **extra}


@bp.get("/")
def index():
    return render_template("repo/index.html", **_index_context())


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
    return render_template("repo/detail.html", **_detail_context(package, ))


@bp.post("/<tag>/labels")
def labels(tag: str):
    package = _package_or_404(tag)
    try:
        packages.edit_labels(SessionLocal(), tag, labels=packages.parse_labels(request.form.get("labels", "")),
                             note=request.form.get("note", ""), protected=bool(request.form.get("protected")))
    except PackageError as exc:
        SessionLocal().rollback()
        return render_template("repo/detail.html", **_detail_context(package, error=exc.message, )), 422
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
        return render_template("repo/detail.html", **_detail_context(package, error=exc.message, )), 422
    return redirect(url_for("repo.index"), code=303)
