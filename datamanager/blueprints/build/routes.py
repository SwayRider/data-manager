from flask import Blueprint, abort, redirect, render_template, request, url_for

from datamanager.db import SessionLocal
from datamanager.errors import ValidationError
from datamanager.models import DownloadRecord
from datamanager.services import config_profiles as profiles
from datamanager.services import downloads, resolve, runs
from datamanager.services import settings as settings_service
from datamanager.stages.download_osm import planned_paths
from datamanager.stages.download_planet import PLANET_KEY
from datamanager.stages.download_srtm import planned_tiles
from datamanager.stages.download_tiles import TILES_KEY
from datamanager.stages.osm_extract import plan_inputs
from datamanager.stages import status as stage_status

bp = Blueprint(
    "build", __name__, url_prefix="/build", template_folder="templates", static_folder="static"
)

LAST_CONFIG_COOKIE = "last_config"  # written by the Configure section
ACTIVE = ("queued", "running")

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
    ("download-srtm", "Download elevation (SRTM)",
     "Fetches the 1° SRTM tiles every region needs (core and overlap) as versioned downloads and unpacks them for "
     "Valhalla. Tiles that did not change upstream are not fetched again; open-sea tiles do not exist and are skipped."),
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

    return queue.enqueue("datamanager.jobs.tasks.run_stage", run_id, job_timeout=6 * 3600).id


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
        elif key == "download-srtm" and not planned_tiles(resolved):
            states[key] = "No region has an SRTM box."
        elif key == "polygons" and not any(r["core"] for r in resolved["regions"]):
            states[key] = "No region has a core country."
        elif key == "osm-extract":
            _, problems = plan_inputs(session, config.id, resolved)
            states[key] = problems[0] + (f" (+{len(problems) - 1} more)" if len(problems) > 1 else "") if problems else None
        else:
            states[key] = None
    return states


def _planet_info(key: str = PLANET_KEY):
    record = downloads.resolve_version(SessionLocal(), key)
    return {"version": record.version_label, "data": record.data_timestamp, "size": record.size_bytes} if record else None


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
        runs=runs.list_runs(session, config.id if config else None),
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
    if stage_key == "osm-extract" and request.form.getlist("regions"):
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

def _downloads_context(cleanup=None, error=None):
    session = SessionLocal()
    sources = []
    for key in downloads.source_keys(session):
        found = downloads.versions(session, key)
        selected = downloads.resolve_version(session, key)
        sources.append({"key": key, "versions": found, "selected_id": selected.id if selected else None})
    return {"sources": sources, "cleanup": cleanup, "error": error, "keep": downloads.KEEP_DEFAULT}


def _render_downloads(status=200, **kwargs):
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


@bp.post("/downloads/cleanup")
def cleanup():
    keep = request.form.get("keep", type=int) or downloads.KEEP_DEFAULT
    confirmed = request.form.get("confirm") == "1"
    try:
        plan = downloads.cleanup(SessionLocal(), keep=keep, dry_run=not confirmed)
    except ValidationError as exc:
        return _render_downloads(422, error=exc.message)
    return _render_downloads(cleanup={**plan, "keep": keep})
