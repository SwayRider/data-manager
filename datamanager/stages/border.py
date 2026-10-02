import hashlib
import json
import shutil
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from datamanager.config import config
from datamanager.db import SessionLocal
from datamanager.errors import ValidationError
from datamanager.models import Asset
from datamanager.services import assets, borders, osmium
from datamanager.services.polygons import slug
from datamanager.stages.contract import StageIO, StageResult, StageRunContext, StageRunner


@dataclass
class RegionPlan:
    name: str
    slug: str
    core: object | None = None  # osm-core-pbf Asset
    full: object | None = None  # osm-pbf Asset

    @property
    def fingerprint(self) -> str:
        return _hash([self.core.content_hash, self.full.content_hash])


@dataclass
class PairPlan:
    a: RegionPlan
    b: RegionPlan
    poly: object | None = None  # border-polygon Asset

    @property
    def name(self) -> str:
        return f"{self.a.slug}-{self.b.slug}"

    @property
    def fingerprint(self) -> str:
        return _hash([self.a.full.content_hash, self.a.core.content_hash, self.poly.content_hash, self.a.name, self.b.name])


@dataclass
class BorderPlan:
    regions: list[RegionPlan] = field(default_factory=list)
    pairs: list[PairPlan] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def _hash(parts: list) -> str:
    return hashlib.sha256(json.dumps(parts).encode()).hexdigest()[:16]


def plan_inputs(session, config_id: int, resolved: dict, regions: list[str] | None = None) -> BorderPlan:
    """What the stage will build, and every reason it cannot: a region PBF or border polygon that is not approved.
    A pair is built from its first region (slug order, as in the polygon name): that region's full PBF clipped to
    the border zone, crossings measured against its core outline."""
    plan = BorderPlan()
    wanted = set(regions) if regions else None
    by_name: dict[str, RegionPlan] = {}
    for region in resolved.get("regions", []):
        if not region["core"]:
            continue
        item = RegionPlan(region["name"], slug(region["name"]))
        item.core = assets.current(session, config_id, "osm-core-pbf", f"{item.slug}-core")
        item.full = assets.current(session, config_id, "osm-pbf", item.slug)
        by_name[region["name"]] = item
    for name, item in by_name.items():
        if wanted is not None and name not in wanted:
            continue
        if item.core is None or item.full is None:
            plan.problems.append(f"{name}: no approved region PBFs (run and approve OSM extract)")
            continue
        plan.regions.append(item)
    if wanted:
        for name in sorted(wanted - set(by_name)):
            plan.problems.append(f"Unknown region or no core countries: {name}")
    seen = set()
    for pair in resolved.get("border_regions", []):
        first, second = sorted(pair, key=slug)
        if first not in by_name or second not in by_name or (first, second) in seen:
            continue
        seen.add((first, second))
        if wanted is not None and first not in wanted:
            continue
        a, b = by_name[first], by_name[second]
        item = PairPlan(a, b)
        if a.full is None or a.core is None:
            continue  # reported above, once per region
        item.poly = assets.current(session, config_id, "border-polygon", f"{a.slug}-{b.slug}-border")
        if item.poly is None:
            plan.problems.append(f"{first} / {second}: no approved border polygon (run and approve Region polygons)")
            continue
        plan.pairs.append(item)
    return plan


def border_fingerprint(plan: BorderPlan) -> str:
    return _hash([[r.name, r.fingerprint] for r in plan.regions] + [[p.name, p.fingerprint] for p in plan.pairs])


class BorderStage(StageRunner):
    """Per region its core and extended country outlines (`region-outline`), and per bordering pair the road
    crossings (`border-crossings`, CSV like the legacy pipeline's `border-crossings/<a>-<b>.csv`). Needs the
    approved region PBFs and border polygons; a region or pair whose inputs are unchanged since its current
    approved asset is skipped. `params.regions` limits a run to the pairs that start at these regions."""

    key = "border"
    produces = (StageIO("region-outline"), StageIO("border-crossings"))
    consumes = (StageIO("osm-pbf"), StageIO("osm-core-pbf"), StageIO("border-polygon"))

    def run(self, context: StageRunContext) -> StageResult:
        if context.config_resolved is None or context.config_id is None:
            raise ValidationError("border needs a configuration")
        session = SessionLocal()
        exe = osmium.binary(session)
        plan = plan_inputs(session, context.config_id, context.config_resolved, context.params.get("regions"))
        if not plan.regions and not plan.problems:
            plan.problems.append("The configuration has no region with a core country.")
        if plan.problems:
            return StageResult("failed", report={"error": "; ".join(plan.problems), "problems": plan.problems})

        run_id = int(context.run_id)
        work = Path(context.work_dir)
        out_dir = assets.asset_dir("border", run_id)
        outlines, crossings, failed = [], [], []
        try:
            for region in plan.regions:
                entry = self._outline(session, exe, region, context, run_id, work / region.slug, out_dir)
                outlines.append(entry)
                if entry["status"] == "failed":
                    failed.append(region.name)
            for pair in plan.pairs:
                entry = self._pair(session, exe, pair, context, run_id, work / pair.name, out_dir)
                crossings.append(entry)
                if entry["status"] == "failed":
                    failed.append(pair.name)
        finally:
            shutil.rmtree(work, ignore_errors=True)

        review_map = {} if failed else self._map(session, outlines, crossings)
        if failed:
            assets.discard(session, run_id)
        warnings = [w for e in outlines + crossings for w in e.get("warnings", [])]
        report = {
            "summary": {
                "regions": len(outlines), "pairs": len(crossings), "failed": len(failed), "warnings": len(warnings),
                "crossings": sum(e.get("count", 0) for e in crossings), "fingerprint": border_fingerprint(plan),
            },
            "outlines": outlines,
            "borders": crossings,
            "map": review_map,
            "warnings": warnings,
        }
        if failed:
            report["error"] = "Failed: " + ", ".join(failed)
        return StageResult("failed" if failed else "success", report=report)

    @staticmethod
    def _map(session, outlines: list[dict], crossings: list[dict]) -> dict:
        """Coarse outlines and crossing points for the run page's review map; read from the stored files so
        regions and pairs skipped as unchanged show up too."""
        data = {"outlines": [], "crossings": []}
        for entry in outlines:
            for f in entry.get("files", []):
                asset = session.get(Asset, f["asset_id"]) if f.get("asset_id") else None
                preview = borders.map_outline(assets.abs_path(asset)) if asset is not None and assets.abs_path(asset).exists() else None
                if preview:
                    data["outlines"].append({"name": f["name"], "region": entry["name"], "kind": f["name"].rsplit("-", 1)[-1], "preview": preview})
        for entry in crossings:
            asset = session.get(Asset, entry["asset_id"]) if entry.get("asset_id") else None
            relative = (asset.meta_json or {}).get("geojson") if asset is not None else None
            file = Path(config.DATA_ROOT) / relative if relative else None
            if file is not None and file.exists():
                data["crossings"].append({"pair": entry["name"], "count": entry.get("count", 0), "points": borders.map_points(file)})
        return data

    def _outline(self, session, exe, region: RegionPlan, context, run_id: int, work: Path, out_dir: Path) -> dict:
        entry = {"name": region.name, "status": "success", "files": [], "warnings": []}
        try:
            for suffix, source in (("core", region.core), ("extended", region.full)):
                name = f"{region.slug}-{suffix}"
                current = assets.current(session, context.config_id, "region-outline", name)
                if current is not None and current.meta_json.get("fingerprint") == region.fingerprint:
                    entry["files"].append({"name": name, "status": "unchanged", "asset_id": current.id})
                    continue
                context.step_cb(f"{region.name}: {suffix} outline")
                target = out_dir / f"{name}.geojson"
                facts = borders.outline(exe, assets.abs_path(source), target, work)
                asset = assets.create(
                    session, run_id, context.config_id, "region-outline", name, target,
                    meta={"region": region.name, "source_asset_id": source.id, "fingerprint": region.fingerprint, **facts},
                )
                entry["files"].append({"name": name, "status": "built", "asset_id": asset.id, "size_bytes": asset.size_bytes, **facts})
                if facts["parts"] == 0:
                    entry["warnings"].append(f"{region.name}: no admin_level=2 boundary found in the {suffix} PBF, the outline is empty.")
        except (osmium.OsmiumError, OSError) as exc:
            entry.update(status="failed", message=str(exc))
        return entry

    def _pair(self, session, exe, pair: PairPlan, context, run_id: int, work: Path, out_dir: Path) -> dict:
        entry = {"name": pair.name, "from": pair.a.name, "to": pair.b.name, "status": "success", "warnings": []}
        current = assets.current(session, context.config_id, "border-crossings", pair.name)
        if current is not None and current.meta_json.get("fingerprint") == pair.fingerprint:
            return {**entry, "result": "unchanged", "asset_id": current.id, "count": current.meta_json.get("count", 0),
                    "by_type": current.meta_json.get("by_type", {})}
        try:
            core_outline = assets.current(session, context.config_id, "region-outline", f"{pair.a.slug}-core")
            fresh = out_dir / f"{pair.a.slug}-core.geojson"
            outline_file = fresh if fresh.exists() else (assets.abs_path(core_outline) if core_outline else None)
            if outline_file is None:
                raise osmium.OsmiumError(f"no core outline of {pair.a.name}")
            context.step_cb(f"{pair.name}: border area")
            area = osmium.extract(exe, assets.abs_path(pair.a.full), assets.abs_path(pair.poly), work / f"{pair.name}.osm.pbf")
            context.step_cb(f"{pair.name}: crossings")
            found = borders.detect(borders.roads(exe, area, work), borders.load_outline(outline_file), pair.a.name, pair.b.name)
            target = borders.write_csv(out_dir / f"{pair.name}.csv", found)
            points = out_dir / f"{pair.name}.geojson"
            points.write_text(json.dumps(borders.crossings_geojson(found)), encoding="utf-8")
            by_type = dict(sorted(Counter(c.osm_type for c in found).items()))
            asset = assets.create(
                session, run_id, context.config_id, "border-crossings", pair.name, target,
                meta={"from": pair.a.name, "to": pair.b.name, "count": len(found), "by_type": by_type, "fingerprint": pair.fingerprint,
                      "geojson": str(points.resolve().relative_to(Path(config.DATA_ROOT).resolve()))},
            )
            entry.update(result="built", asset_id=asset.id, count=len(found), by_type=by_type, sha256=asset.content_hash)
            if not found:
                entry["warnings"].append(f"{pair.name}: no road crossings found between {pair.a.name} and {pair.b.name}.")
        except (osmium.OsmiumError, OSError) as exc:
            entry.update(status="failed", message=str(exc))
        return entry
