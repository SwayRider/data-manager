from flask import Blueprint, Response, abort, redirect, render_template, request, url_for

from datamanager.db import SessionLocal
from datamanager.errors import DataManagerError, PackageError, ValidationError
from datamanager.models import DownloadRecord
from datamanager.services import config_profiles as profiles
from datamanager.services import cleanup as cleanup_service
from datamanager.services import downloads, packages, resolve, runs
from datamanager.services.cleanup_categories import CATEGORIES
from datamanager.services import settings as settings_service
from datamanager.stages.border import plan_inputs as border_inputs
from datamanager.stages.download_osm import planned_paths
from datamanager.stages.download_pelias import planned as pelias_planned
from datamanager.stages.download_planet import PLANET_KEY
from datamanager.stages.download_srtm import planned_tiles
from datamanager.stages.download_tiles import TILES_KEY
from datamanager.stages.osm_extract import plan_inputs
from datamanager.stages.download_overture import plan_inputs as overture_inputs
from datamanager.stages.pelias import plan_inputs as pelias_inputs
from datamanager.stages.pelias_interpolation import plan_inputs as interpolation_inputs
from datamanager.stages.valhalla import plan_inputs as valhalla_inputs
from datamanager.stages.wof_patch import plan_inputs as wof_patch_inputs
from datamanager.stages import status as stage_status

bp = Blueprint(
    "build", __name__, url_prefix="/build", template_folder="templates", static_folder="static"
)

LAST_CONFIG_COOKIE = "last_config"  # written by the Configure section
ACTIVE = ("queued", "running")
RUNS_PER_PAGE = (10, 25, 50, 100)  # choices of the runs overview; the first is the default

# key, title, what it does. Each is runnable on its own once its inputs are approved. The order is top down by
# dependency: a stage only needs stages above it (planet → country PBFs → region PBFs → border/Valhalla → Pelias → tiles → package).
STAGES = (
    ("download-planet", "Download planet",
     "Fetches the OSM planet (URL in Settings → Download sources) as a new version, unless there is no newer one. "
     "At most the newest two versions are kept. Nothing downstream uses it until you approve the run."),
    ("download-osm", "Download OSM extracts (Geofabrik)",
     "Alternative to the planet route (Settings → Country PBF source = geofabrik): fetches the Geofabrik extract of every "
     "core and overlap country as a new dated version."),
    ("extract-countries", "Extract countries",
     "Cuts every core and overlap country of the configuration out of the approved planet in one pass, with the "
     "Geofabrik country polygons. Countries already extracted from this planet version are not extracted again."),
    ("polygons", "Region polygons",
     "Derives each region's core, 100 km overlap zone (true distance), the 10 km border zones between "
     "bordering regions and the keep-polygons of carved countries, as .poly files. Needs only the configuration."),
    ("osm-extract", "OSM extract",
     "Builds each region's core PBF (core countries merged, carved ones clipped) and full PBF (core plus the "
     "overlap countries clipped to the overlap polygon). Needs the approved country PBFs and approved polygons; "
     "regions can be built one at a time."),
    ("download-srtm", "Download elevation (SRTM)",
     "Fetches the 1° SRTM tiles every region needs (core and overlap) as versioned downloads and unpacks them for "
     "Valhalla. Tiles that did not change upstream are not fetched again; open-sea tiles do not exist and are skipped."),
    ("border", "Border crossings",
     "Outlines every region's country area (core and extended) and finds the motorway to secondary roads that cross "
     "the border between bordering regions, as one CSV per pair. Needs the approved region PBFs and border polygons."),
    ("valhalla", "Valhalla routing data",
     "Builds each region's routing data (tiles.tar, admin and timezone databases, and the edge polylines Pelias uses) "
     "from its approved full PBF and the approved elevation tiles. Needs Valhalla compiled under Settings → Tools; "
     "regions whose inputs did not change are skipped."),
    ("download-pelias-data", "Download Pelias data",
     "Fetches what the Pelias import loads as versioned downloads: the Placeholder store, GeoNames, the Who's On First bundles "
     "of every core and overlap country, GeoNames postal files, official locality polygons and the OpenAddresses sources "
     "(needs the token of Settings → Pelias, otherwise those are skipped). Unchanged sources are not fetched again."),
    ("download-overture-gtfs", "Download Overture & GTFS",
     "Fetches Overture Maps places and addresses of every region with an Overture-enabled country (bounding box of its overlap "
     "polygon, converted to CSV for the Pelias csv-importer) and the region's GTFS feeds (Configure → Transit) as versioned "
     "downloads. Needs the overturemaps tool and the approved Region polygons. A source that fails is a warning."),
    ("wof-patch", "Patch WOF localities",
     "Replaces the Who's On First locality (and municipality) polygons of every country that has a locality boundary source "
     "(right-click a country on the Configure map) by OSM administrative boundaries or an official polygon file, and writes "
     "the patched WOF database Pelias reads. Needs the approved country extracts and the approved Pelias data downloads; "
     "countries whose inputs did not change are skipped."),
    ("pelias", "Pelias index",
     "Imports each region into a temporary Elasticsearch container (data on the disk set under Settings → Pelias, removed when "
     "the container stops) from its approved PBF, edge polylines, Who's On First (patched when available), GeoNames and "
     "OpenAddresses, with the importers built under Settings → Tools. Produces the Elasticsearch snapshot, the production "
     "pelias.json and the WOF directory of the PIP service. Takes hours for a large region; regions can be built one at a time."),
    ("pelias-interpolation", "Pelias interpolation",
     "Builds each region's address interpolation databases (street.db and address.db) with the Pelias interpolation importers "
     "from its approved edge polylines, the OpenAddresses sources and the house numbers of its PBF. Needs no Elasticsearch; "
     "takes hours for a large region. Regions whose inputs did not change are skipped."),
    ("download-tiles", "Download tiles (Protomaps)",
     "Fetches the newest daily Protomaps planet build (Z0–15, about 140 GB, Protomaps basemap schema) as a new version, "
     "unless there is no newer one, and checks its header and layers. Needs no configuration; the newest build "
     "is kept (Settings → Download sources)."),
    ("styles", "Map styles",
     "Writes style-light.json and style-dark.json of the configuration (base styles and label zooms of the Style tab) as "
     "templates for tilesservice, with a style id/name (Settings) and a version that goes up when the content changes. "
     "Needs only the configuration."),
    ("package", "Package",
     "Copies the approved results of this configuration (tiles, routing, Pelias, geodata) into a new, immutable, tagged package in the "
     "package repository (Repo), with the fixed tags date and config plus your own labels. Optionally verifies the copy afterwards. "
     "Needs every chosen part approved and up to date."),
    ("package-verify", "Verify package",
     "Reads every file of a package again and compares its SHA-256 with package.json. Needed before the cleanup, and any time later "
     "to prove the package is still intact."),
    ("cleanup", "Cleanup",
     "Frees SSD space after packaging: removes the files of the categories you tick (intermediate PBFs, snapshots, source downloads, ...) "
     "once a verified package holds the results. Approved results are purged, not forgotten: their records and hashes stay."),
)


@bp.app_template_filter("filesize")
def filesize(n) -> str:
    n = float(n or 0)
    for unit in ("B", "kB", "MB", "GB"):
        if n < 1000 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000


def enqueue_run(run_id: int) -> str:
    from datamanager.jobs.queue import queue

    run = runs.get_run(SessionLocal(), run_id)
    hours = 6
    if run is not None and run.stage_key in ("pelias", "pelias-interpolation"):
        hours = 48  # a Pelias import of a large region takes hours
    elif run is not None and run.stage_key in ("package", "package-verify"):
        hours = 12  # hashing and copying a few hundred GB onto a spinning disk
    return queue.enqueue("datamanager.jobs.tasks.run_stage", run_id, job_timeout=hours * 3600).id


def _current_config():
    session = SessionLocal()
    wanted = request.args.get("config", type=int) or request.cookies.get(LAST_CONFIG_COOKIE, type=int)
    config = profiles.get_profile(session, wanted) if wanted else None
    return config or next(iter(profiles.list_profiles(session)), None)


def _stage_states(config, resolved=None) -> dict[str, str | None]:
    """stage key -> reason it cannot run now (None = runnable)."""
    if config is None:
        return {}
    session = SessionLocal()
    if resolved is None:
        resolved = resolve.to_dict(resolve.resolve_config(session, config.id))
    planet = downloads.resolve_version(session, PLANET_KEY)
    states = {}
    for key, _, _ in STAGES:
        if key == "download-osm" and not planned_paths(resolved):
            states[key] = "The configuration has no core country with a Geofabrik path."
        elif key == "extract-countries" and not planned_paths(resolved):
            states[key] = "The configuration has no core country with a Geofabrik path."
        elif key == "extract-countries" and (planet is None or planet.status != "approved"):
            states[key] = "No approved planet: run Download planet and approve it first."
        elif key == "border":
            problems = border_inputs(session, config.id, resolved).problems
            states[key] = problems[0] + (f" (+{len(problems) - 1} more)" if len(problems) > 1 else "") if problems else None
        elif key == "download-srtm" and not planned_tiles(resolved):
            states[key] = "No region has an SRTM box."
        elif key == "download-pelias-data" and not pelias_planned(session, resolved):
            states[key] = "The configuration has no core or overlap country with a Geofabrik path."
        elif key == "pelias":
            problems = pelias_inputs(session, config.id, resolved).problems
            states[key] = problems[0] + (f" (+{len(problems) - 1} more)" if len(problems) > 1 else "") if problems else None
        elif key == "download-overture-gtfs":
            problems = overture_inputs(session, config.id, resolved).problems
            states[key] = problems[0] + (f" (+{len(problems) - 1} more)" if len(problems) > 1 else "") if problems else None
        elif key == "pelias-interpolation":
            problems = interpolation_inputs(session, config.id, resolved).problems
            states[key] = problems[0] + (f" (+{len(problems) - 1} more)" if len(problems) > 1 else "") if problems else None
        elif key == "wof-patch":
            problems = wof_patch_inputs(session, resolved).problems
            states[key] = problems[0] + (f" (+{len(problems) - 1} more)" if len(problems) > 1 else "") if problems else None
        elif key == "package":
            everything = packages.plan(session, config.id, None)  # blocked only when no class has anything to package
            states[key] = None if everything.items else (everything.problems[0] if everything.problems else "Nothing to package yet.")
        elif key == "package-verify":
            states[key] = None if packages.newest(session, config.id) else "No package of this configuration yet."
        elif key == "cleanup":
            states[key] = None if packages.newest(session, config.id, verified=True) else "No verified package yet: verify one first."
        elif key == "polygons" and not any(r["core"] for r in resolved["regions"]):
            states[key] = "No region has a core country."
        elif key == "valhalla":
            problems = valhalla_inputs(session, config.id, resolved).problems
            states[key] = problems[0] + (f" (+{len(problems) - 1} more)" if len(problems) > 1 else "") if problems else None
        elif key == "osm-extract":
            _, problems = plan_inputs(session, config.id, resolved)
            states[key] = problems[0] + (f" (+{len(problems) - 1} more)" if len(problems) > 1 else "") if problems else None
        else:
            states[key] = None
    return states


def _planet_info(key: str = PLANET_KEY):
    record = downloads.resolve_version(SessionLocal(), key)
    return {"version": record.version_label, "data": record.data_timestamp, "size": record.size_bytes} if record else None


def _runs_page(session, config_id: int | None) -> dict:
    """The runs overview slice for `?page=` and `?per_page=` (newest first, 10 per page by default, page clamped to the range)."""
    per_page = request.args.get("per_page", type=int)
    per_page = per_page if per_page in RUNS_PER_PAGE else RUNS_PER_PAGE[0]
    total = runs.count_runs(session, config_id)
    pages = max(1, -(-total // per_page))
    page = min(max(request.args.get("page", 1, type=int), 1), pages)
    return {"rows": runs.list_runs(session, config_id, limit=per_page, offset=(page - 1) * per_page),
            "page": page, "pages": pages, "per_page": per_page, "total": total, "choices": RUNS_PER_PAGE}


def _package_ui(session, config) -> dict:
    """What the Package, Verify and Cleanup modals need."""
    complete = session.query(packages.Package).filter_by(config_profile_id=config.id, status="complete").order_by(packages.Package.id.desc()).all()
    return {"classes": packages.CLASSES, "class_help": packages.CLASS_HELP, "fixed": packages.default_tags(config.name),
            "packages": complete, "verified": [p for p in complete if p.verified_at], "categories": CATEGORIES}


def _problem(message: str, status: int = 422) -> Response:
    return Response(message, status=status, mimetype="text/plain")


@bp.post("/package/plan")
def package_plan():
    """Live plan inside the Package modal (htmx)."""
    from datamanager.blueprints.repo.routes import _repo_status

    session = SessionLocal()
    config = _current_config()
    if config is None:
        abort(404)
    classes = [c for c in request.form.getlist("classes") if c in packages.CLASSES] or list(packages.CLASSES)
    error = the_plan = None
    try:
        packages.check_labels(packages.parse_labels(request.form.get("labels", "")))
        the_plan = packages.plan(session, config.id, classes, check_status=not request.form.get("force"))
    except DataManagerError as exc:
        error = exc.message
    return render_template("build/_package_plan.html", plan=the_plan, error=error, free=_repo_status().get("free"))


@bp.post("/cleanup/plan")
def cleanup_plan():
    """Categories with their sizes for the chosen package inside the Cleanup modal (htmx)."""
    session = SessionLocal()
    config = _current_config()
    tag = request.form.get("tag", "")
    package = session.query(packages.Package).filter_by(tag=tag, config_profile_id=config.id if config else None).first()
    if package is None:
        return render_template("build/_cleanup_plan.html", error="Choose a verified package.", categories=CATEGORIES)
    try:
        full = cleanup_service.plan(session, tag)
    except PackageError as exc:
        return render_template("build/_cleanup_plan.html", error=exc.message, categories=CATEGORIES)
    per_category = full.by_category()
    ticked = {k for k, on in cleanup_service.defaults(session).items() if on}
    checked = {c for c in request.form.getlist("category") if c in per_category} if request.form.get("touched") else ticked
    return render_template("build/_cleanup_plan.html", error=None, categories=CATEGORIES, per_category=per_category, checked=checked,
                           busy=full.busy, selected_bytes=sum(per_category[k]["bytes"] for k in checked),
                           selected_count=sum(per_category[k]["count"] for k in checked))


@bp.get("/")
def index():
    session = SessionLocal()
    config = _current_config()
    resolved = resolve.to_dict(resolve.resolve_config(session, config.id)) if config else None
    states = _stage_states(config, resolved)
    statuses = stage_status.compute(session, config.id, resolved, states) if config else {}
    for key, status in statuses.items():  # a stage blocked by a cleaned-up input cannot be started either
        if status.state == "blocked" and not states.get(key):
            states[key] = status.detail
    return render_template(
        "build/index.html",
        config=config,
        configs=profiles.list_profiles(session),
        stages=[st for st in STAGES if st[0] != "download-osm" or settings_service.get(session, "osm.source") == "geofabrik"],
        states=states,
        statuses=statuses,
        planet=_planet_info(),
        tiles=_planet_info(TILES_KEY),
        region_names=[r["name"] for r in resolved["regions"] if r["core"]] if config else [],
        runs=_runs_page(session, config.id if config else None),
        package_ui=_package_ui(session, config) if config else None,
    )


@bp.post("/run/<stage_key>")
def start(stage_key: str):
    session = SessionLocal()
    config = _current_config()
    if config is None or stage_key not in {k for k, _, _ in STAGES}:
        abort(404)
    if _stage_states(config).get(stage_key):
        abort(422)
    params = {}
    if stage_key in ("osm-extract", "valhalla", "pelias", "pelias-interpolation", "download-overture-gtfs") and request.form.getlist("regions"):
        params["regions"] = request.form.getlist("regions")
    if stage_key in ("download-planet", "download-tiles"):
        if request.form.get("use_existing"):
            params["use_existing"] = True
        if request.form.get("local_file", "").strip():
            params["local_file"] = request.form["local_file"].strip()
    if stage_key == "package":
        classes = [c for c in request.form.getlist("classes") if c in packages.CLASSES] or list(packages.CLASSES)
        force = bool(request.form.get("force"))
        try:
            labels = packages.parse_labels(request.form.get("labels", ""))
            packages.check_labels(labels)
            problems = packages.plan(session, config.id, classes, check_status=not force).problems
        except DataManagerError as exc:
            return _problem(exc.message)
        if problems:
            return _problem("Cannot package: " + "; ".join(problems))
        params = {"classes": classes, "labels": labels, "note": request.form.get("note", "")[:500], "force": force,
                  "verify": bool(request.form.get("verify")), "created_by": "ui"}
    elif stage_key == "package-verify":
        package = session.query(packages.Package).filter_by(tag=request.form.get("tag", ""), config_profile_id=config.id, status="complete").first()
        if package is None:
            return _problem("Choose a package of this configuration.")
        params = {"tag": package.tag}
    elif stage_key == "cleanup":
        chosen = [c for c in request.form.getlist("category")]
        try:
            if not chosen:
                raise PackageError("Tick at least one category.")
            the_plan = cleanup_service.plan(session, request.form.get("tag", ""), chosen)
        except PackageError as exc:
            return _problem(exc.message)
        if the_plan.busy:
            return _problem(f"Run(s) {', '.join(str(r) for r in the_plan.busy)} are queued or running: wait for them before cleaning up.")
        params = {"tag": request.form["tag"], "categories": chosen}
    run = runs.create_run(session, stage_key, config.id, params=params)
    try:
        runs.set_job_id(session, run, enqueue_run(run.id))
    except Exception as exc:  # broker down: the run exists, visibly failed, instead of an error page
        runs.fail(session, run, exc)
    return redirect(url_for("build.run_detail", run_id=run.id))


def _run_or_404(run_id: int):
    run = runs.get_run(SessionLocal(), run_id)
    if run is None:
        abort(404)
    return run


@bp.get("/runs/<int:run_id>")
def run_detail(run_id: int):
    run = _run_or_404(run_id)
    template = "build/_run.html" if request.headers.get("HX-Request") else "build/run.html"
    return render_template(template, run=run, active=run.status in ACTIVE)


def _review(run_id: int, action):
    run = _run_or_404(run_id)
    try:
        action(SessionLocal(), run, request.form.get("note", ""))
    except ValidationError as exc:
        return render_template("build/_run.html", run=run, active=False, error=exc.message), 422
    return render_template("build/_run.html", run=run, active=False)


@bp.post("/runs/<int:run_id>/approve")
def approve(run_id: int):
    return _review(run_id, runs.approve)


@bp.post("/runs/<int:run_id>/reject")
def reject(run_id: int):
    return _review(run_id, runs.reject)


# ---- downloads: versions, pinning, cleanup -------------------------------------------------------------------------

OPEN_ROWS = 8  # a download group with at most this many versions starts expanded


def _downloads_context(cleanup=None, error=None, open_group=None):
    """Versions grouped by the kind of their source key (`osm`, `srtm`, ...): one flat row list per group."""
    session = SessionLocal()
    groups: dict[str, dict] = {}
    for key in downloads.source_keys(session):
        selected = downloads.resolve_version(session, key)
        group = groups.setdefault(key.split(":", 1)[0], {"kind": key.split(":", 1)[0], "rows": [], "sources": 0, "bytes": 0})
        group["sources"] += 1
        group.setdefault("runs", {})
        for version in downloads.versions(session, key):
            group["rows"].append({"key": key, "v": version, "in_use": selected is not None and version.id == selected.id})
            group["bytes"] += version.size_bytes or 0
            if version.run_id:
                group["runs"][version.run_id] = group["runs"].get(version.run_id, 0) + 1
    for group in groups.values():
        group["open"] = group["kind"] == open_group if open_group else len(group["rows"]) <= OPEN_ROWS
    return {"groups": list(groups.values()), "cleanup": cleanup, "error": error, "keep": downloads.KEEP_DEFAULT}


def _render_downloads(status=200, **kwargs):
    kwargs.setdefault("open_group", request.form.get("group") or request.args.get("group"))
    template = "build/_downloads.html" if request.headers.get("HX-Request") else "build/downloads.html"
    return render_template(template, **_downloads_context(**kwargs)), status


@bp.get("/downloads")
def downloads_index():
    return _render_downloads()


def _record_or_404(record_id: int) -> DownloadRecord:
    record = SessionLocal().get(DownloadRecord, record_id)
    if record is None:
        abort(404)
    return record


@bp.post("/downloads/<int:record_id>/pin")
def pin(record_id: int):
    record = _record_or_404(record_id)
    try:
        (downloads.unpin if request.form.get("pinned") == "0" else downloads.pin)(SessionLocal(), record)
    except ValidationError as exc:
        return _render_downloads(422, error=exc.message)
    return _render_downloads()


@bp.post("/downloads/<int:record_id>/delete")
def delete(record_id: int):
    record = _record_or_404(record_id)
    try:
        downloads.delete_version(SessionLocal(), record)
    except ValidationError as exc:
        return _render_downloads(422, error=exc.message)
    return _render_downloads()


@bp.post("/downloads/runs/delete")
def delete_run():
    run_id = request.form.get("run_id", type=int)
    group = request.form.get("group") or None
    if run_id is None:
        return _render_downloads(422, error="Select a run first.")
    deleted, skipped = downloads.delete_run_versions(SessionLocal(), run_id, group)
    note = f"Deleted {deleted} version(s) of run {run_id}." + (f" {skipped} pinned or in-use version(s) were kept." if skipped else "")
    return _render_downloads(error=note if skipped or not deleted else None, cleanup=None)


@bp.post("/downloads/cleanup")
def cleanup():
    keep = request.form.get("keep", type=int) or downloads.KEEP_DEFAULT
    confirmed = request.form.get("confirm") == "1"
    try:
        plan = downloads.cleanup(SessionLocal(), keep=keep, dry_run=not confirmed)
    except ValidationError as exc:
        return _render_downloads(422, error=exc.message)
    return _render_downloads(cleanup={**plan, "keep": keep})
