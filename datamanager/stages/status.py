"""Where each build stage stands for a configuration, for the colored indicators on the Build page.

Per stage: running / waiting for review / failed (from its latest run), blocked (a prerequisite is missing),
otherwise by content: not run yet, up to date, or outdated because something it was built from has changed
since (a newer approved planet, other regions or carves, other country files or polygons, ...). "Built from"
is recorded as a fingerprint in the meta of the assets a stage produces, and compared with the current one."""
import datetime
from dataclasses import dataclass

from sqlalchemy.orm import Session

from datamanager.models import Asset, BuildRun
from datamanager.services import assets, downloads
from datamanager.stages.download_osm import osm_source_key, planned_paths
from datamanager.stages.download_planet import PLANET_KEY, STALE_DAYS, _age_days
from datamanager.stages.border import border_fingerprint, plan_inputs as border_inputs
from datamanager.stages.download_pelias import planned as pelias_planned
from datamanager.services import pelias_sources
from datamanager.stages.download_srtm import planned_tiles, srtm_fingerprint
from datamanager.stages.download_tiles import STALE_DAYS as TILES_STALE_DAYS, TILES_KEY
from datamanager.stages.osm_extract import plan_inputs, region_fingerprint
from datamanager.stages.polygons import polygons_fingerprint
from datamanager.stages.styles import styles_fingerprint
from datamanager.stages.wof_patch import ASSET_TYPE as WOF_PATCH_ASSET, plan_inputs as wof_patch_inputs
from datamanager.stages.download_overture import fingerprint as overture_fingerprint, plan_inputs as overture_inputs
from datamanager.stages.pelias import ASSET_TYPES as PELIAS_ASSETS, plan_inputs as pelias_inputs
from datamanager.stages.pelias_interpolation import ASSET_TYPES as INTERPOLATION_ASSETS, plan_inputs as interpolation_inputs
from datamanager.stages.valhalla import ASSET_TYPES as VALHALLA_ASSETS, plan_inputs as valhalla_inputs

GLOBAL_STAGES = {"download-planet", "download-tiles"}  # not tied to one configuration
LABELS = {
    "ok": "Up to date",
    "outdated": "Outdated: run it again",
    "todo": "Ready, not run yet",
    "review": "Waiting for your review",
    "running": "Running",
    "failed": "Last run failed",
    "blocked": "Blocked by a prerequisite",
}


@dataclass
class StageStatus:
    state: str
    detail: str = ""
    run_id: int | None = None  # the run behind a running / review / failed state, for linking to its page

    @property
    def label(self) -> str:
        return LABELS[self.state]


def _latest_run(session: Session, key: str, config_id: int) -> BuildRun | None:
    query = session.query(BuildRun).filter(BuildRun.stage_key == key)
    if key not in GLOBAL_STAGES:
        query = query.filter(BuildRun.config_profile_id == config_id)
    return query.order_by(BuildRun.id.desc()).first()


def _planet(session) -> StageStatus:
    planet = downloads.resolve_version(session, PLANET_KEY)
    if planet is None or planet.status != "approved":
        return StageStatus("todo", "No approved planet yet.")
    age = _age_days(planet.data_timestamp)
    stamp = (planet.data_timestamp or planet.fetched_at.strftime("%Y-%m-%d"))[:10]
    if age is not None and age > STALE_DAYS:
        return StageStatus("outdated", f"Planet data of {stamp} is {age} days old; check for a newer one.")
    return StageStatus("ok", f"Planet data of {stamp}.")


def _tiles(session) -> StageStatus:
    build = downloads.resolve_version(session, TILES_KEY)
    if build is None or build.status != "approved":
        return StageStatus("todo", "No approved tiles build yet.")
    age = _age_days(build.data_timestamp)
    stamp = (build.data_timestamp or build.fetched_at.strftime("%Y-%m-%d"))[:10]
    if age is not None and age > TILES_STALE_DAYS:
        return StageStatus("outdated", f"Tiles build of {stamp} is {age} days old; check for a newer one.")
    return StageStatus("ok", f"Protomaps build of {stamp}.")


def _countries(session, resolved) -> StageStatus:
    paths = planned_paths(resolved)
    planet = downloads.resolve_version(session, PLANET_KEY)
    found = {p: assets.current(session, None, "country-pbf", p) for p in paths}
    if not any(found.values()):
        return StageStatus("todo", f"{len(paths)} countries to cut from the planet.")
    stale = [p for p, a in found.items()
             if a is None or (planet is not None and a.meta_json.get("planet_record_id") != planet.id)]
    if stale:
        return StageStatus("outdated", f"{len(stale)} of {len(paths)} countries are missing or were cut from an older planet.")
    return StageStatus("ok", f"{len(paths)} countries, cut from planet {planet.version_label if planet else '?'}.")


def _geofabrik(session, resolved) -> StageStatus:
    paths = planned_paths(resolved)
    missing = [p for p in paths if downloads.latest_approved(session, osm_source_key(p)) is None]
    if len(missing) == len(paths):
        return StageStatus("todo", f"{len(paths)} extracts to download.")
    if missing:
        return StageStatus("outdated", f"{len(missing)} of {len(paths)} extracts are not downloaded.")
    return StageStatus("ok", f"{len(paths)} extracts downloaded.")


def _polygons(session, config_id, resolved) -> StageStatus:
    newest = (
        session.query(Asset)
        .filter(Asset.config_profile_id == config_id, Asset.asset_type == "core-polygon", Asset.status == "approved")
        .order_by(Asset.id.desc()).first()
    )
    if newest is None:
        return StageStatus("todo", "No approved polygons yet.")
    if newest.meta_json.get("fingerprint") != polygons_fingerprint(resolved):
        return StageStatus("outdated", "Regions, overlap or carved countries changed since the polygons were made.")
    return StageStatus("ok", "Polygons match the current regions.")


def _srtm(session, config_id, resolved) -> StageStatus:
    names = planned_tiles(resolved)
    run = (
        session.query(BuildRun)
        .filter(BuildRun.stage_key == "download-srtm", BuildRun.config_profile_id == config_id, BuildRun.status == "approved")
        .order_by(BuildRun.id.desc()).first()
    )
    if run is None:
        return StageStatus("todo", f"{len(names)} elevation tiles to download.")
    summary = (run.report_json or {}).get("summary", {})
    if summary.get("fingerprint") != srtm_fingerprint(resolved):
        return StageStatus("outdated", "The regions need other elevation tiles than the last run fetched.")
    return StageStatus("ok", f"{summary.get('tiles', len(names))} tiles ({summary.get('missing', 0)} over open sea).")


def _pelias_data(session, config_id, resolved) -> StageStatus:
    items = pelias_planned(session, resolved)
    run = (
        session.query(BuildRun)
        .filter(BuildRun.stage_key == "download-pelias-data", BuildRun.config_profile_id == config_id, BuildRun.status == "approved")
        .order_by(BuildRun.id.desc()).first()
    )
    if run is None:
        return StageStatus("todo", f"{len(items)} source(s) to download.")
    summary = (run.report_json or {}).get("summary", {})
    if summary.get("fingerprint") != pelias_sources.fingerprint(items):
        return StageStatus("outdated", "The regions need other Pelias sources than the last run fetched.")
    return StageStatus("ok", f"{summary.get('sources', len(items))} source(s) fetched.")


def _wof_patch(session, config_id, resolved) -> StageStatus | None:
    plan = wof_patch_inputs(session, resolved)
    if plan.problems or not plan.countries:
        return None
    built, stale = [], []
    for country in plan.countries:
        current = assets.current(session, None, WOF_PATCH_ASSET, country.iso2.lower())
        if current is None:
            stale.append(f"{country.iso2} (not patched)")
        else:
            built.append(country.iso2)
            if current.meta_json.get("fingerprint") != country.fingerprint:
                stale.append(country.iso2)
    if not built:
        return StageStatus("todo", f"{len(plan.countries)} country(ies) to patch.")
    if stale:
        return StageStatus("outdated", "Needs a run: " + ", ".join(stale) + ".")
    return StageStatus("ok", f"{len(plan.countries)} country(ies) patched from the current inputs.")


def _pelias(session, config_id, resolved) -> StageStatus | None:
    plan = pelias_inputs(session, config_id, resolved)
    if plan.problems or not plan.regions:
        return None
    built, stale = [], []
    for region in plan.regions:
        current = [assets.current(session, config_id, t, region.slug) for t in PELIAS_ASSETS.values()]
        if any(a is None for a in current):
            stale.append(f"{region.name} (not imported)")
        else:
            built.append(region.name)
            if any(a.meta_json.get("fingerprint") != region.fingerprint for a in current):
                stale.append(region.name)
    if not built:
        return StageStatus("todo", f"{len(plan.regions)} region(s) to import.")
    if stale:
        return StageStatus("outdated", "Needs a run: " + ", ".join(stale) + ".")
    return StageStatus("ok", f"{len(plan.regions)} region(s) imported from the current inputs.")


def _overture(session, config_id, resolved) -> StageStatus | None:
    plan = overture_inputs(session, config_id, resolved)
    if plan.problems or not plan.regions:
        return None
    run = (
        session.query(BuildRun)
        .filter(BuildRun.stage_key == "download-overture-gtfs", BuildRun.config_profile_id == config_id, BuildRun.status == "approved")
        .order_by(BuildRun.id.desc()).first()
    )
    if run is None:
        return StageStatus("todo", f"{len(plan.regions)} region(s) to download.")
    if (run.report_json or {}).get("summary", {}).get("fingerprint") != overture_fingerprint(plan):
        return StageStatus("outdated", "The regions, their areas or GTFS feeds changed since the last run.")
    return StageStatus("ok", f"{len(plan.regions)} region(s) downloaded.")


def _pelias_interpolation(session, config_id, resolved) -> StageStatus | None:
    plan = interpolation_inputs(session, config_id, resolved)
    if plan.problems or not plan.regions:
        return None
    built, stale = [], []
    for region in plan.regions:
        current = [assets.current(session, config_id, t, region.slug) for t in INTERPOLATION_ASSETS.values()]
        if any(a is None for a in current):
            stale.append(f"{region.name} (not built)")
        else:
            built.append(region.name)
            if any(a.meta_json.get("fingerprint") != region.fingerprint for a in current):
                stale.append(region.name)
    if not built:
        return StageStatus("todo", f"{len(plan.regions)} region(s) to build.")
    if stale:
        return StageStatus("outdated", "Needs a run: " + ", ".join(stale) + ".")
    return StageStatus("ok", f"{len(plan.regions)} region(s) built from the current inputs.")


def _border(session, config_id, resolved) -> StageStatus | None:
    plan = border_inputs(session, config_id, resolved)
    if plan.problems or not plan.regions:
        return None
    newest = (
        session.query(Asset)
        .filter(Asset.config_profile_id == config_id, Asset.asset_type == "region-outline", Asset.status == "approved")
        .order_by(Asset.id.desc()).first()
    )
    if newest is None:
        return StageStatus("todo", f"{len(plan.regions)} region outline(s) and {len(plan.pairs)} border pair(s) to build.")
    stale = [r.name for r in plan.regions
             if (a := assets.current(session, config_id, "region-outline", f"{r.slug}-core")) is None or a.meta_json.get("fingerprint") != r.fingerprint]
    stale += [p.name for p in plan.pairs
              if (a := assets.current(session, config_id, "border-crossings", p.name)) is None or a.meta_json.get("fingerprint") != p.fingerprint]
    if stale:
        return StageStatus("outdated", "Needs a run: " + ", ".join(stale) + ".")
    return StageStatus("ok", f"{len(plan.regions)} outline(s) and {len(plan.pairs)} pair(s) match the current region PBFs.")


def _valhalla(session, config_id, resolved) -> StageStatus | None:
    plan = valhalla_inputs(session, config_id, resolved)
    if plan.problems or not plan.regions:
        return None
    built = [r for r in plan.regions if assets.current(session, config_id, "valhalla-tiles", r.slug) is not None]
    if not built:
        return StageStatus("todo", f"{len(plan.regions)} region(s) to build.")
    stale = [r.name for r in plan.regions
             if any((a := assets.current(session, config_id, t, r.slug)) is None or a.meta_json.get("fingerprint") != r.fingerprint
                    for t in VALHALLA_ASSETS.values())]
    if stale:
        return StageStatus("outdated", "Needs a run: " + ", ".join(stale) + ".")
    return StageStatus("ok", f"{len(plan.regions)} region(s) match the current PBFs, elevation and Valhalla build.")


def _styles(session, config_id) -> StageStatus:
    newest = (
        session.query(Asset)
        .filter(Asset.config_profile_id == config_id, Asset.asset_type == "style", Asset.status == "approved")
        .order_by(Asset.id.desc()).first()
    )
    if newest is None:
        return StageStatus("todo", "No approved style files yet.")
    if newest.meta_json.get("fingerprint") != styles_fingerprint(session, config_id):
        return StageStatus("outdated", "Styles, label zooms or public URLs changed since the style files were made.")
    return StageStatus("ok", "Style files match the Style tab.")


def _package(session, config_id) -> StageStatus:
    from datamanager.services import packages

    newest = packages.newest(session, config_id)
    if newest is None:
        return StageStatus("todo", "No package of this configuration yet.")
    stale = packages.stale_parts(session, newest)
    tail = "" if newest.verified_at else " It is not verified yet."
    if stale:
        return StageStatus("outdated", f"{newest.tag} is behind the approved results: {', '.join(stale[:4])}{' …' if len(stale) > 4 else ''}.{tail}")
    return StageStatus("ok", f"{newest.tag} holds the current approved results ({newest.size_bytes / 1e9:.0f} GB).{tail}")


def _package_verify(session, config_id) -> StageStatus:
    from datamanager.services import packages

    newest = packages.newest(session, config_id)
    if newest is None:
        return StageStatus("blocked", "No package yet.")
    if newest.verified_at is None:
        return StageStatus("todo", f"{newest.tag} is not verified yet.")
    return StageStatus("ok", f"{newest.tag} verified {newest.verified_at:%Y-%m-%d %H:%M} UTC.")


def _cleanup(session, config_id) -> StageStatus:
    from datamanager.services import packages

    newest = packages.newest(session, config_id, verified=True)
    if newest is None:
        return StageStatus("blocked", "Needs a verified package.")
    last = _latest_run(session, "cleanup", config_id)
    if last is not None and last.status == "approved" and (last.params_json or {}).get("tag") == newest.tag:
        return StageStatus("ok", f"Cleaned up after {newest.tag}.")
    return StageStatus("todo", f"Choose what to remove after {newest.tag}.")


def _region_is_current(asset, plan) -> bool:
    """Compare a built region with what it would be built from now. Files made before fingerprints were
    recorded are compared by the country files and overlap polygon their meta lists."""
    stored = asset.meta_json.get("fingerprint")
    if stored:
        return stored == region_fingerprint(plan)
    countries = asset.meta_json.get("country_asset_ids")
    if countries is None:
        return False  # no record of its inputs (e.g. built from Geofabrik downloads before fingerprints)
    now = sorted(i.asset.id for i in plan.inputs if i.asset is not None)
    return countries == now and asset.meta_json.get("overlap_polygon_asset_id") == (plan.overlap_poly.id if plan.overlap_poly else None)


def _regions(session, config_id, resolved) -> StageStatus | None:
    plans, problems = plan_inputs(session, config_id, resolved)
    if problems or not plans:
        return None
    built, stale = [], []
    for plan in plans:
        current = assets.current(session, config_id, "osm-pbf", plan.slug)
        if current is None:
            stale.append(f"{plan.name} (not built)")
        else:
            built.append(plan.name)
            if not _region_is_current(current, plan):
                stale.append(plan.name)
    if not built:
        return StageStatus("todo", f"{len(plans)} region(s) to build.")
    if stale:
        return StageStatus("outdated", "Needs a run: " + ", ".join(stale) + ".")
    return StageStatus("ok", f"{len(plans)} region(s) built from the current inputs.")


def compute(session: Session, config_id: int, resolved: dict, blocked: dict[str, str | None]) -> dict[str, StageStatus]:
    content = {
        "download-planet": lambda: _planet(session),
        "download-tiles": lambda: _tiles(session),
        "extract-countries": lambda: _countries(session, resolved),
        "download-osm": lambda: _geofabrik(session, resolved),
        "polygons": lambda: _polygons(session, config_id, resolved),
        "download-srtm": lambda: _srtm(session, config_id, resolved),
        "download-pelias-data": lambda: _pelias_data(session, config_id, resolved),
        "download-overture-gtfs": lambda: _overture(session, config_id, resolved),
        "wof-patch": lambda: _wof_patch(session, config_id, resolved),
        "pelias": lambda: _pelias(session, config_id, resolved),
        "pelias-interpolation": lambda: _pelias_interpolation(session, config_id, resolved),
        "border": lambda: _border(session, config_id, resolved),
        "valhalla": lambda: _valhalla(session, config_id, resolved),
        "styles": lambda: _styles(session, config_id),
        "osm-extract": lambda: _regions(session, config_id, resolved),
        "package": lambda: _package(session, config_id),
        "package-verify": lambda: _package_verify(session, config_id),
        "cleanup": lambda: _cleanup(session, config_id),
    }
    result = {}
    for key, build in content.items():
        run = _latest_run(session, key, config_id)
        reason = blocked.get(key)
        if run is not None and run.status in ("queued", "running"):
            result[key] = StageStatus("running", f"Run {run.id} is {run.status}.", run.id)
        elif run is not None and run.status == "awaiting_review":
            result[key] = StageStatus("review", f"Run {run.id} finished; approve or reject it.", run.id)
        elif reason:
            result[key] = StageStatus("blocked", reason)
        else:
            status = build() or StageStatus("blocked", "Inputs are not ready.")
            if run is not None and run.status == "failed" and status.state != "ok":
                status = StageStatus("failed", f"Run {run.id}: {(run.error_message or 'failed')[:160]}", run.id)
            result[key] = status
    return result
