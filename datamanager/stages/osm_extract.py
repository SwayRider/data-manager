import datetime
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from datamanager.db import SessionLocal
from datamanager.errors import ValidationError
from datamanager.services import assets, downloads, osmium
from datamanager.services import settings as settings_service
from datamanager.services.polygons import slug
from datamanager.stages.contract import StageIO, StageResult, StageRunContext, StageRunner
from datamanager.stages.download_osm import osm_source_key

STALE_DAYS = 60  # warn when an input download is older than this
MIN_COVERAGE = 0.5  # the data bbox must cover at least this share of the overlap polygon's bbox


@dataclass
class Input:
    """One country PBF a region is built from: a Geofabrik download version (`record`) or, with
    `osm.source = planet`, a country extract cut from the planet (`asset`)."""

    iso2: str
    role: str  # core | overlap
    source_key: str
    record: object | None = None  # DownloadRecord
    asset: object | None = None  # country-pbf Asset
    carve: object | None = None  # carve-polygon Asset when the country is carved

    @property
    def file(self) -> Path:
        return assets.abs_path(self.asset) if self.asset is not None else downloads.abs_path(self.record)

    @property
    def download_ids(self) -> list[int]:
        return list(self.asset.source_download_ids) if self.asset is not None else [self.record.id]

    @property
    def label(self) -> str:
        return self.asset.meta_json.get("planet_version", "") if self.asset is not None else self.record.version_label

    @property
    def date(self) -> str:
        if self.asset is not None:
            stamp = self.asset.meta_json.get("planet_data_timestamp") or self.asset.created_at.strftime("%Y-%m-%d")
            return stamp[:10]
        return self.record.fetched_at.strftime("%Y-%m-%d")

    def age_days(self, now: datetime.datetime) -> int:
        stamp = self.asset.meta_json.get("planet_data_timestamp") if self.asset is not None else None
        if stamp:
            try:
                return (now - datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00")).replace(tzinfo=None)).days
            except ValueError:
                pass
        return (now - (self.asset.created_at if self.asset is not None else self.record.fetched_at)).days


@dataclass
class RegionPlan:
    name: str
    slug: str
    inputs: list[Input] = field(default_factory=list)
    overlap_poly: object | None = None  # overlap-polygon Asset (None: region has no overlap countries)

    def of(self, role: str) -> list[Input]:
        return [i for i in self.inputs if i.role == role]


def region_fingerprint(plan: RegionPlan) -> str:
    """What a region's PBFs are built from: its country files (planet extract or Geofabrik version),
    overlap polygon and carve polygons. A different fingerprint means the stage should run again."""
    import hashlib
    import json

    content = {
        "inputs": sorted([i.iso2, i.role, "asset" if i.asset is not None else "record",
                          i.asset.id if i.asset is not None else i.record.id] for i in plan.inputs),
        "overlap": plan.overlap_poly.id if plan.overlap_poly is not None else None,
        "carve": sorted(i.carve.id for i in plan.inputs if i.carve is not None),
    }
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()[:16]


def plan_inputs(session, config_id: int, resolved: dict, regions: list[str] | None = None,
                overrides: dict | None = None) -> tuple[list[RegionPlan], list[str]]:
    """What each selected region will be built from, and every reason it cannot be built yet:
    a country without an approved download, or a missing approved polygon."""
    plans, problems = [], []
    source = str(settings_service.get(session, "osm.source"))
    wanted = set(regions) if regions else None
    for region in resolved.get("regions", []):
        if wanted is not None and region["name"] not in wanted:
            continue
        if not region["core"]:
            continue
        plan = RegionPlan(region["name"], slug(region["name"]))
        for role in ("core", "overlap"):
            for country in region[role]:
                path = country.get("geofabrik_path")
                if not path:
                    continue
                key = osm_source_key(path)
                if source == "planet":
                    asset = assets.current(session, None, "country-pbf", path)
                    if asset is None:
                        problems.append(f"{region['name']}: no approved country extract of {path} (run Extract countries)")
                        continue
                    item = Input(country["iso2"], role, key, asset=asset)
                else:
                    record = downloads.resolve_version(session, key, overrides)
                    if record is None or record.status == "fetched":
                        problems.append(f"{region['name']}: no approved download of {path}")
                        continue
                    item = Input(country["iso2"], role, key, record=record)
                if country.get("carve"):
                    item.carve = assets.current(session, config_id, "carve-polygon", f"carve-{country['iso2']}")
                    if item.carve is None:
                        problems.append(f"{region['name']}: no approved carve polygon for {country['iso2']}")
                plan.inputs.append(item)
        if plan.of("overlap"):
            plan.overlap_poly = assets.current(session, config_id, "overlap-polygon", f"{plan.slug}-overlap")
            if plan.overlap_poly is None:
                problems.append(f"{region['name']}: no approved overlap polygon (run and approve Region polygons)")
        plans.append(plan)
    if wanted:
        for name in sorted(wanted - {p.name for p in plans}):
            problems.append(f"Unknown region or no core countries: {name}")
    return plans, problems


class OsmExtractStage(StageRunner):
    """Builds each region's `<region>-core.osm.pbf` (core countries merged, carved ones clipped) and
    `<region>.osm.pbf` (core plus the overlap countries clipped to the region's overlap polygon), from
    approved downloads and approved polygons. Regions are independent: `params.regions` limits a run."""

    key = "osm-extract"
    produces = (StageIO("osm-core-pbf"), StageIO("osm-pbf"))
    consumes = (StageIO("overlap-polygon"), StageIO("carve-polygon"))
    consumes_downloads = ("osm",)

    def run(self, context: StageRunContext) -> StageResult:
        if context.config_resolved is None or context.config_id is None:
            raise ValidationError("osm-extract needs a configuration")
        session = SessionLocal()
        exe = osmium.binary(session)
        plans, problems = plan_inputs(
            session, context.config_id, context.config_resolved, context.params.get("regions"), context.input_versions
        )
        if not plans and not problems:
            problems.append("The configuration has no region with a core country.")
        if problems:
            return StageResult("failed", report={"error": "; ".join(problems), "problems": problems})

        run_id = int(context.run_id)
        work = Path(context.work_dir)
        entries = []
        try:
            for plan in plans:
                entries.append(self._build_region(session, exe, plan, context, run_id, work / plan.slug))
        finally:
            shutil.rmtree(work, ignore_errors=True)

        failed = [e["name"] for e in entries if e["status"] == "failed"]
        if failed:  # a failed run cannot be reviewed, so do not leave its multi-GB files behind
            assets.discard(session, run_id)
        report = {
            "summary": {
                "regions": len(entries),
                "failed": len(failed),
                "warnings": sum(len(e["warnings"]) for e in entries),
                "bytes": sum(e.get("pbf", {}).get("size_bytes", 0) for e in entries),
            },
            "regions": entries,
        }
        if failed:
            report["error"] = "Failed: " + ", ".join(failed)
        return StageResult("failed" if failed else "success", report=report)

    def _build_region(self, session, exe, plan: RegionPlan, context: StageRunContext, run_id: int, work: Path) -> dict:
        entry = {
            "name": plan.name, "status": "success", "warnings": [],
            "inputs": [_describe(i) for i in plan.inputs],
        }
        try:
            core_file, region_file = self._merge(exe, plan, context, work)
            out_dir = assets.asset_dir("osm", run_id)
            context.step_cb(f"{plan.name}: checking")
            source_ids = sorted({d for i in plan.inputs for d in i.download_ids})
            for label, file, asset_type, name in (
                ("core", core_file, "osm-core-pbf", f"{plan.slug}-core"),
                ("pbf", region_file, "osm-pbf", plan.slug),
            ):
                final = out_dir / f"{name}.osm.pbf"
                file.replace(final)
                info = osmium.fileinfo(exe, final)
                asset = assets.create(
                    session, run_id, context.config_id, asset_type, name, final,
                    meta={"region": plan.name, "counts": info["counts"], "bbox": info["bbox"],
                          "replication_timestamp": info["replication_timestamp"],
                          "overlap_polygon_asset_id": plan.overlap_poly.id if plan.overlap_poly else None,
                          "country_asset_ids": sorted(i.asset.id for i in plan.inputs if i.asset is not None),
                          "fingerprint": region_fingerprint(plan)},
                    source_download_ids=source_ids,
                )
                entry[label] = {"asset_id": asset.id, "size_bytes": asset.size_bytes, "sha256": asset.content_hash, **info}
            entry["warnings"] = _checks(plan, entry)
        except (osmium.OsmiumError, OSError) as exc:
            entry.update(status="failed", message=str(exc))
        return entry

    def _merge(self, exe, plan: RegionPlan, context: StageRunContext, work: Path) -> tuple[Path, Path]:
        work.mkdir(parents=True, exist_ok=True)

        def carved(item: Input, prefix: str) -> Path:
            source = item.file
            if item.carve is None:
                return source
            context.step_cb(f"{plan.name}: clip {item.iso2} to its carve polygon")
            return osmium.extract(exe, source, assets.abs_path(item.carve), work / f"{prefix}-{item.iso2}-carved.osm.pbf")

        core_inputs = [carved(i, "core") for i in plan.of("core")]
        context.step_cb(f"{plan.name}: merge core ({len(core_inputs)} file(s))")
        core_file = osmium.merge_latest(exe, core_inputs, work / f"{plan.slug}-core.osm.pbf")

        clips = []
        for item in plan.of("overlap"):
            source = carved(item, "overlap")
            context.step_cb(f"{plan.name}: clip {item.iso2} to the overlap polygon")
            clips.append(osmium.extract(exe, source, assets.abs_path(plan.overlap_poly), work / f"overlap-{item.iso2}.osm.pbf"))
        context.step_cb(f"{plan.name}: merge core and overlap")
        region_file = osmium.merge_latest(exe, [core_file, *clips], work / f"{plan.slug}.osm.pbf")
        return core_file, region_file


def _describe(item: Input) -> dict:
    status = item.asset.status if item.asset is not None else item.record.status
    return {
        "iso2": item.iso2, "role": item.role, "source_key": item.source_key,
        "record_id": item.record.id if item.record is not None else None,
        "asset_id": item.asset.id if item.asset is not None else None,
        "from_planet": item.asset is not None, "version": item.label, "fetched_at": item.date,
        "record_status": status, "pinned": bool(item.record.pinned) if item.record is not None else False,
        "carved": item.carve is not None,
    }


def _checks(plan: RegionPlan, entry: dict) -> list[str]:
    """Warnings a reviewer should look at; none of them fail the run."""
    warnings = []
    core, full = entry["core"], entry["pbf"]
    if not sum(full["counts"].values()):
        warnings.append("The region file is empty.")
    if full["size_bytes"] < core["size_bytes"]:
        warnings.append("The region file is smaller than its core file.")
    for label, part in (("core", core), ("region", full)):
        if part["multiple_versions"]:
            warnings.append(f"The {label} file still holds several versions of some objects.")
        if part["ordered"] is False:
            warnings.append(f"The {label} file is not sorted.")
    if plan.overlap_poly is not None and full["bbox"]:
        box = osmium.poly_bbox(assets.abs_path(plan.overlap_poly))
        coverage = osmium.bbox_coverage(full["bbox"], box)
        if coverage is not None and coverage < MIN_COVERAGE:
            warnings.append(f"The data covers only {coverage:.0%} of the overlap polygon's bounding box.")
    now = datetime.datetime.now(datetime.UTC).replace(tzinfo=None)
    for item in plan.inputs:
        age = item.age_days(now)
        if age > STALE_DAYS:
            warnings.append(f"{item.source_key} is {age} days old.")
    return warnings
