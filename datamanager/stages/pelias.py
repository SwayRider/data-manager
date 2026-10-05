import hashlib
import json
import shutil
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from datamanager.config import config
from datamanager.db import SessionLocal
from datamanager.errors import ValidationError
from datamanager.models import BuildRun
from datamanager.services import assets, downloads, pelias_build, pelias_data, pelias_es
from datamanager.services import settings as settings_service
from datamanager.services import wof_patch
from datamanager.services.polygons import slug
from datamanager.services.valhalla_build import BuildError
from datamanager.stages.contract import StageIO, StageResult, StageRunContext, StageRunner

PLAN_VERSION = 3  # bump when the layout of the imported data or the produced assets changes
ASSET_TYPES = {"snapshot": "pelias-index-snapshot", "config": "pelias-config", "wof": "pelias-wof"}
LOG_TAIL = 15


@dataclass
class CountryInput:
    iso2: str
    wof_code: str
    admin: object  # Asset (wof-patched-sqlite, plain SQLite) or DownloadRecord (bz2 bundle)
    geonames: object  # DownloadRecord
    postal: object | None = None  # DownloadRecord of the postal-code bundle

    @property
    def patched(self) -> bool:
        return hasattr(self.admin, "asset_type")


@dataclass
class RegionPlan:
    name: str
    slug: str
    pbf: object | None = None  # osm-pbf Asset
    polylines: object | None = None  # valhalla-polylines Asset
    countries: list[CountryInput] = field(default_factory=list)
    openaddresses: dict[str, object] = field(default_factory=dict)  # source -> DownloadRecord
    overture: dict[str, object] = field(default_factory=dict)  # theme (places|addresses) -> DownloadRecord of the CSV
    gtfs: dict[str, object] = field(default_factory=dict)  # feed name -> DownloadRecord of the zip
    warnings: list[str] = field(default_factory=list)
    fingerprint: str = ""


@dataclass
class PeliasPlan:
    regions: list[RegionPlan] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def _hash(parts: list) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()[:16]


def environment_problems(session) -> list[str]:
    """What must be in place before any region is imported: the built importers, docker and the kernel setting."""
    problems = []
    found = pelias_build.detect(session)
    if found["status"] not in ("ok", "outdated"):
        problems.append("The Pelias importers are not built (Settings → Tools). " + found["message"])
    try:
        pelias_es.docker_bin(session, force=False)
    except ValidationError as exc:
        problems.append(exc.message)
    problems += pelias_es.environment_problems()
    return problems


def _approved(record) -> bool:
    return record is not None and record.status != "fetched"


def plan_inputs(session, config_id: int, resolved: dict, regions: list[str] | None = None) -> PeliasPlan:
    """What each region is imported from and every reason it cannot be yet. A region's fingerprint covers its PBF and
    polylines, the WOF databases, GeoNames and OpenAddresses versions, the importer commits and the Elasticsearch version."""
    plan = PeliasPlan(problems=environment_problems(session))
    es_version = str(settings_service.get(session, "tool.elasticsearch_version"))
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
            iso2, code = country["iso2"], str(country["wof_code"]).lower()
            admin = assets.current(session, None, "wof-patched-sqlite", iso2.lower())
            if admin is None:
                admin = downloads.resolve_version(session, f"wof:admin-{code}")
                if not _approved(admin):
                    plan.problems.append(f"{item.name}: no approved Who's On First bundle for {iso2} (run Download Pelias data)")
                    continue
            geonames = downloads.resolve_version(session, f"geonames:{iso2.split('-')[-1].lower()}")
            if not _approved(geonames):
                plan.problems.append(f"{item.name}: no approved GeoNames file for {iso2} (run Download Pelias data)")
                continue
            postal = downloads.resolve_version(session, f"wof:postalcode-{code}")
            if not _approved(postal):
                postal = None
                item.warnings.append(f"{iso2}: no approved WOF postal-code bundle; postal codes are not imported for it.")
            item.countries.append(CountryInput(iso2, code, admin, geonames, postal))
            for source in country.get("openaddresses", []):
                record = downloads.resolve_version(session, f"openaddresses:{source}")
                if _approved(record):
                    item.openaddresses[source] = record
                else:
                    item.warnings.append(f"{iso2}: OpenAddresses source {source} has no approved download and is skipped.")
        if any(c.get("overture") and c.get("geofabrik_path") for c in [*region["core"], *region["overlap"]]):
            for theme in pelias_data.OVERTURE_FILES:
                record = downloads.resolve_version(session, f"overture:{item.slug}-{theme}")
                if _approved(record):
                    item.overture[theme] = record
                else:
                    item.warnings.append(f"No approved Overture {theme} for {item.name} (run Download Overture & GTFS): not imported.")
        for feed in region.get("gtfs_feeds", []):
            name = f"{item.slug}-{feed['id']}"
            record = downloads.resolve_version(session, f"gtfs:{name}")
            if _approved(record):
                item.gtfs[name] = record
            else:
                item.warnings.append(f"GTFS feed {feed.get('label') or feed['url']} has no approved download (run Download Overture & GTFS): not imported.")
        if item.pbf is not None and item.polylines is not None and item.countries:
            item.fingerprint = _hash([
                item.pbf.content_hash, item.polylines.content_hash,
                [[c.iso2, c.admin.content_hash, c.geonames.content_hash, c.postal.content_hash if c.postal else None] for c in item.countries],
                sorted([s, r.content_hash] for s, r in item.openaddresses.items()),
                sorted([s, r.content_hash] for s, r in item.overture.items()), sorted([s, r.content_hash] for s, r in item.gtfs.items()),
                pelias_build.version(session), es_version, PLAN_VERSION])
        plan.regions.append(item)
    for name in sorted((wanted or set()) - known):
        plan.problems.append(f"Unknown region or no core countries: {name}")
    if not plan.regions and not plan.problems:
        plan.problems.append("The configuration has no region with a core country.")
    return plan


def pelias_fingerprint(plan: PeliasPlan) -> str:
    return _hash([[r.name, r.fingerprint] for r in plan.regions])


def _log_tail(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()[-LOG_TAIL:]
    except OSError:
        return []


class PeliasStage(StageRunner):
    """Per region, a Pelias index imported into a temporary Elasticsearch container (`services/pelias_es.py`) from the
    approved region PBF, edge polylines, Who's On First databases (the patched one when `wof-patch` made it), GeoNames per
    country and OpenAddresses sources, with the importers built under Settings → Tools. Produces the Elasticsearch snapshot
    of that index, the production `pelias.json` and the WOF directory the PIP service reads (`pelias-*` assets, name = region
    slug). The container and its bind-mounted directories are removed whenever the run ends. A region whose inputs did not
    change since its approved assets is skipped; `params.regions` limits a run. Deploy (restore, alias switch) is not here."""

    key = "pelias"
    produces = tuple(StageIO(t) for t in ASSET_TYPES.values())
    consumes = (StageIO("osm-pbf"), StageIO("valhalla-polylines"))  # wof-patched-sqlite is optional: the plain download is used without it
    consumes_downloads = ("wof", "geonames", "openaddresses")

    def run(self, context: StageRunContext) -> StageResult:
        if context.config_resolved is None or context.config_id is None:
            raise ValidationError("pelias needs a configuration")
        session = SessionLocal()
        plan = plan_inputs(session, context.config_id, context.config_resolved, context.params.get("regions"))
        if plan.problems:
            return StageResult("failed", report={"error": "; ".join(plan.problems), "problems": plan.problems})
        run_id = int(context.run_id)
        docker = pelias_es.docker_bin(session)
        work = Path(context.work_dir)
        work.mkdir(parents=True, exist_ok=True)
        es_work_dir = str(settings_service.get(session, "pelias.es_work_dir"))
        es_base = (Path(es_work_dir) / str(run_id)) if es_work_dir else work / "es"
        context.step_cb("Removing leftovers of earlier runs")
        active = {str(r) for (r,) in session.query(BuildRun.id).filter(BuildRun.status.in_(("queued", "running")))} | {str(run_id)}
        removed = pelias_es.cleanup_stale(docker, active, pelias_es.stale_candidates(es_work_dir, Path(config.DATA_ROOT)))
        out_root = assets.asset_dir("pelias", run_id)
        entries, failed = [], []
        try:
            for region in plan.regions:
                entry = self._region(session, plan, region, context, run_id, docker, work, es_base, out_root)
                entries.append(entry)
                if entry["status"] == "failed":
                    failed.append(region.name)
        finally:
            shutil.rmtree(work, ignore_errors=True)
            if es_work_dir:
                shutil.rmtree(es_base, ignore_errors=True)
        if failed:
            assets.discard(session, run_id)
        report = {
            "summary": {"regions": len(entries), "failed": len(failed), "built": sum(e.get("result") == "built" for e in entries),
                        "unchanged": sum(e.get("result") == "unchanged" for e in entries), "fingerprint": pelias_fingerprint(plan),
                        "pelias": pelias_build.version(session), "elasticsearch": str(settings_service.get(session, "tool.elasticsearch_version"))},
            "pelias": entries,
            "warnings": [w for e in entries for w in e.get("warnings", [])],
            "cleaned_up": removed,
        }
        if failed:
            report["error"] = "Failed: " + ", ".join(failed)
        return StageResult("failed" if failed else "success", report=report)

    def _region(self, session, plan, region: RegionPlan, context, run_id: int, docker: str, work: Path, es_base: Path, out_root: Path) -> dict:
        entry = {"name": region.name, "status": "success", "warnings": list(region.warnings), "importers": []}
        current = {k: assets.current(session, context.config_id, t, region.slug) for k, t in ASSET_TYPES.items()}
        if all(a is not None and a.meta_json.get("fingerprint") == region.fingerprint and assets.abs_path(a).exists() for a in current.values()):
            return {**entry, "result": "unchanged", "asset_ids": {k: a.id for k, a in current.items()},
                    "index": current["snapshot"].meta_json.get("index_name"), "docs": current["snapshot"].meta_json.get("docs")}
        started = time.monotonic()
        layout = pelias_data.Layout(work / region.slug, region.slug)
        log = layout.logs / "import.log"
        try:
            version = str(settings_service.get(session, "tool.elasticsearch_version"))
            pelias_es.preflight(docker, version, es_base, need_gb=20.0)
            context.step_cb(f"{region.name}: laying out the data")
            countries = [pelias_data.CountryData(
                c.iso2, c.wof_code, assets.abs_path(c.admin) if c.patched else downloads.abs_path(c.admin), not c.patched,
                downloads.abs_path(c.geonames), downloads.abs_path(c.postal) if c.postal else None) for c in region.countries]
            oa_files = {s: downloads.abs_path(r) for s, r in region.openaddresses.items()}
            overture_files = {theme: downloads.abs_path(r) for theme, r in region.overture.items()}
            pelias_data.prepare(layout, countries, oa_files, assets.abs_path(region.pbf), assets.abs_path(region.polylines), overture_files)
            try:
                transit_feeds = pelias_data.prepare_gtfs(layout, [(n, downloads.abs_path(r)) for n, r in region.gtfs.items()])
            except (OSError, KeyError, zipfile.BadZipFile) as exc:
                transit_feeds = []
                entry["warnings"].append(f"{region.name}: GTFS feeds not imported: {exc}")
            index = f"pelias_{region.slug}-{run_id}"
            wof_codes = [c.wof_code for c in countries]
            snapshot_name = index
            out_dir = out_root / region.slug
            out_dir.mkdir(parents=True, exist_ok=True)
            with pelias_es.TemporaryElasticsearch(docker, version, str(settings_service.get(session, "pelias.es_heap")), run_id,
                                                  es_base / region.slug) as es:
                load = pelias_data.render_config(layout, index=index, es_host="127.0.0.1", es_port=es.port, wof_codes=wof_codes,
                                                 openaddresses=sorted(oa_files),
                                                 csv_files=[pelias_data.OVERTURE_FILES[theme] for theme in overture_files], transit_feeds=transit_feeds)
                config_path = pelias_data.write_config(load, layout.configs / "pelias.json")
                previous = 0
                for name in pelias_data.IMPORTERS:
                    if (name == "openaddresses" and not oa_files) or (name == "csv-importer" and not overture_files) or (name == "transit" and not transit_feeds):
                        continue
                    context.step_cb(f"{region.name}: {name}")
                    began = time.monotonic()
                    repo = pelias_build.require(session, name)
                    if name == "geonames":
                        for country in countries:
                            path = pelias_data.write_config(pelias_data.geonames_config(load, layout, country.geonames_code),
                                                            layout.configs / f"geonames-{country.geonames_code.lower()}.json")
                            pelias_data.run_importer(name, repo, path, log, f"geonames {country.geonames_code}")
                    else:
                        try:
                            pelias_data.run_importer(name, repo, config_path, log)
                        except BuildError as exc:
                            if name not in pelias_data.OPTIONAL_IMPORTERS:
                                raise
                            entry["warnings"].append(f"{region.name}: {name} failed and is skipped (documents it already added stay in the index): {exc}")
                            entry["importers"].append({"name": name, "seconds": round(time.monotonic() - began), "docs": 0, "total": previous, "error": str(exc)})
                            continue
                    es.refresh(index)  # the index refreshes only once a minute: without it the count lags behind
                    docs = es.count(index)
                    if name in pelias_data.OPTIONAL_IMPORTERS and docs == previous:  # an importer may exit 0 after rejecting its config
                        entry["warnings"].append(f"{region.name}: {name} added no documents; check its input (see the import log).")
                    entry["importers"].append({"name": name, "seconds": round(time.monotonic() - began), "docs": docs - previous, "total": docs})
                    previous = docs
                es.refresh(index)
                docs = es.count(index)
                context.step_cb(f"{region.name}: snapshot")
                repository = es.snapshot(index, snapshot_name)
                snapshot_file = pelias_data.tar_directory(repository, out_dir / f"{region.slug}.es-snapshot.tar")
            wof_file = pelias_data.tar_directory(layout.wof_sqlite, out_dir / "wof.tar.gz", gz=True)
            prod = pelias_data.render_config(layout, index=index, es_host="elasticsearch", es_port=9200, wof_codes=wof_codes,
                                             openaddresses=sorted(oa_files), prod=True)
            config_file = pelias_data.write_config(prod, out_dir / "pelias.json")
            meta = {"region": region.name, "fingerprint": region.fingerprint, "index_name": index, "snapshot_name": snapshot_name,
                    "snapshot_repository": pelias_es.SNAPSHOT_REPO, "elasticsearch": version, "docs": docs,
                    "pelias": pelias_build.version(session), "wof_patched": [c.iso2 for c in region.countries if c.patched],
                    "countries": [c.iso2 for c in region.countries]}
            ids = {}
            for key, file in (("snapshot", snapshot_file), ("config", config_file), ("wof", wof_file)):
                ids[key] = assets.create(session, run_id, context.config_id, ASSET_TYPES[key], region.slug, file, meta=meta).id
            entry.update(result="built", asset_ids=ids, index=index, docs=docs, snapshot_bytes=snapshot_file.stat().st_size,
                         seconds=round(time.monotonic() - started))
            if docs == 0:
                entry["warnings"].append(f"{region.name}: the index is empty.")
        except (BuildError, pelias_es.EsError, wof_patch.PatchError, OSError, ValidationError) as exc:
            entry.update(status="failed", message=str(exc), log=_log_tail(log))
        return entry
