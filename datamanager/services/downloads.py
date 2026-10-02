"""Versioned downloads.

Every fetch that brings new data creates an immutable `DownloadRecord` (source key + UTC fetch time).
Versions coexist. Later stages take, per source, the version the run pins, else the pinned one, else
the newest *approved* one. Identical bytes are stored once (records share `local_path`). Cleanup keeps
the newest N versions plus anything pinned or currently selected, and is dry-run first."""
import datetime
import hashlib
import os
import re
import shutil
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path

import requests
from sqlalchemy.orm import Session

from datamanager.config import config
from datamanager.errors import DownloadError, ValidationError
from datamanager.models import BuildRun, DownloadRecord

CHUNK = 1024 * 1024
KEEP_DEFAULT = 2
ATTEMPTS = 4  # per request / per download; transient network errors are retried with a growing pause
RETRY_PAUSE_S = 3.0
TRANSIENT = (requests.Timeout, requests.ConnectionError, requests.exceptions.ChunkedEncodingError)

# Large files are fetched as short Range segments over fresh connections: some servers (Geofabrik) throttle a
# long-lived connection to ~100 kB/s while a new one runs at full speed.
SEGMENT_BYTES = 128 * 1024 * 1024
PARALLEL_MIN_BYTES = 100 * 1024 * 1024  # smaller files use one plain stream
SLOW_BPS = 1_000_000  # a connection below this for SLOW_WINDOW_S is dropped and re-requested
SLOW_WINDOW_S = 20.0
READ_CHUNK = 256 * 1024
SPACE_FACTOR = 1.2  # free disk needed relative to the download size


class _NoRanges(Exception):
    """The server ignored a Range request (answered 200): segmented download is not possible."""


class _Transient(Exception):
    """HTTP 429/5xx on a segment: retried like a connection error."""


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC).replace(tzinfo=None)


def version_label(moment: datetime.datetime) -> str:
    return moment.strftime("%Y%m%dT%H%M%SZ")


def _rel_dir(source_key: str) -> Path:
    parts = source_key.replace(":", "/").split("/")
    if not all(parts) or ".." in parts:
        raise ValidationError(f"Invalid source key: {source_key}")
    return Path("downloads", *parts)


def abs_path(record: DownloadRecord) -> Path:
    return Path(config.DATA_ROOT) / record.local_path  # an absolute local_path (registered file) wins


def is_managed(path: Path) -> bool:
    """True for files under DATA_ROOT/downloads, i.e. ones we fetched and may delete."""
    try:
        Path(path).resolve().relative_to((Path(config.DATA_ROOT) / "downloads").resolve())
    except ValueError:
        return False
    return True


# ---- selecting versions ---------------------------------------------------------------------------------------

def versions(session: Session, source_key: str) -> list[DownloadRecord]:
    """Newest first."""
    return (
        session.query(DownloadRecord)
        .filter(DownloadRecord.source_key == source_key)
        .order_by(DownloadRecord.fetched_at.desc(), DownloadRecord.id.desc())
        .all()
    )


def latest_approved(session: Session, source_key: str) -> DownloadRecord | None:
    return next((r for r in versions(session, source_key) if r.status == "approved"), None)


def resolve_version(session: Session, source_key: str, overrides: dict | None = None) -> DownloadRecord | None:
    """The version a stage should use: the run's override, else the pinned one, else the latest approved."""
    forced = (overrides or {}).get(source_key)
    if forced is not None:
        record = session.get(DownloadRecord, forced)
        if record is None or record.source_key != source_key or record.status == "rejected":
            raise ValidationError(f"Pinned version {forced} is not a usable version of {source_key}")
        return record
    found = versions(session, source_key)
    pinned = next((r for r in found if r.pinned and r.status != "rejected"), None)
    return pinned or next((r for r in found if r.status == "approved"), None)


def source_keys(session: Session) -> list[str]:
    return [k for (k,) in session.query(DownloadRecord.source_key).distinct().order_by(DownloadRecord.source_key)]


# ---- fetching -------------------------------------------------------------------------------------------------

@dataclass
class FetchOutcome:
    record: DownloadRecord
    status: str  # downloaded | unchanged
    reused_bytes: bool = False  # new version, but the bytes were identical to an older one
    md5_ok: bool | None = None  # checked against the `<url>.md5` sidecar; None = no sidecar


def _head(url: str, timeout: float, headers: dict | None = None) -> requests.Response:
    for attempt in range(1, ATTEMPTS + 1):
        try:
            response = requests.head(url, allow_redirects=True, timeout=timeout, headers=headers)
            break
        except requests.TooManyRedirects as exc:
            raise DownloadError(f"Could not reach {url}: {exc}", url=url, redirect_loop=True) from exc
        except TRANSIENT as exc:  # Geofabrik answers slowly under load; a retry usually goes through
            if attempt == ATTEMPTS:
                raise DownloadError(f"Could not reach {url} after {ATTEMPTS} attempts: {exc}", url=url) from exc
            time.sleep(RETRY_PAUSE_S * attempt)
        except requests.RequestException as exc:
            raise DownloadError(f"Could not reach {url}: {exc}", url=url) from exc
    if response.status_code in (405, 501):  # the server refuses HEAD (OpenAddresses): a GET whose body is never read gives the same headers
        try:
            response = requests.get(url, stream=True, allow_redirects=True, timeout=timeout, headers=headers)
            response.close()
        except requests.RequestException as exc:
            raise DownloadError(f"Could not reach {url}: {exc}", url=url) from exc
    if response.status_code >= 400:
        raise DownloadError(f"{url} answered HTTP {response.status_code}", url=url, status=response.status_code)
    return response


def _unchanged(record: DownloadRecord | None, etag: str | None, modified: str | None) -> bool:
    if record is None:
        return False
    if etag and record.etag:
        return etag == record.etag
    return bool(modified and record.upstream_modified and modified == record.upstream_modified)


def _stream_single(url: str, temp: Path, total: int, timeout: float, progress_cb, extra: dict | None = None) -> tuple[int, int]:
    """One connection into `temp`, resuming with a Range request after a drop. Returns (size, total).
    A server that ignores Range restarts from zero."""
    size, failures = 0, 0
    while True:
        headers = {**(extra or {}), **({"Range": f"bytes={size}-"} if size else {})}
        try:
            with requests.get(url, stream=True, allow_redirects=True, timeout=(timeout, 60), headers=headers) as response:
                if response.status_code >= 400:
                    raise DownloadError(f"{url} answered HTTP {response.status_code}", url=url, status=response.status_code)
                if size and response.status_code != 206:  # no resume support: start over
                    size = 0
                if response.status_code == 200:
                    total = int(response.headers.get("Content-Length") or total)
                with open(temp, "ab" if size else "wb") as out:
                    for chunk in response.iter_content(CHUNK):
                        out.write(chunk)
                        size += len(chunk)
                        progress_cb(size, total, f"{size / 1e6:.0f} of {total / 1e6:.0f} MB" if total else f"{size / 1e6:.0f} MB")
            if not total or size >= total:
                return size, total
            raise requests.exceptions.ChunkedEncodingError(f"stopped at {size} of {total} bytes")
        except TRANSIENT as exc:
            failures += 1
            if failures >= ATTEMPTS:
                raise
            progress_cb(size, total, f"connection lost at {size / 1e6:.0f} MB, retrying ({failures}/{ATTEMPTS - 1})")
            time.sleep(RETRY_PAUSE_S * failures)


def _fetch_segment(url: str, fd: int, start: int, end: int, timeout: float, stop: threading.Event, counter: list, lock, extra: dict | None = None) -> None:
    """Bytes start..end (inclusive) written at their offsets. Reconnects (fresh connection, from where it
    stopped) after an error or when the connection runs slower than SLOW_BPS for SLOW_WINDOW_S."""
    pos, failures = start, 0
    while pos <= end and not stop.is_set():
        got = 0
        try:
            with requests.get(url, stream=True, allow_redirects=True, timeout=(timeout, 60),
                              headers={**(extra or {}), "Range": f"bytes={pos}-{end}"}) as response:
                if response.status_code == 200:
                    raise _NoRanges()
                if response.status_code == 429 or response.status_code >= 500:
                    raise _Transient(f"HTTP {response.status_code}")
                if response.status_code >= 400:
                    raise DownloadError(f"{url} answered HTTP {response.status_code}", url=url, status=response.status_code)
                window_start, window_bytes = time.monotonic(), 0
                for chunk in response.iter_content(READ_CHUNK):
                    if stop.is_set():
                        return
                    n = min(len(chunk), end + 1 - pos)
                    os.pwrite(fd, chunk[:n], pos)
                    pos += n
                    got += n
                    window_bytes += n
                    with lock:
                        counter[0] += n
                    if pos > end:
                        break
                    elapsed = time.monotonic() - window_start
                    if elapsed >= SLOW_WINDOW_S:
                        if window_bytes / elapsed < SLOW_BPS:
                            break  # throttled connection: drop it and ask again
                        window_start, window_bytes = time.monotonic(), 0
        except (*TRANSIENT, _Transient):
            pass
        if pos > end:
            return
        if got:
            failures = 0
        else:
            failures += 1
            if failures >= ATTEMPTS:
                raise DownloadError(f"{url}: no data for bytes {pos}-{end} after {ATTEMPTS} attempts", url=url)
            time.sleep(RETRY_PAUSE_S * failures)


def _stream_segments(url: str, temp: Path, total: int, timeout: float, progress_cb, connections: int, extra: dict | None = None) -> tuple[int, int]:
    segments = [(start, min(start + SEGMENT_BYTES, total) - 1) for start in range(0, total, SEGMENT_BYTES)]
    stop, lock, counter = threading.Event(), threading.Lock(), [0]
    fd = os.open(temp, os.O_RDWR | os.O_CREAT | os.O_TRUNC)
    try:
        os.ftruncate(fd, total)
        started = time.monotonic()
        with ThreadPoolExecutor(max_workers=max(1, connections)) as pool:
            pending = {pool.submit(_fetch_segment, url, fd, s, e, timeout, stop, counter, lock, extra) for s, e in segments}
            try:
                while pending:
                    done, pending = wait(pending, timeout=1.0, return_when=FIRST_COMPLETED)
                    for future in done:
                        future.result()
                    size = counter[0]
                    rate = size / max(time.monotonic() - started, 0.001) / 1e6
                    progress_cb(size, total, f"{size / 1e6:.0f} of {total / 1e6:.0f} MB, {rate:.1f} MB/s")
            except BaseException:
                stop.set()
                for future in pending:
                    future.cancel()
                raise
    finally:
        os.close(fd)
    return total, total


def _stream(url: str, temp: Path, total: int, timeout: float, progress_cb, connections: int = 1, ranges: bool = False,
            extra: dict | None = None) -> tuple[int, int]:
    if ranges and total >= PARALLEL_MIN_BYTES:
        try:
            return _stream_segments(url, temp, total, timeout, progress_cb, connections, extra)
        except _NoRanges:
            temp.unlink(missing_ok=True)  # server cannot do ranges after all: plain stream
    return _stream_single(url, temp, total, timeout, progress_cb, extra)


def _hash_file(path: Path, progress_cb, with_md5: bool = False) -> tuple[str, str | None]:
    """sha256 (and md5 for the optional sidecar check) of a finished file, one read pass."""
    sha, md5 = hashlib.sha256(), hashlib.md5() if with_md5 else None  # noqa: S324 (integrity check against a published md5)
    size, done = path.stat().st_size, 0
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8 * CHUNK), b""):
            sha.update(chunk)
            if md5:
                md5.update(chunk)
            done += len(chunk)
            progress_cb(done, size, f"verifying {done / 1e6:.0f} of {size / 1e6:.0f} MB")
    return sha.hexdigest(), md5.hexdigest() if md5 else None


def _expected_md5(md5_url: str | None, timeout: float) -> str | None:
    """First hex token of the `<url>.md5` sidecar, or None when there is none."""
    if not md5_url:
        return None
    try:
        response = requests.get(md5_url, timeout=timeout)
    except requests.RequestException:
        return None
    match = re.match(r"\s*([0-9a-fA-F]{32})\b", response.text) if response.status_code == 200 else None
    return match.group(1).lower() if match else None


def fetch(
    session: Session,
    source_key: str,
    url: str,
    run_id: int | None = None,
    progress_cb=lambda current, total, message: None,
    timeout: float = 30,
    connections: int = 1,
    md5_url: str | None = None,
    headers: dict | None = None,
) -> FetchOutcome:
    """Download `url` as a new version of `source_key`, unless upstream says nothing changed since the
    newest existing version (ETag, else Last-Modified) — then that version is returned as `unchanged`.
    Big files are fetched as short Range segments (`connections` in parallel); a `<url>.md5` sidecar
    (`md5_url`) is verified when it exists. `headers` (e.g. an Authorization token) go on every request."""
    head = _head(url, timeout, headers)
    etag = head.headers.get("ETag")
    modified = head.headers.get("Last-Modified")
    existing = versions(session, source_key)
    newest = next((r for r in existing if r.status != "rejected"), None)
    if _unchanged(newest, etag, modified) and abs_path(newest).exists():
        return FetchOutcome(newest, "unchanged")

    total = int(head.headers.get("Content-Length") or 0)
    if total:
        free = shutil.disk_usage(config.DATA_ROOT).free
        if free < total * SPACE_FACTOR:
            raise DownloadError(
                f"Not enough disk space for {url}: needs {total * SPACE_FACTOR / 1e9:.1f} GB, {free / 1e9:.1f} GB free.", url=url
            )
    ranges = head.headers.get("Accept-Ranges", "").lower() == "bytes"
    moment = _now()
    taken = {r.version_label for r in existing}
    while version_label(moment) in taken:  # labels are second-precise and unique per source
        moment += datetime.timedelta(seconds=1)
    label = version_label(moment)
    filename = os.path.basename((head.url or url).split("?")[0]) or os.path.basename(url.split("?")[0]) or "download"
    target_dir = Path(config.DATA_ROOT) / _rel_dir(source_key) / label
    target_dir.mkdir(parents=True, exist_ok=True)
    temp = target_dir / (filename + ".part")
    expected_md5 = _expected_md5(md5_url, timeout)
    try:
        size, total = _stream(url, temp, total, timeout, progress_cb, connections=connections, ranges=ranges, extra=headers)
        if total and size != total:
            raise DownloadError(f"{url}: received {size} bytes, expected {total}", url=url)
        if size == 0:
            raise DownloadError(f"{url}: empty download", url=url)
        content_hash, md5 = _hash_file(temp, progress_cb, with_md5=expected_md5 is not None)
        if expected_md5 is not None and md5 != expected_md5:
            raise DownloadError(f"{url}: md5 {md5} does not match the published {expected_md5}", url=url)
    except requests.RequestException as exc:
        shutil.rmtree(target_dir, ignore_errors=True)
        raise DownloadError(f"Download of {url} failed: {exc}", url=url) from exc
    except BaseException:
        shutil.rmtree(target_dir, ignore_errors=True)
        raise

    twin = next((r for r in existing if r.content_hash == content_hash and abs_path(r).exists()), None)
    if twin is not None:
        shutil.rmtree(target_dir, ignore_errors=True)
        local_path = twin.local_path
    else:
        final = target_dir / filename
        temp.rename(final)
        local_path = str(final.relative_to(config.DATA_ROOT))
    record = DownloadRecord(
        source_key=source_key, version_label=label, url=url, filename=filename, local_path=local_path,
        size_bytes=size, content_hash=content_hash, etag=etag, upstream_modified=modified,
        fetched_at=moment, run_id=run_id,
    )
    session.add(record)
    session.commit()
    return FetchOutcome(record, "downloaded", reused_bytes=twin is not None, md5_ok=True if expected_md5 else None)


# ---- review, pinning, cleanup ---------------------------------------------------------------------------------

def apply_review(session: Session, run: BuildRun, approved: bool) -> None:
    """Approving a download run approves every version it fetched or confirmed (`report.record_ids`);
    rejecting only rejects the versions this very run fetched."""
    if approved:
        ids = (run.report_json or {}).get("record_ids", [])
        if ids:
            session.query(DownloadRecord).filter(
                DownloadRecord.id.in_(ids), DownloadRecord.status == "fetched"
            ).update({"status": "approved"})
            _prune_planet(session, ids)
    else:
        session.query(DownloadRecord).filter(
            DownloadRecord.run_id == run.id, DownloadRecord.status == "fetched"
        ).update({"status": "rejected"})


PRUNED_PREFIXES = {"planet:": "download.planet_keep", "tiles:": "download.tiles_keep", "srtm:": "download.srtm_keep"}  # huge sources: keep N after approval


def _prune_planet(session: Session, ids: list[int]) -> None:
    """After approving a huge download (planet, tile build) only the newest N versions of its source stay."""
    from datamanager.services import settings as settings_service

    for key in {k for (k,) in session.query(DownloadRecord.source_key).filter(DownloadRecord.id.in_(ids))}:
        for prefix, setting in PRUNED_PREFIXES.items():
            if key.startswith(prefix):
                prune(session, key, int(settings_service.get(session, setting)))


def pin(session: Session, record: DownloadRecord) -> None:
    if record.status == "rejected":
        raise ValidationError("A rejected version cannot be pinned.")
    session.query(DownloadRecord).filter(DownloadRecord.source_key == record.source_key).update({"pinned": False})
    record.pinned = True
    session.commit()


def unpin(session: Session, record: DownloadRecord) -> None:
    record.pinned = False
    session.commit()


def protected_ids(session: Session, source_key: str, keep: int) -> set[int]:
    """Versions cleanup must leave alone: the newest `keep`, pinned ones and the currently selected one."""
    found = versions(session, source_key)
    keep_ids = {r.id for r in found[:keep]} | {r.id for r in found if r.pinned}
    selected = resolve_version(session, source_key)
    if selected is not None:
        keep_ids.add(selected.id)
    keep_ids |= _referenced_by_assets({r.id for r in found}, session)
    return keep_ids


def _referenced_by_assets(ids: set[int], session: Session) -> set[int]:
    """Versions that a live (not rejected) asset was built from: deleting them would orphan its provenance."""
    from datamanager.models import Asset

    used = set()
    for (source_ids,) in session.query(Asset.source_download_ids).filter(Asset.status != "rejected"):
        used.update(source_ids or [])
    return used & ids


def cleanup_candidates(session: Session, keep: int = KEEP_DEFAULT) -> list[DownloadRecord]:
    """Versions a cleanup would delete (none of them pinned, selected or among the newest `keep`).
    Versions a live asset was built from are protected too."""
    if keep < 1:
        raise ValidationError("Keep at least one version per source.")
    doomed = []
    for key in source_keys(session):
        protected = protected_ids(session, key, keep)
        doomed += [r for r in versions(session, key) if r.id not in protected]
    return doomed


def _remove(session: Session, record: DownloadRecord) -> None:
    """Delete the record and, when no other version shares it, its file."""
    shared = session.query(DownloadRecord).filter(
        DownloadRecord.local_path == record.local_path, DownloadRecord.id != record.id
    ).count()
    path = abs_path(record)
    if not shared and path.exists() and is_managed(path):  # never delete a file that was only registered
        path.unlink()
        try:
            path.parent.rmdir()
        except OSError:
            pass
    session.delete(record)
    session.commit()


def register_file(session: Session, source_key: str, file: str | Path, run_id: int | None = None,
                  progress_cb=lambda current, total, message: None) -> DownloadRecord:
    """Register a file that is already on disk (e.g. a planet downloaded by hand) as a new version of
    `source_key`, without copying it: the record points at the file where it is, and removing the version
    never deletes it. Identical bytes of an existing version are reused instead."""
    path = Path(file).expanduser()
    if not path.is_file():
        raise ValidationError(f"{path} is not a file.")
    path = path.resolve()
    sha, _ = _hash_file(path, progress_cb)
    existing = versions(session, source_key)
    twin = next((r for r in existing if r.content_hash == sha and abs_path(r).exists()), None)
    moment = _now()
    taken = {r.version_label for r in existing}
    while version_label(moment) in taken:
        moment += datetime.timedelta(seconds=1)
    stat = path.stat()
    record = DownloadRecord(
        source_key=source_key, version_label=version_label(moment), url=path.as_uri(), filename=path.name,
        local_path=twin.local_path if twin else str(path), size_bytes=stat.st_size, content_hash=sha,
        etag=None, upstream_modified=datetime.datetime.fromtimestamp(stat.st_mtime, datetime.UTC).strftime("%a, %d %b %Y %H:%M:%S GMT"),
        fetched_at=moment, run_id=run_id,
    )
    session.add(record)
    session.commit()
    return record


def prune(session: Session, source_key: str, keep: int) -> list[int]:
    """Delete versions beyond the newest `keep` non-rejected ones (pinned versions stay). Used for sources
    whose files are huge (the planet), where the generic cleanup's "newest N plus in use" is too lax."""
    found = [r for r in versions(session, source_key) if r.status != "rejected"]
    keep_ids = {r.id for r in found[:keep]} | {r.id for r in found if r.pinned}
    removed = []
    for record in found:
        if record.id not in keep_ids:
            removed.append(record.id)
            _remove(session, record)
    return removed


def delete_version(session: Session, record: DownloadRecord) -> None:
    """Manual delete of one version; refused while it is pinned or the version stages would pick."""
    selected = resolve_version(session, record.source_key)
    if record.pinned or (selected is not None and selected.id == record.id):
        raise ValidationError("This version is pinned or currently in use; unpin it or approve a newer one first.")
    _remove(session, record)


def delete_run_versions(session: Session, run_id: int, kind: str | None = None) -> tuple[int, int]:
    """Deletes every version a run fetched (optionally only the sources of one kind such as `srtm`), except pinned
    or in-use ones. Returns (deleted, skipped)."""
    query = session.query(DownloadRecord).filter(DownloadRecord.run_id == run_id)
    if kind:
        query = query.filter(DownloadRecord.source_key.like(kind + ":%"))
    deleted = skipped = 0
    for record in query.all():
        try:
            delete_version(session, record)
            deleted += 1
        except ValidationError:
            skipped += 1
    return deleted, skipped


def cleanup(session: Session, keep: int = KEEP_DEFAULT, dry_run: bool = True) -> dict:
    """Delete (or, by default, only list) every version outside the protected set. `bytes` counts files
    that would actually disappear (a file shared with a surviving version stays)."""
    doomed = cleanup_candidates(session, keep)
    doomed_ids = {r.id for r in doomed}
    paths: dict[str, int] = {}
    for record in doomed:
        survivors = session.query(DownloadRecord).filter(
            DownloadRecord.local_path == record.local_path, DownloadRecord.id.notin_(doomed_ids)
        ).count()
        if not survivors:
            paths[record.local_path] = record.size_bytes
    if not dry_run:
        for record in doomed:
            _remove(session, record)
    return {"ids": sorted(doomed_ids), "bytes": sum(paths.values()), "dry_run": dry_run}
