from flask import Blueprint, abort, redirect, render_template, request, url_for

from datamanager.db import SessionLocal
from datamanager.errors import ValidationError
from datamanager.models import DownloadRecord
from datamanager.services import config_profiles as profiles
from datamanager.services import downloads, resolve, runs
from datamanager.services import settings as settings_service
from datamanager.stages.border import plan_inputs as border_inputs
from datamanager.stages.download_osm import planned_paths
from datamanager.stages.download_pelias import planned as pelias_planned
from datamanager.stages.download_planet import PLANET_KEY
from datamanager.stages.download_srtm import planned_tiles
from datamanager.stages.download_tiles import TILES_KEY
from datamanager.stages.osm_extract import plan_inputs
from datamanager.stages.pelias import plan_inputs as pelias_inputs
from datamanager.stages.valhalla import plan_inputs as valhalla_inputs
from datamanager.stages.wof_patch import plan_inputs as wof_patch_inputs
from datamanager.stages import status as stage_status

bp = Blueprint(
    "build", __name__, url_prefix="/build", template_folder="templates", static_folder="static"
)

LAST_CONFIG_COOKIE = "last_config"  # written by the Configure section
ACTIVE = ("queued", "running")
RUNS_PER_PAGE = (10, 25, 50, 100)  # choices of the runs overview; the first is the default

# key, title, what it does. Each is runnable on its own once its inputs are approved.
STAGES = (
    ("download-planet", "Download planet",
     "Fetches the OSM planet (URL in Settings → Download sources) as a new version, unless there is no newer one. "
     "At most the newest two versions are kept. Nothing downstream uses it until you approve the run."),
    ("extract-countries", "Extract countries",
     "Cuts every core and overlap country of the configuration out of the approved planet in one pass, with the "
     "Geofabrik country polygons. Countries already extracted from this planet version are not extracted again."),
    ("download-osm", "Download OSM extracts (Geofabrik)",
     "Alternative to the planet route (Settings → Country PBF source = geofabrik): fetches the Geofabrik extract of every "
     "core and overlap country as a new dated version."),
    ("polygons", "Region polygons",
     "Derives each region's core, 100 km overlap zone (true distance), the 10 km border zones between "
     "bordering regions and the keep-polygons of carved countries, as .poly files. Needs only the configuration."),
    ("download-tiles", "Download tiles (Protomaps)",
     "Fetches the newest daily Protomaps planet build (Z0–15, about 140 GB, Protomaps basemap schema) as a new version, "
     "unless there is no newer one, and checks its header and layers. Needs no configuration; the newest build "
     "is kept (Settings → Download sources)."),
    ("border", "Border crossings",
     "Outlines every region's country area (core and extended) and finds the motorway to secondary roads that cross "
     "the border between bordering regions, as one CSV per pair. Needs the approved region PBFs and border polygons."),
    ("download-srtm", "Download elevation (SRTM)",
     "Fetches the 1° SRTM tiles every region needs (core and overlap) as versioned downloads and unpacks them for "
     "Valhalla. Tiles that did not change upstream are not fetched again; open-sea tiles do not exist and are skipped."),
    ("valhalla", "Valhalla routing data",
     "Builds each region's routing data (tiles.tar, admin and timezone databases, and the edge polylines Pelias uses) "
     "from its approved full PBF and the approved elevation tiles. Needs Valhalla compiled under Settings → Tools; "
     "regions whose inputs did not change are skipped."),
    ("download-pelias-data", "Download Pelias data",
     "Fetches what the Pelias import loads as versioned downloads: the Placeholder store, GeoNames, the Who's On First bundles "
     "of every core and overlap country, GeoNames postal files, official locality polygons and the OpenAddresses sources "
     "(needs the token of Settings → Pelias, otherwise those are skipped). Unchanged sources are not fetched again."),
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
    ("styles", "Map styles",
     "Writes style-light.json and style-dark.json of the configuration (base styles and label zooms of the Style tab, "
     "URLs of Settings → Public URLs). Needs only the configuration."),
    ("osm-extract", "OSM extract",
     "Builds each region's core PBF (core countries merged, carved ones clipped) and full PBF (core plus the "
     "overlap countries clipped to the overlap polygon). Needs the approved country PBFs and approved polygons; "
     "regions can be built one at a time."),
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
    hours = 48 if run is not None and run.stage_key == "pelias" else 6  # a Pelias import of a large region takes hours
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
        elif key == "wof-patch":
            problems = wof_patch_inputs(session, resolved).problems
            states[key] = problems[0] + (f" (+{len(problems) - 1} more)" if len(problems) > 1 else "") if problems else None
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


@bp.get("/")
def index():
    session = SessionLocal()
    config = _current_config()
    resolved = resolve.to_dict(resolve.resolve_config(session, config.id)) if config else None
    states = _stage_states(config, resolved)
    return render_template(
        "build/index.html",
        config=config,
        configs=profiles.list_profiles(session),
        stages=[st for st in STAGES if st[0] != "download-osm" or settings_service.get(session, "osm.source") == "geofabrik"],
        states=states,
        statuses=stage_status.compute(session, config.id, resolved, states) if config else {},
        planet=_planet_info(),
        tiles=_planet_info(TILES_KEY),
        region_names=[r["name"] for r in resolved["regions"] if r["core"]] if config else [],
        runs=_runs_page(session, config.id if config else None),
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
    if stage_key in ("osm-extract", "valhalla", "pelias") and request.form.getlist("regions"):
        params["regions"] = request.form.getlist("regions")
    if stage_key in ("download-planet", "download-tiles"):
        if request.form.get("use_existing"):
            params["use_existing"] = True
        if request.form.get("local_file", "").strip():
            params["local_file"] = request.form["local_file"].strip()
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
