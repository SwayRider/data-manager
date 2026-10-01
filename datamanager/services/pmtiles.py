"""Reading PMTiles v3 files and the Protomaps build list (no tile decoding, only header and metadata)."""
import datetime
import gzip
import json
import re
import struct
from pathlib import Path

import requests

from datamanager.errors import DownloadError

HEADER_SIZE = 127
BUILD_KEY = re.compile(r"^(\d{8})\.pmtiles$")
COMPRESSION = {0: "unknown", 1: "none", 2: "gzip", 3: "brotli", 4: "zstd"}
TILE_TYPE = {0: "unknown", 1: "mvt", 2: "png", 3: "jpeg", 4: "webp", 5: "avif"}


class PmtilesError(Exception):
    pass


def parse_header(raw: bytes) -> dict:
    """Fields of the fixed 127-byte PMTiles v3 header."""
    if len(raw) < HEADER_SIZE or raw[:7] != b"PMTiles":
        raise PmtilesError("not a PMTiles file (magic bytes missing)")
    if raw[7] != 3:
        raise PmtilesError(f"unsupported PMTiles version {raw[7]} (only v3)")
    offsets = struct.unpack_from("<11Q", raw, 8)
    clustered, internal, tile_comp, tile_type, min_zoom, max_zoom = struct.unpack_from("<6B", raw, 96)
    lon0, lat0, lon1, lat1 = (v / 1e7 for v in struct.unpack_from("<4i", raw, 102))
    return {
        "version": raw[7],
        "metadata_offset": offsets[2], "metadata_length": offsets[3],
        "tile_data_length": offsets[7],
        "addressed_tiles": offsets[8], "tile_entries": offsets[9], "tile_contents": offsets[10],
        "clustered": bool(clustered),
        "internal_compression": COMPRESSION.get(internal, "unknown"),
        "tile_compression": COMPRESSION.get(tile_comp, "unknown"),
        "tile_type": TILE_TYPE.get(tile_type, "unknown"),
        "min_zoom": min_zoom, "max_zoom": max_zoom,
        "bounds": [lon0, lat0, lon1, lat1],
    }


def read_info(path: Path) -> dict:
    """Header plus the JSON metadata (vector layers, build/schema version) of a local PMTiles file."""
    with open(path, "rb") as handle:
        header = parse_header(handle.read(HEADER_SIZE))
        handle.seek(header["metadata_offset"])
        raw = handle.read(header["metadata_length"])
    if header["internal_compression"] == "gzip":
        raw = gzip.decompress(raw)
    elif header["internal_compression"] not in ("none", "unknown"):
        raise PmtilesError(f"unsupported metadata compression {header['internal_compression']}")
    try:
        metadata = json.loads(raw) if raw else {}
    except ValueError as exc:
        raise PmtilesError(f"metadata is not valid JSON: {exc}") from exc
    return {**header, "metadata": metadata,
            "layers": sorted(layer.get("id", "") for layer in metadata.get("vector_layers", []))}


def newest_build(builds_url: str, base_url: str, timeout: float = 30) -> dict:
    """The newest daily Protomaps build: {key, url, size, uploaded, version}. The build list is JSON
    (`[{"key": "20260930.pmtiles", "size": ..., "uploaded": ..., "version": "..."}]`); when it cannot be read the
    last week of dated files is probed with HEAD requests instead."""
    base = base_url if base_url.endswith("/") else base_url + "/"
    try:
        response = requests.get(builds_url, timeout=timeout)
        response.raise_for_status()
        found = [b for b in response.json() if isinstance(b, dict) and BUILD_KEY.match(str(b.get("key", "")))]
    except (requests.RequestException, ValueError):
        found = []
    if found:
        best = max(found, key=lambda b: b["key"])
        return {"key": best["key"], "url": base + best["key"], "size": best.get("size"),
                "uploaded": best.get("uploaded"), "version": best.get("version")}
    today = datetime.datetime.now(datetime.UTC).date()
    for back in range(8):
        key = (today - datetime.timedelta(days=back)).strftime("%Y%m%d") + ".pmtiles"
        try:
            head = requests.head(base + key, timeout=timeout, allow_redirects=True)
        except requests.RequestException:
            continue
        if head.status_code == 200:
            return {"key": key, "url": base + key, "size": int(head.headers.get("Content-Length") or 0) or None,
                    "uploaded": head.headers.get("Last-Modified"), "version": None}
    raise DownloadError(f"No Protomaps build found (build list {builds_url}, files under {base}).", url=builds_url)


def build_date(key: str) -> str | None:
    """ISO date of a build key such as `20260930.pmtiles`."""
    match = BUILD_KEY.match(key)
    return f"{match.group(1)[:4]}-{match.group(1)[4:6]}-{match.group(1)[6:]}" if match else None
