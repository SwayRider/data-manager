"""Valhalla routing data of one region: config, admins, timezones, tiles, extract and the edge polylines Pelias reads.
The commands and arguments are the legacy pipeline's (`valhalla_funcs.py`), run without a shell."""
import gzip
import os
import shutil
import sqlite3
import tarfile
from pathlib import Path

from datamanager.services import valhalla_build as vb

FILES = {"tiles": "tiles.tar", "admin": "admin.sqlite", "timezones": "tz_world.sqlite", "polylines": "polylines.0sv.gz"}


class DataError(Exception):
    pass


def _tool(session, name: str) -> str:
    return vb.binary(session, name)


def build_timezones(session, target: Path, log) -> Path:
    """valhalla_build_timezones downloads its own data and prints the database to stdout; independent of the region."""
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "wb") as out:
        vb.run([_tool(session, "valhalla_build_timezones")], stdout=out, log=log)
    return target


def build_region(session, pbf: Path, srtm_dir: Path, work: Path, out_dir: Path, tz_file: Path,
                 concurrency: int, step_cb=lambda _: None) -> dict:
    """Writes `FILES` into `out_dir` and returns facts about them. Raises DataError / vb.BuildError."""
    shutil.rmtree(work, ignore_errors=True)
    (work / "tiles").mkdir(parents=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    cfg, tar, admin = work / "valhalla.json", work / "tiles.tar", work / "admin.sqlite"
    tz = work / "tz_world.sqlite"
    shutil.copyfile(tz_file, tz)
    with open(work / "build.log", "w", encoding="utf-8") as log:
        step_cb("config")
        with open(cfg, "wb") as out:
            vb.run([_tool(session, "valhalla_build_config"), "--mjolnir-tile-dir", str(work / "tiles"), "--mjolnir-tile-extract", str(tar),
                    "--mjolnir-admin", str(admin), "--mjolnir-timezone", str(tz), "--additional-data-elevation", str(srtm_dir)],
                   stdout=out, log=log)
        step_cb("admins")
        vb.run([_tool(session, "valhalla_build_admins"), "--config", str(cfg), str(pbf)], log=log)
        step_cb("tiles")
        vb.run([_tool(session, "valhalla_build_tiles"), "--config", str(cfg), f"--concurrency={concurrency}", str(pbf)], log=log)
        step_cb("tiles.tar")
        vb.run([_tool(session, "valhalla_build_extract"), "--config", str(cfg)], log=log)
        step_cb("polylines")
        raw = work / "polylines.raw"
        with open(raw, "wb") as out:
            vb.run([_tool(session, "valhalla_export_edges"), "--config", str(cfg)], stdout=out, log=log)
        gz = work / FILES["polylines"]
        with open(raw, "rb") as src, gzip.open(gz, "wb") as dst:
            shutil.copyfileobj(src, dst)
    facts = validate(tar, admin, tz, gz)
    for key, source in (("tiles", tar), ("admin", admin), ("timezones", tz), ("polylines", gz)):
        _place(source, out_dir / FILES[key])
    return facts


def _place(source: Path, target: Path) -> None:
    target.unlink(missing_ok=True)
    try:
        os.link(source, target)  # the work dir is removed right after; no second copy of big files
    except OSError:
        shutil.copyfile(source, target)


def _tables(path: Path) -> int:
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
        return db.execute("select count(*) from sqlite_master where type='table'").fetchone()[0]


def validate(tar: Path, admin: Path, tz: Path, polylines: Path) -> dict:
    for path in (tar, admin, tz, polylines):
        if not path.is_file() or path.stat().st_size == 0:
            raise DataError(f"{path.name} was not produced or is empty")
    try:
        with tarfile.open(tar) as archive:
            tiles = sum(1 for m in archive if m.isfile() and m.name.endswith(".gph"))
    except tarfile.TarError as exc:
        raise DataError(f"tiles.tar is not a readable archive: {exc}") from exc
    if tiles == 0:
        raise DataError("tiles.tar contains no graph tiles")
    for path in (admin, tz):
        try:
            if _tables(path) == 0:
                raise DataError(f"{path.name} has no tables")
        except sqlite3.Error as exc:
            raise DataError(f"{path.name} is not a SQLite database: {exc}") from exc
    with gzip.open(polylines, "rt", encoding="utf-8", errors="replace") as f:
        lines = sum(1 for _ in f)
    sizes = {"tiles": tar, "admin": admin, "timezones": tz, "polylines": polylines}
    return {"tiles": tiles, "polyline_lines": lines, "bytes": {k: p.stat().st_size for k, p in sizes.items()}}
