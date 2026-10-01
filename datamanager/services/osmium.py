"""Thin wrapper around the `osmium` CLI used by the OSM extract stage.

Every command writes to `<out>.tmp` and renames on success, so an interrupted or failed run never
leaves a half-written PBF under its final name. Binaries run with fixed argument lists, no shell."""
import json
import os
import re
import select
import shutil
import subprocess
from pathlib import Path

from datamanager.errors import ValidationError


class OsmiumError(Exception):
    pass


class OsmiumMemoryError(OsmiumError):
    """osmium was stopped because the machine ran low on memory."""


def available_gb() -> float | None:
    """MemAvailable from /proc/meminfo in GB (None where that is not readable)."""
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / 1e6
    except (OSError, ValueError):
        pass
    return None


def _peak_rss(pid: int) -> int:
    """Peak resident memory (bytes) of a running process (VmHWM), 0 when unreadable."""
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmHWM:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError):
        pass
    return 0


def binary(session) -> str:
    """Path of a working osmium; every stage re-detects the tools it needs before starting."""
    from datamanager.services import tools

    status = tools.detect(session, "osmium", force=True)
    if not status.ok:
        raise ValidationError(f"osmium is not available ({status.message or status.status}); see Settings → Tools.")
    return status.path


def _run(cmd: list[str]) -> str:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except OSError as exc:
        raise OsmiumError(f"could not run osmium: {exc}") from exc
    if proc.returncode != 0:
        raise OsmiumError(f"osmium {cmd[1]} failed (exit {proc.returncode}): {(proc.stderr or proc.stdout).strip()[-500:]}")
    return proc.stdout


def _tmp_name(out: Path, suffix: str = "") -> Path:
    return out.with_name(out.name + ".tmp" + suffix)


def _atomic(out: Path, build) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_name(out, ".pbf")  # osmium picks the format from the extension
    try:
        build(tmp)
        tmp.replace(out)
    finally:
        tmp.unlink(missing_ok=True)
    return out


def extract(exe: str, source: Path, poly: Path, out: Path) -> Path:
    """Objects of `source` inside the polygon (osmium's default strategy, complete ways)."""
    return _atomic(out, lambda tmp: _run([exe, "extract", "-p", str(poly), str(source), "-o", str(tmp), "--overwrite"]))


def merge_latest(exe: str, sources: list[Path], out: Path) -> Path:
    """Merge PBFs keeping only the newest version of each object.

    Geofabrik extracts of neighbouring countries can hold the same border object at different versions;
    `osmium merge` keeps both, so the result is sorted into history form and time-filtered to the far
    future, which retains the latest version only (the legacy pipeline's approach)."""
    if not sources:
        raise OsmiumError("nothing to merge")
    if len(sources) == 1:
        return _atomic(out, lambda tmp: shutil.copyfile(sources[0], tmp))

    def build(tmp: Path) -> None:
        merged = out.with_name(out.name + ".merged.tmp.pbf")
        history = out.with_name(out.name + ".sorted.tmp.osh.pbf")
        try:
            _run([exe, "merge", *map(str, sources), "-o", str(merged), "--overwrite"])
            _run([exe, "sort", str(merged), "-o", str(history), "--strategy=multipass", "--overwrite"])
            merged.unlink()
            _run([exe, "time-filter", str(history), "2099-01-01T00:00:00Z", "-o", str(tmp), "--overwrite"])
        finally:
            merged.unlink(missing_ok=True)
            history.unlink(missing_ok=True)

    return _atomic(out, build)


def fileinfo(exe: str, path: Path) -> dict:
    """Extended file info: object counts, data bbox, ordering, duplicate versions, timestamps."""
    info = json.loads(_run([exe, "fileinfo", "-e", "-j", str(path)]))
    data, header = info.get("data", {}), info.get("header", {})
    return {
        "counts": data.get("count", {}),
        "bbox": data.get("bbox"),
        "ordered": data.get("objects_ordered"),
        "multiple_versions": data.get("multiple_versions"),
        "last_timestamp": (data.get("timestamp") or {}).get("last"),
        "replication_timestamp": (header.get("option") or {}).get("osmosis_replication_timestamp"),
    }


def poly_bbox(path: Path) -> list[float] | None:
    """[min_lon, min_lat, max_lon, max_lat] of the coordinates in an osmosis `.poly` file."""
    xs, ys = [], []
    for line in Path(path).read_text().splitlines():
        match = re.fullmatch(r"\s+(-?[\d.eE+-]+)\s+(-?[\d.eE+-]+)\s*", line)
        if match:
            xs.append(float(match.group(1)))
            ys.append(float(match.group(2)))
    return [min(xs), min(ys), max(xs), max(ys)] if xs else None


def extract_many(exe: str, source: Path, jobs: list[tuple[Path, Path]], work: Path, progress_cb=lambda pct, message: None,
                 min_free_gb: float = 0) -> int:
    """Cut several regions out of one big PBF in a single run (`osmium extract -c`; the source is read once,
    not once per region). `jobs` is [(polygon .poly file, output .osm.pbf)]; outputs appear only when the
    whole run succeeded (they are written under `.tmp` names in `work` and then moved).

    osmium keeps ID sets per region (about 3 GB each on the planet), so callers batch the regions. While it
    runs, available memory is watched: below `min_free_gb` osmium is killed (OsmiumMemoryError) instead of
    starving the machine. Returns osmium's peak memory in bytes."""
    work.mkdir(parents=True, exist_ok=True)
    extracts, finals = [], []
    for index, (poly, out) in enumerate(jobs):
        tmp_name = f"extract-{index}.tmp.osm.pbf"
        extracts.append({"output": tmp_name, "polygon": {"file_name": str(Path(poly).resolve()), "file_type": "poly"}})
        finals.append((work / tmp_name, Path(out)))
    config_file = work / "extracts.json"
    config_file.write_text(json.dumps({"directory": str(work), "extracts": extracts}))
    proc = subprocess.Popen(
        [exe, "extract", "-c", str(config_file), "--overwrite", "--progress", str(source)],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    tail, last, peak = b"", -1, 0
    fd = proc.stderr.fileno()
    while True:
        ready, _, _ = select.select([fd], [], [], 2.0)
        peak = max(peak, _peak_rss(proc.pid))
        free = available_gb()
        if min_free_gb and free is not None and free < min_free_gb:
            proc.kill()
            proc.wait()
            raise OsmiumMemoryError(
                f"osmium was stopped: only {free:.1f} GB memory was available (limit {min_free_gb} GB) while it used "
                f"{peak / 1e9:.1f} GB. Lower the batch size (Settings → Country extraction memory)."
            )
        if not ready:
            continue
        chunk = os.read(fd, 4096)
        if not chunk:
            break
        tail = (tail + chunk)[-2000:]
        found = re.findall(rb"(\d{1,3})%", chunk)
        if found and int(found[-1]) != last:
            last = int(found[-1])
            progress_cb(last, f"reading the planet, {last}%")
    if proc.wait() != 0:
        message = re.sub(r"\[[=>\s]*\]\s*\d+%\s*", "", tail.decode(errors="replace")).strip()  # drop the progress bar
        raise OsmiumError(f"osmium extract failed (exit {proc.returncode}): {message[-500:]}")
    for tmp, final in finals:
        final.parent.mkdir(parents=True, exist_ok=True)
        tmp.replace(final)
    config_file.unlink(missing_ok=True)
    return peak


def bbox_coverage(data: list[float] | None, polygon: list[float] | None) -> float | None:
    """Share (0..1) of the polygon's bounding box that the data's bounding box covers; None when unknown.

    The data bbox normally *exceeds* the polygon (osmium keeps whole ways, so ways crossing the border bring
    their outside nodes along, exactly like Geofabrik's extracts); what signals a wrong cut is the data
    covering only a small part of the polygon."""
    if not data or not polygon or data[2] <= data[0] or data[3] <= data[1]:
        return None  # nothing (or a single point/line) to compare
    width = max(0.0, min(data[2], polygon[2]) - max(data[0], polygon[0]))
    height = max(0.0, min(data[3], polygon[3]) - max(data[1], polygon[1]))
    area = (polygon[2] - polygon[0]) * (polygon[3] - polygon[1])
    return min(1.0, width * height / area) if area > 0 else None
