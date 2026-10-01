from datamanager.db import SessionLocal
from datamanager.errors import DownloadError, ValidationError
from datamanager.services import downloads, pmtiles, settings as settings_service
from datamanager.stages.contract import StageResult, StageRunContext, StageRunner
from datamanager.stages.download_planet import SHRINK_WARN, _age_days

TILES_KEY = "tiles:planet"
STALE_DAYS = 14  # warn when the build is older than this
EXPECTED_LAYERS = {"boundaries", "earth", "landuse", "natural", "places", "pois", "roads", "transit", "water"}
MIN_MAX_ZOOM = 15


class DownloadTilesStage(StageRunner):
    """Fetches the newest daily Protomaps planet build (Z0–15, Protomaps basemap schema, one `.pmtiles` file of
    about 140 GB) as a new version unless upstream is unchanged, keeps only the newest `download.tiles_keep`
    versions, validates the PMTiles header and metadata, and ends in review. Needs no configuration.

    Params for testing: `use_existing` (newest version we have, no upstream check) and `local_file` (register a
    `.pmtiles` that is already on disk, in place)."""

    key = "download-tiles"
    consumes_downloads = ("tiles",)

    def run(self, context: StageRunContext) -> StageResult:
        session = SessionLocal()
        keep = int(settings_service.get(session, "download.tiles_keep"))
        connections = int(settings_service.get(session, "download.connections"))

        local_file = str(context.params.get("local_file") or "").strip()
        build = None
        if local_file:
            context.step_cb("Registering the tiles file")
            try:
                record = downloads.register_file(session, TILES_KEY, local_file, int(context.run_id), context.progress_cb)
            except ValidationError as exc:
                return StageResult("failed", report={"error": exc.message})
            status = "imported"
        elif context.params.get("use_existing"):
            record = next((r for r in downloads.versions(session, TILES_KEY) if r.status != "rejected"), None)
            if record is None or not downloads.abs_path(record).exists():
                return StageResult("failed", report={"error": "No tiles build is available yet; untick \"use the one I have\" to download one."})
            status = "existing"
        else:
            context.step_cb("Looking up the newest Protomaps build")
            try:
                build = pmtiles.newest_build(
                    str(settings_service.get(session, "download.tiles_builds")),
                    str(settings_service.get(session, "download.tiles_build")),
                )
                context.step_cb(f"Fetching build {build['key']}")
                outcome = downloads.fetch(
                    session, TILES_KEY, build["url"], run_id=int(context.run_id), progress_cb=context.progress_cb,
                    connections=connections, timeout=60,
                )
            except DownloadError as exc:
                return StageResult("failed", report={"error": exc.message, "url": getattr(exc, "url", None)})
            record, status = outcome.record, outcome.status

        warnings: list[str] = []
        context.step_cb("Reading the PMTiles header")
        try:
            info = pmtiles.read_info(downloads.abs_path(record))
        except (pmtiles.PmtilesError, OSError) as exc:
            return StageResult("failed", report={"error": f"Not a usable PMTiles file: {exc}"})
        if not record.data_timestamp:
            date = pmtiles.build_date(record.filename)
            if date:
                record.data_timestamp = date
                session.commit()

        if info["tile_type"] != "mvt":
            return StageResult("failed", report={"error": f"The file holds {info['tile_type']} tiles, vector (mvt) tiles are required."})
        if info["max_zoom"] < MIN_MAX_ZOOM:
            warnings.append(f"Maximum zoom is {info['max_zoom']}, expected {MIN_MAX_ZOOM}.")
        missing = sorted(EXPECTED_LAYERS - set(info["layers"]))
        if missing:
            warnings.append("Layers missing from the metadata: " + ", ".join(missing) + " (not the Protomaps basemap schema?).")
        west, south, east, north = info["bounds"]
        if (west, south, east, north) != (-180.0, -85.0, 180.0, 85.0) and (east - west < 359 or north - south < 169):
            warnings.append(f"The file does not cover the whole world (bounds {west:.1f},{south:.1f},{east:.1f},{north:.1f}).")

        previous = next((r for r in downloads.versions(session, TILES_KEY) if r.id != record.id and r.status != "rejected"), None)
        age = _age_days(record.data_timestamp)
        if age is not None and age > STALE_DAYS:
            warnings.append(f"The build is {age} days old.")
        if previous is not None and previous.size_bytes and record.size_bytes < previous.size_bytes * (1 - SHRINK_WARN):
            warnings.append("The file is more than 5 % smaller than the previous version.")

        context.step_cb("Removing old versions")
        pruned = downloads.prune(session, TILES_KEY, keep)

        metadata = info["metadata"]
        report = {
            "summary": {
                "label": "Protomaps tiles", "status": status, "version": record.version_label, "size_bytes": record.size_bytes,
                "data_timestamp": record.data_timestamp, "age_days": age, "md5_ok": None, "pruned": len(pruned), "kept": keep,
            },
            "file": {
                "url": record.url if status == "imported" else (build or {}).get("url") or record.url, "filename": record.filename,
                "sha256": record.content_hash, "etag": record.etag, "upstream_modified": record.upstream_modified,
                "fetched_at": record.fetched_at.strftime("%Y-%m-%d %H:%M"), "record_status": record.status,
                "previous_version": previous.version_label if previous else None,
            },
            "tiles": {
                "min_zoom": info["min_zoom"], "max_zoom": info["max_zoom"], "bounds": info["bounds"], "layers": info["layers"],
                "compression": info["tile_compression"], "addressed_tiles": info["addressed_tiles"],
                "schema_version": (build or {}).get("version") or metadata.get("version"),
                "name": metadata.get("name"), "attribution": metadata.get("attribution"),
            },
            "warnings": warnings,
            "record_ids": [record.id],
        }
        return StageResult("success", report=report)
