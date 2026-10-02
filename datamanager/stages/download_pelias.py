import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from datamanager.db import SessionLocal
from datamanager.errors import DownloadError, ValidationError
from datamanager.services import downloads
from datamanager.services import pelias_sources as sources
from datamanager.services import settings as settings_service
from datamanager.stages.contract import StageResult, StageRunContext, StageRunner

OA_INTERVAL_S = 6.0  # the OpenAddresses batch API allows about 10 requests a minute
OA_REFERER = "https://pelias-results.openaddresses.io"
ESSENTIAL = ("placeholder", "geonames", "wof")  # Pelias cannot be built without these (WOF postal codes excepted); every other failure is a warning


def settings_base(session) -> dict[str, str]:
    return {"geonames": str(settings_service.get(session, "download.geonames")),
            "placeholder": str(settings_service.get(session, "download.placeholder"))}


def planned(session, resolved: dict) -> list[sources.Item]:
    return sources.plan(session, resolved, settings_base(session))


class DownloadPeliasStage(StageRunner):
    """Fetches everything the Pelias stage loads, each as its own versioned download (see `services/pelias_sources`):
    Placeholder store, GeoNames, WOF bundles of every buildable country, GeoNames postal files, official locality
    polygons and the OpenAddresses sources (with the token of Settings → Pelias; without one they are skipped).
    Unchanged upstream means no new version. A failure of an essential source (Placeholder, GeoNames, WOF) fails the
    run; any other source that fails or does not exist is reported as a warning."""

    key = "download-pelias-data"
    consumes_downloads = ("placeholder", "geonames", "geonames-postal", "wof", "official", "openaddresses")

    def run(self, context: StageRunContext) -> StageResult:
        if context.config_id is None:
            raise ValidationError("download-pelias-data needs a configuration")
        resolved = context.config_resolved or {}
        session = SessionLocal()
        items = planned(session, resolved)
        if not items:
            return StageResult("failed", report={"error": "The configuration has no core or overlap country with a Geofabrik path."})
        run_id = int(context.run_id)
        workers = max(1, min(4, int(settings_service.get(session, "download.connections"))))
        urls = {i.key: i.url for i in items if i.url}
        rows: dict[str, dict] = {i.key: {"key": i.key, "kind": i.kind, "label": i.label, "status": "pending", "detail": ""} for i in items}

        context.step_cb("Reading the WOF inventory")
        wof_keys = {i.key for i in items if i.kind == "wof"}
        try:
            urls.update(sources.wof_bundles(str(settings_service.get(session, "download.wof")), wof_keys))
        except (requests.RequestException, ValueError) as exc:
            return StageResult("failed", report={"error": f"Could not read the WOF inventory: {exc}"})

        def fetch(item: sources.Item):
            local = SessionLocal()
            try:
                url = urls.get(item.key)
                if url is None:
                    return item.key, "missing", "not in the WOF inventory", None
                outcome = downloads.fetch(local, item.key, url, run_id=run_id, timeout=60, connections=2)
                return item.key, outcome.status, "", outcome.record.id
            except DownloadError as exc:
                return item.key, "missing" if exc.details.get("status") in (403, 404) else "failed", exc.message, None
            finally:
                SessionLocal.remove()

        plain = [i for i in items if i.kind not in ("openaddresses",)]
        context.step_cb(f"Fetching {len(plain)} sources")
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(fetch, i) for i in plain]
            for count, future in enumerate(as_completed(futures), 1):
                context.progress_cb(count, len(plain), "sources")
            for future in futures:
                key, status, detail, record_id = future.result()
                rows[key].update(status=status, detail=detail, record_id=record_id)

        warnings = self._openaddresses(context, session, [i for i in items if i.kind == "openaddresses"], rows, run_id)

        for row in rows.values():
            if row.get("record_id"):
                record = downloads.versions(session, row["key"])
                match = next((r for r in record if r.id == row["record_id"]), None)
                if match is not None:
                    row.update(version=match.version_label, size=match.size_bytes)
        broken = [r for r in rows.values() if r["kind"] in ESSENTIAL and not r["key"].startswith("wof:postalcode-")
                  and r["status"] in ("failed", "missing")]
        if broken:
            return StageResult("failed", report={"error": f"{len(broken)} essential source(s) could not be fetched: "
                                                          + "; ".join(f"{r['label']}: {r['detail']}" for r in broken[:5]),
                                                 "sources": list(rows.values())})
        for row in rows.values():
            if row["status"] in ("failed", "missing"):
                warnings.append(f"{row['label']}: {row['status']}" + (f" ({row['detail']})" if row["detail"] else ""))
        counts: dict[str, int] = {}
        for row in rows.values():
            counts[row["status"]] = counts.get(row["status"], 0) + 1
        report = {
            "summary": {"sources": len(rows), **{s: counts.get(s, 0) for s in ("downloaded", "unchanged", "missing", "failed", "skipped")},
                        "bytes": sum(r.get("size") or 0 for r in rows.values()), "fingerprint": sources.fingerprint(items)},
            "sources": list(rows.values()),
            "warnings": warnings,
            "record_ids": [r["record_id"] for r in rows.values() if r.get("record_id")],
        }
        return StageResult("success", report=report)

    def _openaddresses(self, context, session, items, rows, run_id) -> list[str]:
        """One source at a time (rate limit): look the newest job up, fetch it unless that job was fetched before."""
        if not items:
            return []
        token = str(settings_service.get(session, "pelias.openaddresses_token")).strip()
        if not token:
            for item in items:
                rows[item.key].update(status="skipped", detail="no token")
            return [f"{len(items)} OpenAddresses source(s) skipped: no token in Settings → Pelias."]
        base = str(settings_service.get(session, "download.openaddresses")).rstrip("/")
        headers = {"Authorization": f"Bearer {token}", "Referer": OA_REFERER}
        context.step_cb(f"Fetching {len(items)} OpenAddresses sources")
        for count, item in enumerate(items, 1):
            row = rows[item.key]
            name = item.key.split(":", 1)[1]
            try:
                found = requests.get(f"{base}/api/data", params={"source": name, "layer": "addresses", "validated": "false"}, timeout=30)
                found.raise_for_status()
                data = found.json()
                job = data[0].get("job") if isinstance(data, list) and data else None
                if not job:
                    row.update(status="missing", detail="unknown to OpenAddresses")
                else:
                    url = f"{base}/api/job/{job}/output/source.geojson.gz"
                    newest = next((r for r in downloads.versions(session, item.key) if r.status != "rejected"), None)
                    if newest is not None and newest.url == url and downloads.abs_path(newest).exists():  # a job never changes
                        row.update(status="unchanged", record_id=newest.id, detail=f"job {job}")
                    else:
                        outcome = downloads.fetch(session, item.key, url, run_id=run_id, timeout=60, headers=headers)
                        row.update(status=outcome.status, record_id=outcome.record.id, detail=f"job {job}")
            except (requests.RequestException, ValueError, DownloadError) as exc:
                row.update(status="failed", detail=getattr(exc, "message", str(exc))[:200])
            context.progress_cb(count, len(items), "OpenAddresses")
            if count < len(items):
                time.sleep(OA_INTERVAL_S)
        return []
