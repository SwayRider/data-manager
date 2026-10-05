import hashlib
import json
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from datamanager.db import SessionLocal
from datamanager.errors import ValidationError
from datamanager.models import Country
from datamanager.services import assets, downloads, osmium, wof_patch
from datamanager.services import boundary_sources as boundary_source_service
from datamanager.services import settings as settings_service
from datamanager.stages.contract import StageIO, StageResult, StageRunContext, StageRunner
from datamanager.stages.download_osm import osm_source_key
from datamanager.stages.osm_extract import Input

ASSET_TYPE = "wof-patched-sqlite"
REVIEW_LIST = 300  # records of the "unreplaced" list shown in the report (the count is always complete)
OFFICIAL_FORMATS = ("geojson", "shapefile-zip")


@dataclass
class CountryPlan:
    iso2: str
    name: str
    path: str  # Geofabrik path
    code: str  # WOF country code
    pbf: Input | None = None
    wof: object | None = None  # DownloadRecord of the WOF admin bundle
    official: object | None = None  # DownloadRecord of the official polygons
    official_cfg: dict | None = None
    levels: list[int] = field(default_factory=list)
    admin_levels: list[int] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def fingerprint(self) -> str:
        content = {
            "version": wof_patch.PATCH_VERSION,
            "pbf": [self.pbf.download_ids, self.pbf.asset.id if self.pbf.asset is not None else None] if self.pbf else None,
            "wof": self.wof.id if self.wof else None,
            "official": [self.official.id, (self.official_cfg or {}).get("name_field")] if self.official else None,
            "levels": self.levels, "admin_levels": self.admin_levels,
        }
        return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()[:16]


@dataclass
class Plan:
    countries: list[CountryPlan] = field(default_factory=list)
    untouched: list[str] = field(default_factory=list)  # countries without a locality boundary source
    problems: list[str] = field(default_factory=list)


def _countries(resolved: dict) -> dict[str, dict]:
    found: dict[str, dict] = {}
    for region in resolved.get("regions", []):
        for country in [*region["core"], *region["overlap"]]:
            if country.get("geofabrik_path"):
                found.setdefault(country["iso2"], country)
    return found


def plan_inputs(session, resolved: dict | None, detect: list[str] | None = None, overrides: dict | None = None) -> Plan:
    """The countries to patch (or, with `detect`, to measure) and what each is built from. Problems list every missing
    approved input: the country PBF, the WOF bundle, an official polygon download."""
    plan = Plan()
    if detect:
        rows = {iso: session.get(Country, iso) for iso in detect}
        selected = {iso: {"iso2": iso, "name": c.name, "geofabrik_path": c.geofabrik_path, "wof_code": c.wof_code}
                    for iso, c in rows.items() if c is not None and c.geofabrik_path}
        for iso in set(detect) - set(selected):
            plan.problems.append(f"{iso}: unknown country or no Geofabrik path")
    else:
        selected = _countries(resolved or {})
    source = str(settings_service.get(session, "osm.source"))
    for iso2, country in sorted(selected.items()):
        row = session.get(Country, iso2)
        osm = boundary_source_service.get_state(row, "osm_admin") if row else None
        official = boundary_source_service.get_state(row, "official_polygons") if row else None
        item = CountryPlan(iso2, country["name"], country["geofabrik_path"], str(country["wof_code"]).lower())
        if osm is not None and osm.enabled:
            item.levels = [int(v) for v in osm.config.get("levels", [])]
            item.admin_levels = [int(v) for v in osm.config.get("localadmin_levels", [])]
        if official is not None and official.enabled and official.config.get("url"):
            if official.config.get("format") in OFFICIAL_FORMATS:
                item.official_cfg = dict(official.config)
            else:
                item.notes.append(f"Official polygons in format {official.config.get('format')!r} are not supported yet: ignored.")
        if not detect and not (item.levels or item.admin_levels or item.official_cfg):
            plan.untouched.append(iso2)
            continue
        if source == "planet":
            asset = assets.current(session, None, "country-pbf", item.path)
            if asset is None:
                plan.problems.append(f"{iso2}: no approved country extract of {item.path} (run Extract countries)")
            else:
                item.pbf = Input(iso2, "core", osm_source_key(item.path), asset=asset)
        else:
            record = downloads.resolve_version(session, osm_source_key(item.path), overrides)
            if record is None or record.status == "fetched":
                plan.problems.append(f"{iso2}: no approved download of {item.path}")
            else:
                item.pbf = Input(iso2, "core", osm_source_key(item.path), record=record)
        item.wof = downloads.resolve_version(session, f"wof:admin-{item.code}", overrides)
        if item.wof is None or item.wof.status == "fetched":
            plan.problems.append(f"{iso2}: no approved Who's On First bundle (run Download Pelias data and approve it)")
            item.wof = None
        if item.official_cfg and not detect:
            item.official = downloads.resolve_version(session, f"official:{iso2.lower()}", overrides)
            if item.official is None or item.official.status == "fetched":
                plan.problems.append(f"{iso2}: no approved official polygon download (run Download Pelias data and approve it)")
                item.official = None
        plan.countries.append(item)
    if not detect and not plan.countries and not plan.problems:
        plan.problems.append("No country has a locality boundary source (right-click a country on the Configure map).")
    return plan


def wof_patch_fingerprint(plan: Plan) -> str:
    return hashlib.sha256(json.dumps(sorted((c.iso2, c.fingerprint) for c in plan.countries)).encode()).hexdigest()[:16]


class WofPatchStage(StageRunner):
    """Replaces Who's On First locality (and municipality) polygons by OSM administrative boundaries or an official
    polygon file, per country that has such a source (`services/wof_patch.py` has the rules). Produces the patched
    WOF SQLite (`wof-patched-sqlite`, configuration independent, named after the ISO2 code) from the approved country
    PBF and the approved WOF bundle; a country whose inputs and configuration did not change is skipped.

    With `params.detect = [iso2, ...]` nothing is patched: the OSM admin levels of those countries are measured against
    WOF and a suggestion for the municipality and locality level goes into the report."""

    key = "wof-patch"
    produces = (StageIO(ASSET_TYPE),)
    consumes_downloads = ("wof", "official")

    def run(self, context: StageRunContext) -> StageResult:
        session = SessionLocal()
        detect = [str(c).lower() for c in context.params.get("detect") or []]
        if not detect and context.config_resolved is None:
            raise ValidationError("wof-patch needs a configuration")
        plan = plan_inputs(session, context.config_resolved, detect or None)
        if plan.problems:
            return StageResult("failed", report={"error": "; ".join(plan.problems), "problems": plan.problems})
        exe = osmium.binary(session)
        work = Path(context.work_dir)
        work.mkdir(parents=True, exist_ok=True)
        run_id = int(context.run_id)
        entries, failed = [], []
        try:
            for country in plan.countries:
                entry = (self._detect if detect else self._patch)(session, country, exe, work / country.iso2, context, run_id)
                entries.append(entry)
                if entry["status"] == "failed":
                    failed.append(country.iso2)
        finally:
            shutil.rmtree(work, ignore_errors=True)
        if failed:
            assets.discard(session, run_id)
        warnings = [w for e in entries for w in e.get("warnings", [])]
        report: dict = {"warnings": warnings}
        if detect:
            report["detect"] = entries
        else:
            report["patch"] = entries
            report["summary"] = {"countries": len(entries), "patched": sum(e.get("result") == "patched" for e in entries),
                                 "unchanged": sum(e.get("result") == "unchanged" for e in entries), "failed": len(failed),
                                 "untouched": plan.untouched, "fingerprint": wof_patch_fingerprint(plan)}
        if failed:
            report["error"] = "Failed: " + ", ".join(failed)
        return StageResult("failed" if failed else "success", report=report)

    # ---- one country ----------------------------------------------------------------------------------------

    def _patch(self, session, country: CountryPlan, exe: str, work: Path, context, run_id: int) -> dict:
        entry = {"iso2": country.iso2, "name": country.name, "status": "success", "warnings": list(country.notes),
                 "levels": country.levels, "localadmin_levels": country.admin_levels,
                 "official": bool(country.official_cfg)}
        current = assets.current(session, None, ASSET_TYPE, country.iso2.lower())
        if current is not None and current.meta_json.get("fingerprint") == country.fingerprint and assets.usable(current):
            return {**entry, "result": "unchanged", "asset_id": current.id, "stats": current.meta_json.get("stats", {})}
        started = time.monotonic()
        try:
            context.step_cb(f"{country.iso2}: unpacking the WOF bundle")
            unpacked = wof_patch.unpack_wof(downloads.abs_path(country.wof), work / "wof.db")
            context.step_cb(f"{country.iso2}: reading boundaries")
            locality, localadmin = [], []
            osm_levels = sorted({*country.levels, *country.admin_levels})
            osm = wof_patch.read_osm_boundaries(exe, country.pbf.file, osm_levels, work / "osm") if osm_levels else []
            localadmin = [b for b in osm if b.level in country.admin_levels]
            if country.official_cfg:
                locality = wof_patch.read_official(downloads.abs_path(country.official), country.official_cfg["name_field"], work)
                if country.levels:
                    entry["warnings"].append(f"{country.iso2}: the official polygons replace the OSM locality levels {country.levels}.")
            else:
                locality = [b for b in osm if b.level in country.levels]
            context.step_cb(f"{country.iso2}: patching")
            out = assets.asset_dir("wof", run_id) / country.code / f"whosonfirst-data-admin-{country.code}-latest.db"
            patch = wof_patch.patch_country(unpacked, locality, localadmin, out)
            meta = {"iso2": country.iso2, "fingerprint": country.fingerprint, "stats": patch.stats, "patch_version": wof_patch.PATCH_VERSION,
                    "wof_record_id": country.wof.id, "source_levels": country.levels, "source_admin_levels": country.admin_levels}
            ids = [country.wof.id, *([country.official.id] if country.official else []), *country.pbf.download_ids]
            asset = assets.create(session, run_id, None, ASSET_TYPE, country.iso2.lower(), out, meta=meta, source_download_ids=ids)
            entry.update(result="patched", asset_id=asset.id, stats=patch.stats, skipped=patch.skipped, seconds=round(time.monotonic() - started),
                         unreplaced_total=len(patch.unreplaced), unreplaced=patch.unreplaced[:REVIEW_LIST], bytes=out.stat().st_size)
            if not patch.new:
                entry["warnings"].append(f"{country.iso2}: no boundary polygon was found; the bundle is unchanged.")
            for placetype, stats in patch.stats.items():
                if stats.get("ambiguous"):
                    entry["warnings"].append(f"{country.iso2}: {stats['ambiguous']} new {placetype} polygon(s) overlap two parents about equally.")
            if patch.skipped.get("no_parent"):
                entry["warnings"].append(f"{country.iso2}: {patch.skipped['no_parent']} polygon(s) found no parent and were left out.")
        except (wof_patch.PatchError, osmium.OsmiumError, OSError) as exc:
            entry.update(status="failed", message=str(exc))
        return entry

    def _detect(self, session, country: CountryPlan, exe: str, work: Path, context, run_id: int) -> dict:
        entry = {"iso2": country.iso2, "name": country.name, "status": "success", "warnings": [],
                 "current": {"levels": country.levels, "localadmin_levels": country.admin_levels}}
        try:
            context.step_cb(f"{country.iso2}: unpacking the WOF bundle")
            unpacked = wof_patch.unpack_wof(downloads.abs_path(country.wof), work / "wof.db")
            context.step_cb(f"{country.iso2}: reading admin levels {wof_patch.LEVELS.start}-{wof_patch.LEVELS.stop - 1}")
            boundaries = wof_patch.read_osm_boundaries(exe, country.pbf.file, wof_patch.LEVELS, work / "osm")
            entry.update(wof_patch.detect_levels(unpacked, boundaries))
            if not entry["suggest"]["levels"] and not entry["suggest"]["localadmin_levels"]:
                entry["warnings"].append(f"{country.iso2}: no level could be suggested; read the table and decide.")
        except (wof_patch.PatchError, osmium.OsmiumError, OSError) as exc:
            entry.update(status="failed", message=str(exc))
        return entry
