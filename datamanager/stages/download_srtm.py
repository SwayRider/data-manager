import hashlib
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from datamanager.config import config
from datamanager.db import SessionLocal
from datamanager.errors import DownloadError, ValidationError
from datamanager.models import DownloadRecord
from datamanager.services import downloads, srtm
from datamanager.services import settings as settings_service
from datamanager.stages.contract import StageResult, StageRunContext, StageRunner


def planned_tiles(resolved: dict) -> list[str]:
    """Tile names the regions of a resolved configuration need (their SRTM boxes already include the overlap)."""
    boxes = [box for region in resolved.get("regions", []) for box in region.get("srtm", {}).values()]
    return srtm.tiles(boxes)


def srtm_fingerprint(resolved: dict) -> str:
    return hashlib.sha256(json.dumps(planned_tiles(resolved)).encode()).hexdigest()[:16]


class DownloadSrtmStage(StageRunner):
    """Fetches the SRTM elevation tiles (Skadi, 1°, `download.srtm`) every region of the configuration needs, each as
    its own versioned `srtm:N50E004` download (unchanged upstream means no new version), and unpacks them into
    `library/srtm/<run_id>/N50/N50E004.hgt`, the layout Valhalla reads. Tiles over open sea do not exist and are
    only counted."""

    key = "download-srtm"
    consumes_downloads = ("srtm",)

    def run(self, context: StageRunContext) -> StageResult:
        if context.config_id is None:
            raise ValidationError("download-srtm needs a configuration")
        names = planned_tiles(context.config_resolved or {})
        if not names:
            return StageResult("failed", report={"error": "No region has an SRTM box (Configure → Resolved)."})
        session = SessionLocal()
        base = str(settings_service.get(session, "download.srtm"))
        workers = int(settings_service.get(session, "download.connections"))
        run_id = int(context.run_id)

        def fetch(name: str):
            local = SessionLocal()
            try:
                outcome = downloads.fetch(local, srtm.source_key(name), srtm.tile_url(base, name), run_id=run_id, timeout=60)
                result = (name, outcome.status, outcome.record.id)
            except DownloadError as exc:
                result = (name, "missing" if exc.details.get("status") in (403, 404) else "failed", exc.message)
            finally:
                SessionLocal.remove()
            return result

        context.step_cb(f"Fetching {len(names)} tiles")
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futures = [pool.submit(fetch, name) for name in names]
            for count, future in enumerate(as_completed(futures), 1):  # progress is reported from this thread (own session)
                context.progress_cb(count, len(names), "tiles")
            results = [f.result() for f in futures]
        failed = [(n, why) for n, status, why in results if status == "failed"]
        if failed:
            return StageResult("failed", report={"error": f"{len(failed)} tile(s) failed: " + "; ".join(f"{n}: {w}" for n, w in failed[:5])})

        context.step_cb("Unpacking")
        out_dir = Path(config.DATA_ROOT) / "library" / "srtm" / str(run_id)
        counts = {"downloaded": 0, "unchanged": 0, "missing": 0}
        record_ids, odd_sizes, total = [], [], 0
        for name, status, detail in results:
            counts[status] += 1
            if status == "missing":
                continue
            record = session.get(DownloadRecord, detail)
            record_ids.append(record.id)
            size = srtm.unpack(downloads.abs_path(record), name, record.content_hash, out_dir)
            total += size
            if size not in srtm.SIZES:
                odd_sizes.append(f"{name} ({size} bytes)")

        warnings = []
        if counts["missing"]:
            warnings.append(f"{counts['missing']} tile(s) do not exist upstream (open sea) and are left out.")
        if odd_sizes:
            warnings.append("Unexpected tile size: " + ", ".join(odd_sizes[:5]) + (" …" if len(odd_sizes) > 5 else ""))
        report = {
            "summary": {"tiles": len(names), **counts, "unpacked_bytes": total, "directory": str(out_dir.relative_to(config.DATA_ROOT)),
                        "fingerprint": srtm_fingerprint(context.config_resolved or {})},
            "missing_tiles": [n for n, s, _ in results if s == "missing"],
            "warnings": warnings,
            "record_ids": record_ids,
        }
        return StageResult("success", report=report)
