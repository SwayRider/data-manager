import hashlib
import json
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from datamanager.db import SessionLocal
from datamanager.errors import ValidationError
from datamanager.services import assets, downloads, pelias_build, pelias_interpolation as interpolation, tools
from datamanager.services.polygons import slug
from datamanager.services.valhalla_build import BuildError
from datamanager.stages.contract import StageIO, StageResult, StageRunContext, StageRunner

PLAN_VERSION = 1  # bump when the way the databases are built or what they hold changes
ASSET_TYPES = {"street": "pelias-interpolation-street-db", "address": "pelias-interpolation-address-db"}
FILES = {"street": "street.db", "address": "address.db"}


@dataclass
class RegionPlan:
    name: str
    slug: str
    pbf: object | None = None  # osm-pbf Asset
    polylines: object | None = None  # valhalla-polylines Asset
    openaddresses: dict[str, object] = field(default_factory=dict)  # source -> DownloadRecord
    warnings: list[str] = field(default_factory=list)
    fingerprint: str = ""


@dataclass
class InterpolationPlan:
    regions: list[RegionPlan] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def _hash(parts: list) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()[:16]


def node_bin(session) -> str:
    status = tools.detect(session, "node", force=False)
    if not status.ok:
        raise ValidationError(f"node is not available ({status.message or status.status}); see Settings → Tools.")
    return status.path


def environment_problems(session) -> list[str]:
    problems = []
    found = pelias_build.detect(session)
    if found["status"] not in ("ok", "outdated"):
        problems.append("The Pelias importers are not built (Settings → Tools). " + found["message"])
    try:
        node_bin(session)
    except ValidationError as exc:
        problems.append(exc.message)
    return problems


def plan_inputs(session, config_id: int, resolved: dict, regions: list[str] | None = None) -> InterpolationPlan:
    """Per region: the approved PBF and edge polylines, and the approved OpenAddresses downloads (a source without one is
    a warning). The fingerprint covers their content, the interpolation commit and the plan version."""
    plan = InterpolationPlan(problems=environment_problems(session))
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
        item.polylines = assets.current(session, config_id, "valhalla-polylines", item.slug)
        if item.pbf is None:
            plan.problems.append(f"{item.name}: no approved region PBF (run and approve OSM extract)")
        if item.polylines is None:
            plan.problems.append(f"{item.name}: no approved edge polylines (run and approve Valhalla routing data)")
        seen: set[str] = set()
        for country in [*region["core"], *region["overlap"]]:
            if not country.get("geofabrik_path") or country["iso2"] in seen:
                continue
            seen.add(country["iso2"])
            for source in country.get("openaddresses", []):
                record = downloads.resolve_version(session, f"openaddresses:{source}")
                if record is not None and record.status != "fetched":
                    item.openaddresses[source] = record
                else:
                    item.warnings.append(f"{country['iso2']}: OpenAddresses source {source} has no approved download and is skipped.")
        if item.pbf is not None and item.polylines is not None:
            item.fingerprint = _hash([item.pbf.content_hash, item.polylines.content_hash,
                                      sorted([s, r.content_hash] for s, r in item.openaddresses.items()),
                                      pelias_build.version(session), PLAN_VERSION])
        plan.regions.append(item)
    for name in sorted((wanted or set()) - known):
        plan.problems.append(f"Unknown region or no core countries: {name}")
    if not plan.regions and not plan.problems:
        plan.problems.append("The configuration has no region with a core country.")
    return plan


def interpolation_fingerprint(plan: InterpolationPlan) -> str:
    return _hash([[r.name, r.fingerprint] for r in plan.regions])


class PeliasInterpolationStage(StageRunner):
    """Per region, the two SQLite databases of the Pelias interpolation service built with the cloned `interpolation`
    importers: `street.db` from the edge polylines and `address.db` from the OpenAddresses sources and the region PBF
    (house numbers), with the fractional numbers of the street vertices computed. No Elasticsearch involved. A region
    whose inputs did not change since its approved assets is skipped; `params.regions` limits a run. The service that reads
    the databases is not built here (deploy)."""

    key = "pelias-interpolation"
    produces = tuple(StageIO(t) for t in ASSET_TYPES.values())
    consumes = (StageIO("osm-pbf"), StageIO("valhalla-polylines"))
    consumes_downloads = ("openaddresses",)

    def run(self, context: StageRunContext) -> StageResult:
        if context.config_resolved is None or context.config_id is None:
            raise ValidationError("pelias-interpolation needs a configuration")
        session = SessionLocal()
        plan = plan_inputs(session, context.config_id, context.config_resolved, context.params.get("regions"))
        if plan.problems:
            return StageResult("failed", report={"error": "; ".join(plan.problems), "problems": plan.problems})
        run_id = int(context.run_id)
        node = node_bin(session)
        work = Path(context.work_dir)
        work.mkdir(parents=True, exist_ok=True)
        out_root = assets.asset_dir("pelias-interpolation", run_id)
        entries, failed = [], []
        try:
            for region in plan.regions:
                entry = self._region(session, region, context, run_id, node, work, out_root)
                entries.append(entry)
                if entry["status"] == "failed":
                    failed.append(region.name)
        finally:
            shutil.rmtree(work, ignore_errors=True)
        if failed:
            assets.discard(session, run_id)
        report = {
            "summary": {"regions": len(entries), "failed": len(failed), "built": sum(e.get("result") == "built" for e in entries),
                        "unchanged": sum(e.get("result") == "unchanged" for e in entries), "fingerprint": interpolation_fingerprint(plan),
                        "pelias": pelias_build.version(session)},
            "interpolation": entries,
            "warnings": [w for e in entries for w in e.get("warnings", [])],
        }
        if failed:
            report["error"] = "Failed: " + ", ".join(failed)
        return StageResult("failed" if failed else "success", report=report)

    def _region(self, session, region: RegionPlan, context, run_id: int, node: str, work: Path, out_root: Path) -> dict:
        entry = {"name": region.name, "status": "success", "warnings": list(region.warnings), "steps": []}
        current = {k: assets.current(session, context.config_id, t, region.slug) for k, t in ASSET_TYPES.items()}
        if all(a is not None and a.meta_json.get("fingerprint") == region.fingerprint and assets.usable(a) for a in current.values()):
            return {**entry, "result": "unchanged", "asset_ids": {k: a.id for k, a in current.items()}, "counts": current["street"].meta_json.get("counts")}
        started = time.monotonic()
        base = work / region.slug
        logs, tmp = base / "logs", base / "tmp"
        street_db, address_db = base / FILES["street"], base / FILES["address"]
        try:
            repo = pelias_build.require(session, "interpolation")

            def step(name: str, label: str, call) -> dict:
                context.step_cb(f"{region.name}: {label}")
                began = time.monotonic()
                result = call()
                entry["steps"].append({"name": name, "seconds": round(time.monotonic() - began), **result})
                return result

            step("polyline", "street network", lambda: interpolation.run_script(
                node, repo, "polyline.js", [str(street_db)], logs, "polyline", tmp, interpolation.polyline_lines(assets.input_path(region.polylines))))
            if region.openaddresses:
                csv_file = base / "openaddresses.csv"
                converted = step("openaddresses-csv", "converting OpenAddresses", lambda: interpolation.openaddresses_csv(
                    {s: downloads.input_path(r) for s, r in region.openaddresses.items()}, csv_file, tmp))
                step("oa", "OpenAddresses addresses", lambda: interpolation.run_script(
                    node, repo, "oa.js", [str(address_db), str(street_db)], logs, "oa", tmp, interpolation.csv_lines(csv_file)))
                csv_file.unlink(missing_ok=True)
                entry["openaddresses"] = {"sources": len(region.openaddresses), **converted}
            step("osm", "OpenStreetMap addresses", lambda: interpolation.run_osm(
                node, repo, assets.input_path(region.pbf), address_db, street_db, logs, tmp, base / "leveldb"))
            shutil.rmtree(base / "leveldb", ignore_errors=True)
            step("vertices", "street vertices", lambda: interpolation.run_script(
                node, repo, "vertices.js", [str(address_db), str(street_db)], logs, "vertices", tmp))
            counts = {"streets": interpolation.table_count(street_db, "polyline"), "names": interpolation.table_count(street_db, "names"),
                      "addresses": interpolation.table_count(address_db, "address")}
            out_dir = out_root / region.slug
            out_dir.mkdir(parents=True, exist_ok=True)
            meta = {"region": region.name, "fingerprint": region.fingerprint, "counts": counts, "pelias": pelias_build.version(session)}
            ids, sizes = {}, {}
            for key, db in (("street", street_db), ("address", address_db)):
                if not db.exists():
                    raise BuildError(f"{FILES[key]} was not written")
                target = out_dir / FILES[key]
                shutil.move(str(db), target)
                ids[key] = assets.create(session, run_id, context.config_id, ASSET_TYPES[key], region.slug, target, meta=meta).id
                sizes[key] = target.stat().st_size
            entry.update(result="built", asset_ids=ids, counts=counts, sizes=sizes, seconds=round(time.monotonic() - started))
            if not counts["addresses"]:
                entry["warnings"].append(f"{region.name}: the address database is empty.")
        except (BuildError, OSError, ValidationError) as exc:
            entry.update(status="failed", message=str(exc))
        return entry
