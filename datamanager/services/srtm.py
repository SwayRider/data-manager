"""SRTM elevation tiles (Skadi, 1° tiles, `N50/N50E004.hgt.gz`): which tiles a configuration needs, where they are
fetched from, and unpacking them into the layout Valhalla reads (`<dir>/N50/N50E004.hgt`)."""
import gzip
import os
import shutil
from pathlib import Path

from datamanager.config import config

S3_HTTPS = "https://{bucket}.s3.amazonaws.com/{key}"
SIZES = {2884802: "SRTM3", 25934402: "SRTM1"}  # bytes of an unpacked tile


def tile_name(lat: int, lon: int) -> str:
    return f"{'N' if lat >= 0 else 'S'}{abs(lat):02d}{'E' if lon >= 0 else 'W'}{abs(lon):03d}"


def tiles(boxes) -> list[str]:
    """Distinct tile names over [min_lat, max_lat, min_lon, max_lon] boxes (inclusive, as data-pipeline does)."""
    found = {tile_name(lat, lon) for min_lat, max_lat, min_lon, max_lon in boxes
             for lat in range(min_lat, max_lat + 1) for lon in range(min_lon, max_lon + 1)}
    return sorted(found)


def source_key(name: str) -> str:
    return f"srtm:{name}"


def tile_url(base: str, name: str) -> str:
    """`s3://bucket/prefix/` bases (the setting's default, as the AWS CLI takes it) are fetched over plain HTTPS."""
    if base.startswith("s3://"):
        bucket, _, prefix = base[5:].partition("/")
        base = S3_HTTPS.format(bucket=bucket, key=prefix)
    return f"{base.rstrip('/')}/{name[:3]}/{name}.hgt.gz"


def unpack(gz_file: Path, name: str, content_hash: str, target_dir: Path) -> int:
    """Gunzip a tile once per content hash (shared cache) and hard-link it into `target_dir/N50/`; returns its size."""
    cache = Path(config.DATA_ROOT) / "library" / "srtm-cache"
    cache.mkdir(parents=True, exist_ok=True)
    cached = cache / f"{content_hash}.hgt"
    if not cached.exists():
        part = cached.with_suffix(".part")
        with gzip.open(gz_file, "rb") as source, open(part, "wb") as out:
            shutil.copyfileobj(source, out, 1024 * 1024)
        part.rename(cached)
    final = target_dir / name[:3] / f"{name}.hgt"
    final.parent.mkdir(parents=True, exist_ok=True)
    if final.exists():
        final.unlink()
    try:
        os.link(cached, final)
    except OSError:  # another file system
        shutil.copyfile(cached, final)
    return cached.stat().st_size
