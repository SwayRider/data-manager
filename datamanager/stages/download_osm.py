import subprocess
import time

from datamanager.db import SessionLocal
from datamanager.errors import DownloadError, ValidationError
from datamanager.services import downloads, geofabrik, settings as settings_service, tools
from datamanager.stages.contract import StageResult, StageRunContext, StageRunner


def osm_source_key(geofabrik_path: str) -> str:
    return f"osm:{geofabrik_path}"


def planned_paths(resolved: dict) -> list[str]:
    """Every Geofabrik extract the resolved config needs (core and overlap), each once, sorted."""
    paths = {
        c["geofabrik_path"]
        for region in resolved.get("regions", [])
        for c in region["core"] + region["overlap"]
        if c.get("geofabrik_path")
    }
    return sorted(paths)


def _header_check(session, path) -> dict:
    """Readability of the PBF header via osmium (header only, so it is fast on large files)."""
    status = tools.detect(session, "osmium", force=True)
    if not status.ok:
        return {"ok": None, "message": f"skipped: osmium not available ({status.message or status.status})"}
    try:
        out = subprocess.run(
            [status.path, "fileinfo", "-g", "header.option.osmosis_replication_timestamp", str(path)],
            capture_output=True, text=True, timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "message": f"osmium could not run: {exc}"}
    if out.returncode != 0:
        return {"ok": False, "message": (out.stderr or "osmium failed").strip()[:300]}
    return {"ok": True, "data_timestamp": out.stdout.strip() or None}


class DownloadOsmStage(StageRunner):
    """Fetches the Geofabrik extract of every core/overlap country of a configuration as a new
    version (skipping those upstream reports unchanged). Ends in review: nothing is used by later
    stages until the run is approved."""

    key = "download-osm"
    consumes_downloads = ("osm",)

    def run(self, context: StageRunContext) -> StageResult:
        if context.config_resolved is None:
            raise ValidationError("download-osm needs a configuration")
        session = SessionLocal()
        base = str(settings_service.get(session, "download.osm")).rstrip("/")
        paths = planned_paths(context.config_resolved)
        if not paths:
            raise ValidationError("The configuration has no country with a Geofabrik path.")

        files, record_ids, failures = [], [], []
        for index, path in enumerate(paths, 1):
            key = osm_source_key(path)
            context.step_cb(f"{path} ({index}/{len(paths)})")
            started = time.monotonic()
            entry = {"source_key": key, "path": path}
            try:
                try:
                    outcome = downloads.fetch(
                        session, key, f"{base}/{path}-latest.osm.pbf", run_id=int(context.run_id),
                        progress_cb=context.progress_cb,
                    )
                except DownloadError as exc:
                    if not exc.details.get("redirect_loop"):
                        raise
                    # Geofabrik's "-latest" alias sometimes loops; the dated file it points to is stable
                    entry["note"] = "'-latest' URL redirect-looped; used the newest dated file from the directory listing"
                    outcome = downloads.fetch(
                        session, key, geofabrik.newest_dated_pbf_url(base, path), run_id=int(context.run_id),
                        progress_cb=context.progress_cb,
                    )
            except DownloadError as exc:
                entry.update(status="failed", message=exc.message)
                failures.append(path)
                files.append(entry)
                continue
            record = outcome.record
            entry.update(
                status=outcome.status, record_id=record.id, version=record.version_label,
                size_bytes=record.size_bytes, sha256=record.content_hash,
                fetched_at=record.fetched_at.strftime("%Y-%m-%d %H:%M"), fetched_by_run=record.run_id,
                record_status=record.status,
                same_bytes_as_older_version=outcome.reused_bytes,
                seconds=round(time.monotonic() - started, 1),
            )
            if outcome.status == "downloaded":
                entry["header"] = _header_check(session, downloads.abs_path(record))
                if entry["header"]["ok"] is False:
                    failures.append(path)
                    entry["status"] = "failed"
                    entry["message"] = "PBF header unreadable: " + entry["header"]["message"]
            if entry["status"] != "failed":
                record_ids.append(record.id)
            files.append(entry)

        report = {
            "summary": {
                "sources": len(paths),
                "downloaded": sum(1 for f in files if f["status"] == "downloaded"),
                "unchanged": sum(1 for f in files if f["status"] == "unchanged"),
                "failed": len(failures),
                "bytes": sum(f.get("size_bytes", 0) for f in files if f["status"] == "downloaded"),
            },
            "files": files,
            "record_ids": record_ids,
        }
        if failures:
            report["error"] = "Failed: " + ", ".join(failures)
        return StageResult(status="failed" if failures else "success", report=report)
