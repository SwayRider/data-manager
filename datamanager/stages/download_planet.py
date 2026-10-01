import datetime

from datamanager.db import SessionLocal
from datamanager.errors import DownloadError, ValidationError
from datamanager.services import downloads, settings as settings_service
from datamanager.stages.contract import StageResult, StageRunContext, StageRunner
from datamanager.stages.download_osm import _header_check

PLANET_KEY = "planet:osm"
STALE_DAYS = 14  # warn when the planet's data is older than this
SHRINK_WARN = 0.05  # warn when the file is more than 5 % smaller than the previous version


def _age_days(timestamp: str | None) -> int | None:
    if not timestamp:
        return None
    try:
        moment = datetime.datetime.fromisoformat(timestamp.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None
    return (datetime.datetime.now(datetime.UTC).replace(tzinfo=None) - moment).days


class DownloadPlanetStage(StageRunner):
    """Fetches the OSM planet (URL from Settings → download.planet) as a new version unless upstream is
    unchanged, keeps only the newest `download.planet_keep` versions, and ends in review. The country
    extract stage cuts the countries from the approved version. Needs no configuration.

    Params for testing without a fresh 95 GB download: `use_existing` (use the newest version we have,
    no upstream check) and `local_file` (register a planet PBF that is already on disk, in place)."""

    key = "download-planet"
    consumes_downloads = ("planet",)

    def run(self, context: StageRunContext) -> StageResult:
        session = SessionLocal()
        url = str(settings_service.get(session, "download.planet"))
        keep = int(settings_service.get(session, "download.planet_keep"))
        connections = int(settings_service.get(session, "download.connections"))

        local_file = str(context.params.get("local_file") or "").strip()
        md5_ok = None
        if local_file:
            # a planet already on disk: registered where it is (no copy, never deleted by us)
            context.step_cb("Registering the planet file")
            try:
                record = downloads.register_file(session, PLANET_KEY, local_file, int(context.run_id), context.progress_cb)
            except ValidationError as exc:
                return StageResult("failed", report={"error": exc.message})
            status = "imported"
        elif context.params.get("use_existing"):
            # testing shortcut: do not contact upstream, use the newest version we already have
            record = next((r for r in downloads.versions(session, PLANET_KEY) if r.status != "rejected"), None)
            if record is None or not downloads.abs_path(record).exists():
                return StageResult("failed", report={"error": "No planet version is available yet; untick \"use the one I have\" to download one."})
            status = "existing"
        else:
            context.step_cb("Fetching the planet")
            try:
                outcome = downloads.fetch(
                    session, PLANET_KEY, url, run_id=int(context.run_id), progress_cb=context.progress_cb,
                    connections=connections, md5_url=url + ".md5", timeout=60,
                )
            except DownloadError as exc:
                return StageResult("failed", report={"error": exc.message, "url": url})
            record, status, md5_ok = outcome.record, outcome.status, outcome.md5_ok

        warnings: list[str] = []
        header = {"ok": True, "data_timestamp": record.data_timestamp}
        if status in ("downloaded", "imported") or not record.data_timestamp:
            context.step_cb("Checking the PBF header")
            header = _header_check(session, downloads.abs_path(record))
            if header["ok"] is False:
                return StageResult("failed", report={"error": "Planet header unreadable: " + header["message"], "url": url})
            if header.get("data_timestamp"):
                record.data_timestamp = header["data_timestamp"]
                session.commit()

        previous = next((r for r in downloads.versions(session, PLANET_KEY) if r.id != record.id and r.status != "rejected"), None)
        age = _age_days(record.data_timestamp)
        if age is not None and age > STALE_DAYS:
            warnings.append(f"The planet's data is {age} days old.")
        if previous is not None and previous.size_bytes and record.size_bytes < previous.size_bytes * (1 - SHRINK_WARN):
            warnings.append("The file is more than 5 % smaller than the previous version.")
        if status == "downloaded" and md5_ok is None:
            warnings.append("No .md5 file was published next to the planet: the download is only size-checked.")

        context.step_cb("Removing old versions")
        pruned = downloads.prune(session, PLANET_KEY, keep)

        report = {
            "summary": {
                "status": status, "version": record.version_label, "size_bytes": record.size_bytes,
                "data_timestamp": record.data_timestamp, "age_days": age, "md5_ok": md5_ok,
                "pruned": len(pruned), "kept": keep,
            },
            "file": {
                "url": record.url if status == "imported" else url, "filename": record.filename, "sha256": record.content_hash, "etag": record.etag,
                "upstream_modified": record.upstream_modified, "fetched_at": record.fetched_at.strftime("%Y-%m-%d %H:%M"),
                "record_status": record.status, "previous_version": previous.version_label if previous else None,
            },
            "warnings": warnings,
            "record_ids": [record.id],
        }
        return StageResult("success", report=report)
