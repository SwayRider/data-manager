"""Building the Pelias interpolation databases (`street.db`, `address.db`) of one region.

The cloned `interpolation` repository is driven through the same node entry points its `script/*.sh` use, because
`./interpolate build` needs TIGER and a `find`-based OpenAddresses concatenation:

    polylines.0sv.gz                    -> node cmd/polyline.js street.db
    openaddresses geojson (as CSV, sorted by street) -> node cmd/oa.js address.db street.db
    region PBF -> pbf2json (addresses)  -> node cmd/osm.js address.db street.db
    (both databases)                    -> node cmd/vertices.js address.db street.db

Our OpenAddresses downloads are line-delimited GeoJSON; the importer reads the legacy CSV, so `openaddresses_csv` converts
and sorts them. The skip file (file descriptor 3 of the node scripts) lists records without a matching street."""
import csv
import gzip
import json
import os
import platform
import sqlite3
import subprocess
from pathlib import Path

from datamanager.services.valhalla_build import BuildError

CSV_HEADER = ["LON", "LAT", "NUMBER", "STREET", "UNIT", "CITY", "DISTRICT", "REGION", "POSTCODE", "ID", "HASH"]
SORT_KEYS = ["-t,", "-k4,4d", "-k6,6d", "-k7,7d", "-k8,8d", "-k3,3n"]  # script/concat_oa.sh: street, city, district, region, number
TAGS = "addr:housenumber+addr:street"
LOG_TAIL = 15


def pbf2json_binary(repo: Path) -> Path:
    arch = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine().lower(), platform.machine())
    return repo / "node_modules" / "pbf2json" / "build" / f"pbf2json.{platform.system().lower()}-{arch}"


# ---- OpenAddresses: GeoJSON -> sorted CSV -------------------------------------------------------------------------

def _row(source: str, line: bytes, counter: int) -> list[str] | None:
    try:
        feature = json.loads(line)
        lon, lat = feature["geometry"]["coordinates"][:2]
        props = feature.get("properties") or {}
    except (ValueError, KeyError, TypeError):
        return None
    number, street = str(props.get("number") or "").strip(), str(props.get("street") or "").strip()
    if not number or not street:
        return None
    ident = str(props.get("hash") or props.get("id") or counter)
    return [str(lon), str(lat), number, street, *(str(props.get(k) or "").strip() for k in ("unit", "city", "district", "region", "postcode")),
            str(props.get("id") or ""), f"{source}:{ident}"]


def openaddresses_csv(files: dict[str, Path], target: Path, tmp_dir: Path) -> dict:
    """Convert the sources' GeoJSON (plain or gzipped) into one CSV sorted so that addresses of a street are adjacent (a
    large speed-up of the importer). Returns {rows, skipped}."""
    tmp_dir.mkdir(parents=True, exist_ok=True)
    unsorted = tmp_dir / "oa-unsorted.csv"
    rows = skipped = 0
    with open(unsorted, "w", encoding="utf-8", newline="") as out:
        writer = csv.writer(out, lineterminator="\n")
        for source, file in sorted(files.items()):
            opener = gzip.open if str(file).endswith(".gz") else open
            with opener(file, "rb") as handle:
                for counter, line in enumerate(handle, 1):
                    if not line.strip():
                        continue
                    row = _row(source, line, counter)
                    if row is None:
                        skipped += 1
                        continue
                    writer.writerow(row)
                    rows += 1
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w", encoding="utf-8", newline="") as out:
        out.write(",".join(CSV_HEADER) + "\n")
        out.flush()
        proc = subprocess.run(["sort", *SORT_KEYS, "-T", str(tmp_dir), str(unsorted)], stdout=out, stderr=subprocess.PIPE,
                              env={**os.environ, "LC_ALL": "C"})
    unsorted.unlink(missing_ok=True)
    if proc.returncode != 0:
        raise BuildError("sort failed: " + proc.stderr.decode(errors="replace")[-200:])
    return {"rows": rows, "skipped": skipped}


# ---- running the node scripts -------------------------------------------------------------------------------------

def _env(tmp_dir: Path) -> dict:
    tmp_dir.mkdir(parents=True, exist_ok=True)
    return {**os.environ, "LC_ALL": "en_US.UTF-8", "SQLITE_TMPDIR": str(tmp_dir)}


def _tail(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()[-LOG_TAIL:]
    except OSError:
        return []


def _count_lines(path: Path) -> int:
    try:
        with open(path, "rb") as handle:
            return sum(1 for _ in handle)
    except OSError:
        return 0


def run_script(node: str, repo: Path, script: str, args: list[str], logs: Path, step: str, tmp_dir: Path, lines=None) -> dict:
    """`node cmd/<script> <args>` with `lines` (an iterable of bytes) on stdin. stdout/stderr and the skip file (fd 3) go to
    `logs/<step>.{out,err,skip}`; raises BuildError with the stderr tail on a non-zero exit. Returns {skipped, errors}."""
    logs.mkdir(parents=True, exist_ok=True)
    out, err, skip = logs / f"{step}.out", logs / f"{step}.err", logs / f"{step}.skip"
    command = f'exec "$NODE" "$SCRIPT" "$@" 1>"$OUT" 2>"$ERR" 3>"$SKIP"'
    env = {**_env(tmp_dir), "NODE": node, "SCRIPT": str(repo / "cmd" / script), "OUT": str(out), "ERR": str(err), "SKIP": str(skip)}
    proc = subprocess.Popen(["bash", "-c", command, "bash", *args], cwd=repo, env=env, stdin=subprocess.PIPE if lines is not None else None)
    _feed(proc, lines)
    if proc.wait() != 0:
        raise BuildError(f"interpolation {step} failed (exit {proc.returncode}): " + " | ".join(_tail(err)))
    return {"skipped": _count_lines(skip), "errors": _count_lines(err)}


def _feed(proc: subprocess.Popen, lines) -> None:
    if lines is None:
        return
    try:
        for line in lines:
            proc.stdin.write(line)
    except BrokenPipeError:
        pass  # the script exited early: its exit code and stderr say why
    finally:
        try:
            proc.stdin.close()
        except BrokenPipeError:
            pass


def polyline_lines(file: Path):
    """The Valhalla polylines asset without its log lines (every street row has NUL separators)."""
    with gzip.open(file, "rb") as handle:
        for line in handle:
            if b"\0" in line:
                yield line


def csv_lines(file: Path):
    """The sorted CSV with consecutive identical lines removed (`uniq` of script/concat_oa.sh)."""
    previous = None
    with open(file, "rb") as handle:
        for line in handle:
            if line != previous:
                yield line
            previous = line


def run_osm(node: str, repo: Path, pbf: Path, address_db: Path, street_db: Path, logs: Path, tmp_dir: Path, leveldb: Path) -> dict:
    """pbf2json | node cmd/osm.js."""
    binary = pbf2json_binary(repo)
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise BuildError(f"pbf2json not found or not executable: {binary}")
    leveldb.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    out, err, skip = logs / "osm.out", logs / "osm.err", logs / "osm.skip"
    pbf_err = logs / "pbf2json.err"
    with open(pbf_err, "wb") as pbf_log:
        producer = subprocess.Popen([str(binary), f"-tags={TAGS}", f"-leveldb={leveldb}", str(pbf)], stdout=subprocess.PIPE, stderr=pbf_log)
        env = {**_env(tmp_dir), "NODE": node, "SCRIPT": str(repo / "cmd" / "osm.js"), "OUT": str(out), "ERR": str(err), "SKIP": str(skip)}
        consumer = subprocess.Popen(["bash", "-c", 'exec "$NODE" "$SCRIPT" "$@" 1>"$OUT" 2>"$ERR" 3>"$SKIP"', "bash", str(address_db), str(street_db)],
                                    cwd=repo, env=env, stdin=producer.stdout)
        producer.stdout.close()
        code, producer_code = consumer.wait(), producer.wait()
    if producer_code != 0:
        raise BuildError(f"pbf2json failed (exit {producer_code}): " + " | ".join(_tail(pbf_err)))
    if code != 0:
        raise BuildError(f"interpolation osm failed (exit {code}): " + " | ".join(_tail(err)))
    return {"skipped": _count_lines(skip), "errors": _count_lines(err)}


# ---- counts -------------------------------------------------------------------------------------------------------

def table_count(db: Path, table: str) -> int | None:
    try:
        connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        finally:
            connection.close()
    except sqlite3.Error:
        return None
