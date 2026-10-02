import hashlib
import json
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from datamanager.config import config
from datamanager.db import SessionLocal
from datamanager.errors import ValidationError
from datamanager.models import BuildRun, DownloadRecord
from datamanager.services import assets, srtm, valhalla_build, valhalla_data
from datamanager.services import settings as settings_service
from datamanager.services.polygons import slug
from datamanager.stages.contract import StageIO, StageResult, StageRunContext, StageRunner

ASSET_TYPES = {"tiles": "valhalla-tiles", "admin": "valhalla-admin", "timezones": "valhalla-timezones", "polylines": "valhalla-polylines"}


@dataclass
class RegionPlan:
    name: str
    slug: str
    pbf: object | None = None  # osm-pbf Asset
    tiles: list[str] = field(default_factory=list)  # SRTM tile names the region's boxes touch
    fingerprint: str = ""


@dataclass
class ValhallaPlan:
    regions: list[RegionPlan] = field(default_factory=list)
    srtm_dir: Path | None = None
    problems: list[str] = field(default_factory=list)


def _hash(parts: list) -> str:
    return hashlib.sha256(json.dumps(parts).encode()).hexdigest()[:16]


def srtm_run(session, config_id: int) -> BuildRun | None:
    """The newest approved download-srtm run: its folder is the elevation directory Valhalla reads."""
    return (
        session.query(BuildRun)
        .filter(BuildRun.stage_key == "download-srtm", BuildRun.config_profile_id == config_id, BuildRun.status == "approved")
        .order_by(BuildRun.id.desc()).first()
    )


def plan_inputs(session, config_id: int, resolved: dict, regions: list[str] | None = None) -> ValhallaPlan:
    """What the stage will build and every reason it cannot: Valhalla not built, no approved elevation download,
    or a region without an approved PBF. A region's fingerprint covers its PBF, the Valhalla build and the content of
    the elevation tiles of its boxes, so it is rebuilt only when one of those changed."""
    plan = ValhallaPlan()
    try:
        valhalla_build.binary(session, "valhalla_build_tiles")
    except ValidationError as exc:
        plan.problems.append(exc.message)
    run = srtm_run(session, config_id)
    hashes: dict[str, str] = {}
    if run is None:
        plan.problems.append("No approved elevation download: run Download elevation (SRTM) and approve it first.")
    else:
        plan.srtm_dir = Path(config.DATA_ROOT) / run.report_json["summary"]["directory"]
        for record in session.query(DownloadRecord).filter(DownloadRecord.id.in_(run.report_json.get("record_ids", []))):
            hashes[record.source_key.removeprefix("srtm:")] = record.content_hash
    wanted = set(regions) if regions else None
    known = set()
    for region in resolved.get("regions", []):
        if not region["core"]:
            continue
        known.add(region["name"])
        if wanted is not None and region["name"] not in wanted:
            continue
        item = RegionPlan(region["name"], slug(region["name"]))
        item.pbf = assets.current(session, config_id, "osm-pbf", item.slug)
        if item.pbf is None:
            plan.problems.append(f"{item.name}: no approved region PBF (run and approve OSM extract)")
            continue
        item.tiles = srtm.tiles(list(region.get("srtm", {}).values()))
        item.fingerprint = _hash([item.pbf.content_hash, valhalla_build.version(session), [[t, hashes.get(t)] for t in item.tiles]])
        plan.regions.append(item)
    for name in sorted((wanted or set()) - known):
        plan.problems.append(f"Unknown region or no core countries: {name}")
    return plan


def valhalla_fingerprint(plan: ValhallaPlan) -> str:
    return _hash([[r.name, r.fingerprint] for r in plan.regions])


class ValhallaStage(StageRunner):
    """Per region the routing data Valhalla serves: `tiles.tar`, `admin.sqlite`, `tz_world.sqlite` and the edge
    polylines Pelias uses (`valhalla-*` assets, name = region slug), built from the region's approved full PBF (core plus
    overlap) and the approved elevation tiles with the Valhalla compiled under Settings → Tools. A region whose inputs
    are unchanged since its approved assets is skipped. `params.regions` limits a run."""

    key = "valhalla"
    produces = tuple(StageIO(t) for t in ASSET_TYPES.values())
    consumes = (StageIO("osm-pbf"),)
    consumes_downloads = ("srtm",)

    def run(self, context: StageRunContext) -> StageResult:
        if context.config_resolved is None or context.config_id is None:
            raise ValidationError("valhalla needs a configuration")
        session = SessionLocal()
        plan = plan_inputs(session, context.config_id, context.config_resolved, context.params.get("regions"))
        if not plan.regions and not plan.problems:
            plan.problems.append("The configuration has no region with a core country.")
        if plan.problems:
            return StageResult("failed", report={"error": "; ".join(plan.problems), "problems": plan.problems})

        run_id = int(context.run_id)
        Path(context.work_dir).mkdir(parents=True, exist_ok=True)
        concurrency = int(settings_service.get(session, "run.valhalla_concurrency"))
        work = Path(context.work_dir)
        out_root = assets.asset_dir("valhalla", run_id)
        entries, failed = [], []
        tz_file = work / "tz_world.sqlite"
        try:
            for region in plan.regions:
                entry = self._region(session, plan, region, context, run_id, work, out_root, tz_file, concurrency)
                entries.append(entry)
                if entry["status"] == "failed":
                    failed.append(region.name)
        finally:
            shutil.rmtree(work, ignore_errors=True)
        if failed:
            assets.discard(session, run_id)
        warnings = [w for e in entries for w in e.get("warnings", [])]
        report = {
            "summary": {"regions": len(entries), "failed": len(failed), "built": sum(e.get("result") == "built" for e in entries),
                        "fingerprint": valhalla_fingerprint(plan), "valhalla": valhalla_build.read_state().get("resolved")},
            "routing": entries,
            "warnings": warnings,
        }
        if failed:
            report["error"] = "Failed: " + ", ".join(failed)
        return StageResult("failed" if failed else "success", report=report)

    def _region(self, session, plan, region: RegionPlan, context, run_id: int, work: Path, out_root: Path, tz_file: Path, concurrency: int) -> dict:
        entry = {"name": region.name, "status": "success", "warnings": [], "tiles_wanted": len(region.tiles)}
        current = {k: assets.current(session, context.config_id, t, region.slug) for k, t in ASSET_TYPES.items()}
        if all(a is not None and a.meta_json.get("fingerprint") == region.fingerprint for a in current.values()):
            return {**entry, "result": "unchanged", "asset_ids": {k: a.id for k, a in current.items()},
                    **{k: current["tiles"].meta_json.get(k) for k in ("graph_tiles", "polyline_lines")}}
        started = time.monotonic()
        try:
            if not tz_file.exists():
                context.step_cb("Timezone database")
                with open(work / "tz.log", "w", encoding="utf-8") as log:
                    valhalla_data.build_timezones(session, tz_file, log)
            facts = valhalla_data.build_region(
                session, assets.abs_path(region.pbf), plan.srtm_dir, work / region.slug, out_root / region.slug, tz_file,
                concurrency, step_cb=lambda step: context.step_cb(f"{region.name}: {step}"),
            )
            ids = {}
            for key, asset_type in ASSET_TYPES.items():
                meta = {"region": region.name, "fingerprint": region.fingerprint, "valhalla": valhalla_build.version(session),
                        "source_asset_id": region.pbf.id, "graph_tiles": facts["tiles"], "polyline_lines": facts["polyline_lines"]}
                ids[key] = assets.create(session, run_id, context.config_id, asset_type, region.slug,
                                         out_root / region.slug / valhalla_data.FILES[key], meta=meta).id
            entry.update(result="built", asset_ids=ids, graph_tiles=facts["tiles"], polyline_lines=facts["polyline_lines"],
                         bytes=facts["bytes"], seconds=round(time.monotonic() - started))
            if facts["polyline_lines"] == 0:
                entry["warnings"].append(f"{region.name}: the edge polyline export is empty.")
        except (valhalla_build.BuildError, valhalla_data.DataError, OSError) as exc:
            entry.update(status="failed", message=str(exc))
        return entry
