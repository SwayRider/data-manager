import hashlib
import json
import shutil
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from shapely.geometry import shape

from datamanager.config import config
from datamanager.db import SessionLocal
from datamanager.errors import DownloadError, ValidationError
from datamanager.services import assets, downloads, overture, tools
from datamanager.services import settings as settings_service
from datamanager.services.polygons import slug
from datamanager.stages.contract import StageIO, StageResult, StageRunContext, StageRunner

PLAN_VERSION = 1


@dataclass
class RegionPlan:
    name: str
    slug: str
    bbox: tuple | None = None  # (min_lon, min_lat, max_lon, max_lat) of the region's core + overlap polygon
    countries: list[str] = field(default_factory=list)  # upper-case ISO2 (last part) of the countries whose Overture source is on
    feeds: list[dict] = field(default_factory=list)  # {key, url, label}

    @property
    def overture(self) -> bool:
        return bool(self.countries)

    def keys(self, theme: str) -> str:
        return f"overture:{self.slug}-{theme}"


@dataclass
class OverturePlan:
    regions: list[RegionPlan] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def fingerprint(plan: OverturePlan) -> str:
    content = [[r.name, r.slug, [round(v, 4) for v in r.bbox] if r.bbox else None, sorted(r.countries), sorted((f["key"], f["url"]) for f in r.feeds), PLAN_VERSION]
               for r in plan.regions]
    return hashlib.sha256(json.dumps(content).encode()).hexdigest()[:16]


def _bbox(asset) -> tuple | None:
    try:
        return tuple(shape(json.loads(assets.abs_path(asset).with_suffix(".geojson").read_text())).bounds)
    except (OSError, ValueError, KeyError):
        return None


def plan_inputs(session, config_id: int, resolved: dict, regions: list[str] | None = None) -> OverturePlan:
    """The regions with an Overture-enabled country or GTFS feeds; the bbox comes from the approved overlap polygon (core
    polygon for a region without overlap countries). Every region that cannot be downloaded yet gives a blocked reason."""
    plan = OverturePlan()
    wanted = set(regions) if regions else None
    known = set()
    for region in resolved.get("regions", []):
        if not region["core"]:
            continue
        known.add(region["name"])
        if wanted is not None and region["name"] not in wanted:
            continue
        item = RegionPlan(region["name"], slug(region["name"]))
        item.countries = sorted({c["iso2"].split("-")[-1].upper() for c in [*region["core"], *region["overlap"]] if c.get("overture") and c.get("geofabrik_path")})
        item.feeds = [{"key": f"gtfs:{item.slug}-{f['id']}", "url": f["url"], "label": f.get("label") or f["url"]} for f in region.get("gtfs_feeds", [])]
        if not item.overture and not item.feeds:
            continue
        if item.overture:
            kind = "overlap" if region["overlap"] else "core"
            asset = assets.current(session, config_id, f"{kind}-polygon", f"{item.slug}-{kind}")
            item.bbox = _bbox(asset) if asset is not None else None
            if item.bbox is None:
                plan.problems.append(f"{item.name}: no approved {kind} polygon (run and approve Region polygons)")
        plan.regions.append(item)
    for name in sorted((wanted or set()) - known):
        plan.problems.append(f"Unknown region or no core countries: {name}")
    if not plan.regions and not plan.problems:
        plan.problems.append("No region has a country with Overture enabled or a GTFS feed.")
    return plan


def tool_problem(session) -> str | None:
    status = tools.detect(session, "overturemaps", force=True)
    return None if status.ok else f"overturemaps is not available ({status.message or status.status}); see Settings → Tools."


def _bbox_hash(bbox: tuple) -> str:
    return hashlib.sha256(",".join(f"{v:.4f}" for v in bbox).encode()).hexdigest()[:8]


def valid_gtfs(path: Path) -> bool:
    try:
        with zipfile.ZipFile(path) as archive:
            return "stops.txt" in archive.namelist()
    except (zipfile.BadZipFile, OSError):
        return False


class DownloadOvertureStage(StageRunner):
    """Per region, versioned downloads for the Pelias import: Overture places and addresses of the region's bounding box
    (core + overlap polygon), converted to the CSV files the csv-importer reads (`overture:<slug>-places|addresses`), and
    the region's GTFS feeds (`gtfs:<slug>-<feed id>`). Overture needs the `overturemaps` CLI (Settings → Tools). A release
    that is already downloaded for the same area is not fetched again. A failure of one source is a warning; the run fails
    only when nothing could be downloaded. `params.regions` limits a run."""

    key = "download-overture-gtfs"
    consumes = (StageIO("core-polygon"), StageIO("overlap-polygon"))
    consumes_downloads = ("overture", "gtfs")

    def run(self, context: StageRunContext) -> StageResult:
        if context.config_resolved is None or context.config_id is None:
            raise ValidationError("download-overture-gtfs needs a configuration")
        session = SessionLocal()
        plan = plan_inputs(session, context.config_id, context.config_resolved, context.params.get("regions"))
        if plan.problems:
            return StageResult("failed", report={"error": "; ".join(plan.problems), "problems": plan.problems})
        run_id = int(context.run_id)
        binary = None
        if any(r.overture for r in plan.regions):
            problem = tool_problem(session)
            if problem:
                return StageResult("failed", report={"error": problem})
            binary = tools.detect(session, "overturemaps", force=False).path
        work = Path(context.work_dir)
        work.mkdir(parents=True, exist_ok=True)
        rows, warnings = [], []
        release = None
        try:
            for region in plan.regions:
                if region.overture:
                    try:
                        release = release or overture.latest_release(binary)
                    except overture.OvertureError as exc:
                        warnings.append(f"{region.name}: {exc}")
                        rows += [{"key": region.keys(t), "kind": "overture", "label": f"Overture {t} {region.name}", "status": "failed", "detail": str(exc)} for t in overture.THEMES]
                    else:
                        for theme in overture.THEMES:
                            row = self._theme(session, context, run_id, binary, region, theme, release, work)
                            rows.append(row)
                            if row["status"] == "failed":
                                warnings.append(f"{region.name}: Overture {theme}: {row['detail']}")
                for feed in region.feeds:
                    row = self._feed(session, run_id, feed)
                    rows.append(row)
                    if row["status"] in ("failed", "invalid"):
                        warnings.append(f"{region.name}: GTFS feed {feed['label']}: {row['detail']}")
        finally:
            shutil.rmtree(work, ignore_errors=True)
        done = [r for r in rows if r["status"] in ("downloaded", "unchanged")]
        report = {"summary": {"regions": len(plan.regions), "sources": len(rows), "fetched": sum(r["status"] == "downloaded" for r in rows),
                              "unchanged": sum(r["status"] == "unchanged" for r in rows), "failed": len(rows) - len(done),
                              "release": release, "fingerprint": fingerprint(plan)},
                  "overture_sources": rows, "warnings": warnings,
                  "record_ids": [r["record_id"] for r in done if r.get("record_id")]}  # what approving the run approves (downloads.apply_review)
        if not done:
            report["error"] = "Nothing could be downloaded."
        return StageResult("success" if done else "failed", report=report)

    def _theme(self, session, context, run_id: int, binary: str, region: RegionPlan, theme: str, release: str, work: Path) -> dict:
        key = region.keys(theme)
        row = {"key": key, "kind": "overture", "label": f"Overture {theme} {region.name}", "status": "pending", "detail": ""}
        filename = f"{theme}-{release}-{_bbox_hash(region.bbox)}.csv"
        newest = (downloads.versions(session, key) or [None])[0]
        if newest is not None and newest.filename == filename and downloads.abs_path(newest).exists():
            return {**row, "status": "unchanged", "record_id": newest.id, "detail": f"release {release}"}
        context.step_cb(f"{region.name}: Overture {theme}")
        raw = work / f"{region.slug}-{theme}.geojsonseq"
        try:
            overture.download(binary, region.bbox, overture.THEMES[theme], release, raw)
            target = Path(config.DATA_ROOT) / "downloads" / "overture" / f"{region.slug}-{theme}" / f"{release}-{_bbox_hash(region.bbox)}" / filename
            counts = overture.CONVERTERS[theme](raw, target, set(region.countries))
            record = downloads.register_file(session, key, target, run_id)
        except (overture.OvertureError, OSError) as exc:
            return {**row, "status": "failed", "detail": str(exc)}
        finally:
            raw.unlink(missing_ok=True)
        return {**row, "status": "downloaded", "record_id": record.id, "detail": f"release {release}", **counts}

    def _feed(self, session, run_id: int, feed: dict) -> dict:
        row = {"key": feed["key"], "kind": "gtfs", "label": f"GTFS {feed['label']}", "status": "pending", "detail": ""}
        try:
            outcome = downloads.fetch(session, feed["key"], feed["url"], run_id=run_id, timeout=60)
        except DownloadError as exc:
            return {**row, "status": "failed", "detail": exc.message}
        if not valid_gtfs(downloads.abs_path(outcome.record)):
            return {**row, "status": "invalid", "record_id": outcome.record.id, "detail": "not a zip with stops.txt"}
        return {**row, "status": outcome.status, "record_id": outcome.record.id}
