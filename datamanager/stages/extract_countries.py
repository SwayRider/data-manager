from pathlib import Path

from datamanager.db import SessionLocal
from datamanager.errors import DownloadError, ValidationError
from datamanager.services import assets, downloads, osmium, settings as settings_service
from datamanager.stages.contract import StageIO, StageResult, StageRunContext, StageRunner
from datamanager.stages.download_osm import planned_paths
from datamanager.stages.download_planet import PLANET_KEY

MIN_COVERAGE = 0.5  # the data bbox must cover at least this share of the polygon's bbox
GB_PER_COUNTRY = 3  # osmium's memory per region cut in the same run (measured ~2.6 GB on the planet)
SHRINK_WARN = 0.2  # warn when an extract has this much fewer objects than the previous version of the country


def poly_key(path: str) -> str:
    return f"poly:{path}"


def asset_file_name(path: str) -> str:
    return path.replace("/", "_") + ".osm.pbf"


def _total(counts: dict) -> int:
    return sum(int(v or 0) for v in counts.values())


class ExtractCountriesStage(StageRunner):
    """Cuts every country the configuration needs (core and overlap, by Geofabrik path) out of the
    approved planet with Geofabrik's published country polygons, in one pass over the planet. A country
    already extracted from this very planet version with the same polygon is not extracted again.
    Results are configuration-independent `country-pbf` assets (another configuration reuses them)."""

    key = "extract-countries"
    produces = (StageIO("country-pbf"),)
    consumes_downloads = ("planet", "poly")

    def run(self, context: StageRunContext) -> StageResult:
        if context.config_resolved is None:
            raise ValidationError("extract-countries needs a configuration")
        session = SessionLocal()
        exe = osmium.binary(session)
        planet = downloads.resolve_version(session, PLANET_KEY, context.input_versions)
        if planet is None or planet.status == "fetched" or not downloads.abs_path(planet).exists():
            return StageResult("failed", report={"error": "No approved planet version: run Download planet and approve it first."})

        paths = planned_paths(context.config_resolved)
        wanted = context.params.get("paths")
        if wanted:
            unknown = sorted(set(wanted) - set(paths))
            if unknown:
                return StageResult("failed", report={"error": "Not needed by this configuration: " + ", ".join(unknown)})
            paths = sorted(set(wanted))
        if not paths:
            return StageResult("failed", report={"error": "The configuration has no country with a Geofabrik path."})

        base = str(settings_service.get(session, "download.country_polys")).rstrip("/")
        run_id = int(context.run_id)
        entries: dict[str, dict] = {}
        poly_records: dict[str, object] = {}
        record_ids: list[int] = []
        todo: list[str] = []
        failures: list[str] = []

        context.step_cb("Fetching country polygons")
        for index, path in enumerate(paths, 1):
            context.progress_cb(index, len(paths), path)
            entry = entries[path] = {"path": path, "warnings": []}
            try:
                outcome = downloads.fetch(session, poly_key(path), f"{base}/{path}.poly", run_id=run_id, timeout=60)
            except DownloadError as exc:
                entry.update(status="failed", message=exc.message)
                failures.append(path)
                continue
            record = poly_records[path] = outcome.record
            record_ids.append(record.id)
            existing = assets.find_extract(session, "country-pbf", path, planet.id, record.content_hash)
            if existing is not None:
                entry.update(status="unchanged", asset_id=existing.id, size_bytes=existing.size_bytes,
                             counts=existing.meta_json.get("counts"), bbox=existing.meta_json.get("bbox"),
                             asset_status=existing.status)
            else:
                todo.append(path)

        work = Path(context.work_dir)
        budget_gb = int(settings_service.get(session, "run.extract_memory_gb"))
        min_free = int(settings_service.get(session, "run.extract_min_free_gb"))
        batch_size = max(1, int(budget_gb // GB_PER_COUNTRY))
        batches = [todo[i:i + batch_size] for i in range(0, len(todo), batch_size)] if not failures else []
        peak = 0
        try:
            out_dir = assets.asset_dir("country-pbf", run_id)
            for number, batch in enumerate(batches, 1):
                label = f"batch {number}/{len(batches)}"
                context.step_cb(f"Cutting {', '.join(batch)} from the planet ({label})")
                jobs = [(downloads.abs_path(poly_records[p]), work / asset_file_name(p)) for p in batch]
                try:
                    peak = max(peak, osmium.extract_many(
                        exe, downloads.abs_path(planet), jobs, work / f"cfg-{number}",
                        lambda pct, message, label=label: context.progress_cb(pct, 100, f"{label}: {message}"), min_free_gb=min_free,
                    ))
                except osmium.OsmiumError as exc:
                    for p in batch:
                        entries[p].update(status="failed", message=str(exc))
                    failures += batch
                    break  # earlier batches stay registered: a re-run only does what is missing
                for index, path in enumerate(batch, 1):
                    context.step_cb(f"Checking {path} ({index}/{len(batch)})")
                    self._register(session, exe, entries[path], path, work / asset_file_name(path), out_dir, planet,
                                   poly_records[path], run_id)
                    if entries[path]["status"] == "failed":
                        failures.append(path)
        finally:
            import shutil
            shutil.rmtree(work, ignore_errors=True)

        asset_ids = [e["asset_id"] for e in entries.values() if e.get("asset_id")]
        listed = [entries[p] for p in paths]
        report = {
            "summary": {
                "countries": len(paths),
                "extracted": sum(1 for e in listed if e.get("status") == "extracted"),
                "unchanged": sum(1 for e in listed if e.get("status") == "unchanged"),
                "failed": len(failures),
                "bytes": sum(e.get("size_bytes", 0) for e in listed if e.get("status") == "extracted"),
                "warnings": sum(len(e["warnings"]) for e in listed),
                "planet_version": planet.version_label, "planet_data_timestamp": planet.data_timestamp,
                "batches": len(batches), "batch_size": batch_size, "memory_budget_gb": budget_gb,
                "peak_memory_gb": round(peak / 1e9, 1),
            },
            "countries": listed,
            "record_ids": record_ids,
            "asset_ids": asset_ids,
        }
        if failures:
            reasons = sorted({entries[p].get("message", "") for p in set(failures)} - {""})
            report["error"] = "Failed: " + ", ".join(sorted(set(failures))) + (f" ({'; '.join(reasons)[:600]})" if reasons else "")
        return StageResult("failed" if failures else "success", report=report)

    def _register(self, session, exe, entry: dict, path: str, file: Path, out_dir: Path, planet, poly, run_id: int) -> None:
        try:
            final = out_dir / asset_file_name(path)
            file.replace(final)
            info = osmium.fileinfo(exe, final)
            previous = assets.current(session, None, "country-pbf", path)
            box = osmium.poly_bbox(downloads.abs_path(poly))
            warnings = entry["warnings"]
            total = _total(info["counts"])
            if not total:
                warnings.append("The extract is empty.")
            if info["ordered"] is False:
                warnings.append("The extract is not sorted.")
            if previous is not None and _total(previous.meta_json.get("counts", {})) and total < _total(previous.meta_json["counts"]) * (1 - SHRINK_WARN):
                warnings.append("More than 20 % fewer objects than the previous extract of this country.")
            bbox = info["bbox"]
            coverage = osmium.bbox_coverage(bbox, box)
            if coverage is not None and coverage < MIN_COVERAGE:
                warnings.append(f"The data covers only {coverage:.0%} of the country polygon's bounding box.")
            asset = assets.create(
                session, run_id, None, "country-pbf", path, final,
                meta={"planet_record_id": planet.id, "planet_version": planet.version_label,
                      "planet_data_timestamp": planet.data_timestamp, "poly_record_id": poly.id,
                      "poly_hash": poly.content_hash, "counts": info["counts"], "bbox": info["bbox"],
                      "replication_timestamp": info["replication_timestamp"]},
                source_download_ids=[planet.id, poly.id],
            )
            entry.update(status="extracted", asset_id=asset.id, size_bytes=asset.size_bytes, sha256=asset.content_hash,
                         counts=info["counts"], bbox=info["bbox"])
        except (osmium.OsmiumError, OSError) as exc:
            entry.update(status="failed", message=str(exc))
