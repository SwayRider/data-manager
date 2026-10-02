import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from datamanager.errors import DownloadError, ValidationError
from datamanager.models import DownloadRecord
from datamanager.services import downloads


class Upstream:
    """Tiny HTTP server: `files[path] = (bytes, etag)`; supports HEAD and GET."""

    def __init__(self):
        self.files: dict[str, tuple[bytes, str]] = {}
        self.loops: set[str] = set()  # paths that redirect to themselves forever
        self.listings: dict[str, str] = {}  # directory path -> html
        self.no_ranges: set[str] = set()  # paths whose server ignores Range requests
        self.slow: dict[str, float] = {}  # path -> seconds to sleep per 64 kB written
        self.requests: list = []  # (path, start, end) of every GET with its range
        self.no_head: set[str] = set()  # paths whose server answers HEAD with 405 (OpenAddresses)
        self.cut_first: dict[str, int] = {}  # path -> bytes after which the first GET drops the connection
        upstream = self

        class Handler(BaseHTTPRequestHandler):
            def _send(self, body_too):
                if not body_too and self.path in upstream.no_head:
                    self.send_response(405)
                    self.end_headers()
                    return
                if self.path in upstream.loops:
                    self.send_response(301)
                    self.send_header("Location", self.path)
                    self.end_headers()
                    return
                if self.path in upstream.listings:
                    body = upstream.listings[self.path].encode()
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    if body_too:
                        self.wfile.write(body)
                    return
                entry = upstream.files.get(self.path)
                if entry is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                data, etag = entry
                start, end, ranged = 0, len(data) - 1, False
                spec = self.headers.get("Range")
                if body_too and spec and self.path not in upstream.no_ranges:
                    first, _, last = spec.split("=")[1].partition("-")
                    start, ranged = int(first), True
                    end = int(last) if last else len(data) - 1
                upstream.requests.append((self.path, start, end if ranged else None))
                self.send_response(206 if ranged else 200)
                self.send_header("Content-Length", str(end - start + 1))
                self.send_header("ETag", etag)
                self.send_header("Accept-Ranges", "bytes")
                if ranged:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
                self.end_headers()
                if body_too:
                    cut = upstream.cut_first.pop(self.path, None)
                    if cut is not None:
                        self.wfile.write(data[start:min(cut, end + 1)])
                        self.wfile.flush()
                        self.close_connection = True
                        return
                    delay = upstream.slow.pop(self.path, None)  # only the first connection is slow
                    if delay:
                        import time
                        for i in range(start, end + 1, 65536):
                            self.wfile.write(data[i:min(i + 65536, end + 1)])
                            self.wfile.flush()
                            time.sleep(delay)
                        return
                    self.wfile.write(data[start:end + 1])

            def do_HEAD(self):
                self._send(False)

            def do_GET(self):
                self._send(True)

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def base(self):
        return f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture()
def upstream():
    server = Upstream()
    yield server
    server.close()


def _fetch(session, upstream, key="osm:europe/aa", path="/europe/aa-latest.osm.pbf", **kw):
    return downloads.fetch(session, key, upstream.base + path, **kw)


def test_fetch_creates_version_and_file(db_session, upstream):
    upstream.files["/europe/aa-latest.osm.pbf"] = (b"pbf-one", '"e1"')
    outcome = _fetch(db_session, upstream)
    record = outcome.record
    assert outcome.status == "downloaded" and record.status == "fetched"
    assert record.size_bytes == 7 and len(record.content_hash) == 64 and record.etag == '"e1"'
    assert downloads.abs_path(record).read_bytes() == b"pbf-one"
    assert record.local_path.startswith(f"downloads/osm/europe/aa/{record.version_label}/")


def test_unchanged_upstream_makes_no_new_version(db_session, upstream):
    upstream.files["/europe/aa-latest.osm.pbf"] = (b"pbf-one", '"e1"')
    first = _fetch(db_session, upstream).record
    again = _fetch(db_session, upstream)
    assert again.status == "unchanged" and again.record.id == first.id
    assert len(downloads.versions(db_session, "osm:europe/aa")) == 1


def test_new_upstream_version_keeps_old_one(db_session, upstream, monkeypatch):
    upstream.files["/europe/aa-latest.osm.pbf"] = (b"pbf-one", '"e1"')
    first = _fetch(db_session, upstream).record
    upstream.files["/europe/aa-latest.osm.pbf"] = (b"pbf-two!", '"e2"')
    monkeypatch.setattr(downloads, "version_label", lambda moment: "20991231T000000Z")  # distinct from the first
    second = _fetch(db_session, upstream).record
    assert second.id != first.id
    assert downloads.abs_path(first).read_bytes() == b"pbf-one" and downloads.abs_path(second).read_bytes() == b"pbf-two!"
    assert [r.id for r in downloads.versions(db_session, "osm:europe/aa")] == [second.id, first.id]


def test_same_bytes_new_etag_shares_the_file(db_session, upstream, monkeypatch):
    upstream.files["/europe/aa-latest.osm.pbf"] = (b"same", '"e1"')
    first = _fetch(db_session, upstream).record
    upstream.files["/europe/aa-latest.osm.pbf"] = (b"same", '"e2"')
    monkeypatch.setattr(downloads, "version_label", lambda moment: "20991231T000000Z")
    outcome = _fetch(db_session, upstream)
    assert outcome.reused_bytes and outcome.record.local_path == first.local_path and outcome.record.id != first.id


def test_http_error_and_progress(db_session, upstream):
    with pytest.raises(DownloadError, match="404"):
        _fetch(db_session, upstream, path="/missing.pbf")
    upstream.files["/europe/aa-latest.osm.pbf"] = (b"x" * 10, '"e1"')
    seen = []
    _fetch(db_session, upstream, progress_cb=lambda c, t, m: seen.append((c, t)))
    assert seen[-1] == (10, 10)


def _make(session, key, label, status="approved", pinned=False, content="a", path=None):
    import datetime
    record = DownloadRecord(
        source_key=key, version_label=label, url="http://x", filename="f", local_path=path or f"downloads/{key}/{label}/f",
        size_bytes=10, content_hash=content, fetched_at=datetime.datetime.strptime(label, "%Y%m%dT%H%M%SZ"),
        status=status, pinned=pinned,
    )
    session.add(record)
    session.commit()
    return record


def test_resolve_prefers_override_then_pin_then_latest_approved(db_session):
    a = _make(db_session, "osm:x", "20260101T000000Z")
    b = _make(db_session, "osm:x", "20260102T000000Z")
    c = _make(db_session, "osm:x", "20260103T000000Z", status="fetched")  # not reviewed yet
    assert downloads.resolve_version(db_session, "osm:x").id == b.id
    downloads.pin(db_session, a)
    assert downloads.resolve_version(db_session, "osm:x").id == a.id
    assert downloads.resolve_version(db_session, "osm:x", {"osm:x": c.id}).id == c.id
    assert downloads.resolve_version(db_session, "osm:none") is None
    with pytest.raises(ValidationError):
        downloads.resolve_version(db_session, "osm:other", {"osm:other": a.id})


def test_pin_is_exclusive_and_rejected_cannot_be_pinned(db_session):
    a = _make(db_session, "osm:x", "20260101T000000Z")
    b = _make(db_session, "osm:x", "20260102T000000Z")
    r = _make(db_session, "osm:x", "20260103T000000Z", status="rejected")
    downloads.pin(db_session, a)
    downloads.pin(db_session, b)
    assert not a.pinned and b.pinned
    with pytest.raises(ValidationError):
        downloads.pin(db_session, r)


def test_cleanup_keeps_newest_pinned_and_selected(db_session, tmp_data_root):
    records = [_make(db_session, "osm:x", f"2026010{i}T000000Z", content=str(i)) for i in range(1, 6)]
    for r in records:
        path = downloads.abs_path(r)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"0123456789")
    downloads.pin(db_session, records[0])  # oldest is pinned -> survives
    plan = downloads.cleanup(db_session, keep=2)  # dry run
    assert plan["dry_run"] and plan["ids"] == [records[1].id, records[2].id] and plan["bytes"] == 20
    assert all(downloads.abs_path(r).exists() for r in records)

    result = downloads.cleanup(db_session, keep=2, dry_run=False)
    assert result["ids"] == plan["ids"]
    remaining = {r.id for r in downloads.versions(db_session, "osm:x")}
    assert remaining == {records[0].id, records[3].id, records[4].id}
    assert not downloads.abs_path(records[1]).exists() and downloads.abs_path(records[3]).exists()


def test_cleanup_keeps_shared_file_and_validates(db_session, tmp_data_root):
    old = _make(db_session, "osm:x", "20260101T000000Z", path="downloads/osm/x/shared/f")
    new = _make(db_session, "osm:x", "20260102T000000Z", path="downloads/osm/x/shared/f")
    path = downloads.abs_path(new)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"data")
    plan = downloads.cleanup(db_session, keep=1, dry_run=False)
    assert plan["ids"] == [old.id] and plan["bytes"] == 0 and path.exists()
    with pytest.raises(ValidationError):
        downloads.cleanup(db_session, keep=0)


def test_delete_version_refuses_selected(db_session, tmp_data_root):
    old = _make(db_session, "osm:x", "20260101T000000Z")
    new = _make(db_session, "osm:x", "20260102T000000Z")
    with pytest.raises(ValidationError, match="in use"):
        downloads.delete_version(db_session, new)
    downloads.delete_version(db_session, old)
    assert [r.id for r in downloads.versions(db_session, "osm:x")] == [new.id]


def test_dropped_connection_is_resumed_with_a_range_request(db_session, upstream, monkeypatch):
    monkeypatch.setattr(downloads, "RETRY_PAUSE_S", 0)
    payload = bytes(range(256)) * 400
    upstream.files["/europe/aa-latest.osm.pbf"] = (payload, '"e1"')
    upstream.cut_first["/europe/aa-latest.osm.pbf"] = 30000
    outcome = _fetch(db_session, upstream)
    assert outcome.status == "downloaded" and outcome.record.size_bytes == len(payload)
    import hashlib
    assert outcome.record.content_hash == hashlib.sha256(payload).hexdigest()
    assert downloads.abs_path(outcome.record).read_bytes() == payload


def test_head_timeouts_are_retried(db_session, upstream, monkeypatch):
    import requests

    monkeypatch.setattr(downloads, "RETRY_PAUSE_S", 0)
    upstream.files["/europe/aa-latest.osm.pbf"] = (b"x" * 10, '"e1"')
    real, calls = requests.head, []

    def flaky(*args, **kwargs):
        calls.append(1)
        if len(calls) < 3:
            raise requests.ReadTimeout("slow")
        return real(*args, **kwargs)

    monkeypatch.setattr(requests, "head", flaky)
    assert _fetch(db_session, upstream).status == "downloaded" and len(calls) == 3


# ---- segmented downloads, watchdog, md5, prune, space ---------------------------------------------------------

@pytest.fixture()
def segmented(monkeypatch):
    monkeypatch.setattr(downloads, "SEGMENT_BYTES", 100_000)
    monkeypatch.setattr(downloads, "PARALLEL_MIN_BYTES", 1000)
    monkeypatch.setattr(downloads, "RETRY_PAUSE_S", 0)


def _ranged(upstream, path):
    return sorted((s, e) for p, s, e in upstream.requests if p == path and e is not None)


def test_large_file_is_fetched_in_parallel_range_segments(db_session, upstream, segmented):
    import hashlib
    payload = bytes(range(256)) * 1600  # 409,600 bytes -> 5 segments
    upstream.files["/big.pbf"] = (payload, '"b1"')
    outcome = _fetch(db_session, upstream, key="planet:osm", path="/big.pbf", connections=3)
    assert outcome.status == "downloaded"
    assert downloads.abs_path(outcome.record).read_bytes() == payload
    assert outcome.record.content_hash == hashlib.sha256(payload).hexdigest()
    assert _ranged(upstream, "/big.pbf") == [(0, 99_999), (100_000, 199_999), (200_000, 299_999), (300_000, 399_999), (400_000, 409_599)]


def test_segment_resumes_after_a_dropped_connection(db_session, upstream, segmented):
    payload = bytes(range(256)) * 1600
    upstream.files["/big.pbf"] = (payload, '"b1"')
    upstream.cut_first["/big.pbf"] = 50_000  # first GET (segment 0) dies after 50 kB
    outcome = _fetch(db_session, upstream, key="planet:osm", path="/big.pbf", connections=1)
    assert downloads.abs_path(outcome.record).read_bytes() == payload
    # the dropped segment is requested again (from where the bytes it already wrote end; urllib3 hands over
    # whole chunks, so a drop inside the first 256 kB chunk restarts that segment)
    assert len([r for r in _ranged(upstream, "/big.pbf") if r[1] == 99_999]) == 2


def test_server_without_range_support_falls_back_to_a_single_stream(db_session, upstream, segmented):
    payload = b"z" * 300_000
    upstream.files["/big.pbf"] = (payload, '"b1"')
    upstream.no_ranges.add("/big.pbf")
    outcome = _fetch(db_session, upstream, key="planet:osm", path="/big.pbf", connections=2)
    assert downloads.abs_path(outcome.record).read_bytes() == payload


def test_slow_connection_is_dropped_and_reconnected(db_session, upstream, segmented, monkeypatch):
    monkeypatch.setattr(downloads, "SEGMENT_BYTES", 600_000)
    monkeypatch.setattr(downloads, "SLOW_BPS", 5_000_000)
    monkeypatch.setattr(downloads, "SLOW_WINDOW_S", 0.2)
    payload = bytes(range(256)) * 2400  # 614,400 bytes, 2 segments
    upstream.files["/big.pbf"] = (payload, '"b1"')
    upstream.slow["/big.pbf"] = 0.08  # first connection ~0.8 MB/s: below the threshold
    outcome = _fetch(db_session, upstream, key="planet:osm", path="/big.pbf", connections=1)
    assert downloads.abs_path(outcome.record).read_bytes() == payload
    starts = [s for s, _ in _ranged(upstream, "/big.pbf")]
    assert len(starts) >= 3 and 0 in starts  # the first segment needed a second connection


def test_md5_sidecar_is_verified(db_session, upstream, segmented):
    import hashlib
    payload = b"m" * 250_000
    upstream.files["/big.pbf"] = (payload, '"b1"')
    upstream.listings["/big.pbf.md5"] = f"{hashlib.md5(payload).hexdigest()}  big.pbf\n"
    ok = downloads.fetch(db_session, "planet:osm", upstream.base + "/big.pbf", md5_url=upstream.base + "/big.pbf.md5")
    assert ok.md5_ok is True
    upstream.files["/big.pbf"] = (b"n" * 250_000, '"b2"')  # content changed, md5 file did not
    with pytest.raises(DownloadError, match="md5"):
        downloads.fetch(db_session, "planet:osm", upstream.base + "/big.pbf", md5_url=upstream.base + "/big.pbf.md5")
    assert [r.id for r in downloads.versions(db_session, "planet:osm")] == [ok.record.id]  # failed one left nothing


def test_not_enough_disk_space_fails_before_downloading(db_session, upstream, monkeypatch):
    import collections
    usage = collections.namedtuple("usage", "total used free")
    monkeypatch.setattr(downloads.shutil, "disk_usage", lambda path: usage(100, 90, 10))
    upstream.files["/europe/aa-latest.osm.pbf"] = (b"x" * 100, '"e1"')
    with pytest.raises(DownloadError, match="disk space"):
        _fetch(db_session, upstream)


def test_prune_keeps_the_newest_versions_and_pinned(db_session):
    a = _make(db_session, "planet:osm", "20260101T000000Z", content="a")
    b = _make(db_session, "planet:osm", "20260102T000000Z", content="b")
    c = _make(db_session, "planet:osm", "20260103T000000Z", content="c")
    d = _make(db_session, "planet:osm", "20260104T000000Z", content="d", status="fetched")
    downloads.pin(db_session, a)
    removed = downloads.prune(db_session, "planet:osm", keep=2)
    assert removed == [b.id]  # d and c are the newest two; a is pinned
    assert {r.id for r in downloads.versions(db_session, "planet:osm")} == {a.id, c.id, d.id}


def test_fetch_works_when_the_server_refuses_head(tmp_data_root, db_session):
    server = Upstream()
    try:
        server.files["/oa/source.geojson.gz"] = (b"addresses", '"v1"')
        server.no_head.add("/oa/source.geojson.gz")
        outcome = downloads.fetch(db_session, "openaddresses:xx/countrywide", server.base + "/oa/source.geojson.gz")
        assert outcome.status == "downloaded" and downloads.abs_path(outcome.record).read_bytes() == b"addresses"
        again = downloads.fetch(db_session, "openaddresses:xx/countrywide", server.base + "/oa/source.geojson.gz")
        assert again.status == "unchanged"  # the ETag of the GET answer is compared as usual
    finally:
        server.close()
