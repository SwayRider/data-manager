"""Compiling Valhalla from source into the tools dir (`DATA_ROOT/tools/`), started from Settings → Tools.

The build is a machine-local tool cache, not an asset: `build()` clones the configured tag (`tool.valhalla_tag`),
runs cmake and make, and records the result in `tools/valhalla/state.json` (log: `build.log`). Detecting the tool
only reads that record and checks the binaries, so it is cheap and needs no network. Binaries are run straight from
the build directory, as the legacy pipeline did (no `make install`)."""
import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from datamanager.config import config
from datamanager.errors import ValidationError
from datamanager.services import settings as settings_service

REPO = "https://github.com/valhalla/valhalla"
CMAKE_OPTIONS = (
    "-DENABLE_PYTHON_BINDINGS=OFF", "-DENABLE_SERVICES=OFF", "-DENABLE_BENCHMARKS=OFF", "-DENABLE_TESTS=OFF",
    "-DCMAKE_CXX_FLAGS=-Wno-error=format-truncation -Wno-error=deprecated-declarations",
    "-DCMAKE_POLICY_DEFAULT_CMP0144=OLD", "-DSQLITE_ENABLE_LOAD_EXTENSION=ON",
)
BINARIES = (
    "valhalla_build_config", "valhalla_build_admins", "valhalla_build_timezones", "valhalla_build_tiles",
    "valhalla_build_extract", "valhalla_export_edges",
)
PREREQUISITES = ("git", "cmake", "make", "gxx")
_TAG_RE = re.compile(r"refs/tags/(v?\d+\.\d+[\.\d]*)$")
ACTIVE = ("queued", "building")
GIT_ENV = {"GIT_TERMINAL_PROMPT": "0"}


class BuildError(Exception):
    pass


def tools_dir() -> Path:
    return Path(config.DATA_ROOT) / "tools" / "valhalla"


def state_file() -> Path:
    return tools_dir() / "state.json"


def log_file() -> Path:
    return tools_dir() / "build.log"


def flags_hash() -> str:
    return hashlib.sha256(json.dumps([REPO, CMAKE_OPTIONS]).encode()).hexdigest()[:8]


def read_state() -> dict:
    try:
        return json.loads(state_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_state(state: dict) -> None:
    tools_dir().mkdir(parents=True, exist_ok=True)
    tmp = state_file().with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
    tmp.replace(state_file())


def log_tail(lines: int = 12) -> list[str]:
    try:
        return log_file().read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]
    except OSError:
        return []


def bin_dir(state: dict | None = None) -> Path | None:
    state = state or read_state()
    return Path(state["dir"]) / "build" if state.get("dir") else None


def detect(session) -> dict:
    """{status: ok|outdated|missing|building|error, path, version, message} from the record and the files on disk."""
    state = read_state()
    if state.get("status") in ACTIVE:
        return {"status": "building", "message": "Build " + state["status"] + ".", "path": None, "version": state.get("resolved")}
    if state.get("status") == "failed":
        return {"status": "error", "message": "The last build failed: " + str(state.get("message", ""))[:200], "path": None, "version": None}
    directory = bin_dir(state)
    if state.get("status") != "built" or directory is None:
        return {"status": "missing", "message": "Not built yet.", "path": None, "version": None}
    missing = [b for b in BINARIES if not (directory / b).is_file()]
    if missing:
        return {"status": "error", "message": "Missing in the build: " + ", ".join(missing), "path": str(directory), "version": state.get("resolved")}
    wanted = str(settings_service.get(session, "tool.valhalla_tag"))
    version, path = state.get("resolved"), str(directory)
    if state.get("flags") != flags_hash():
        return {"status": "outdated", "message": "Built with other build options: rebuild.", "path": path, "version": version}
    if wanted != "latest" and wanted != state.get("tag"):
        return {"status": "outdated", "message": f"Built {state.get('tag')}, Settings ask for {wanted}: rebuild.", "path": path, "version": version}
    return {"status": "ok", "message": "", "path": path, "version": version}


def binary(session, name: str) -> str:
    """Path of a Valhalla binary, or ValidationError when Valhalla is not built (Settings → Tools)."""
    found = detect(session)
    if found["status"] not in ("ok", "outdated"):
        raise ValidationError("Valhalla is not built: build it under Settings → Tools. " + found["message"])
    return str(Path(found["path"]) / name)


def version(session) -> str:
    """Identifies the build for fingerprints: resolved tag and build options."""
    state = read_state()
    return f"{state.get('resolved', '?')}-{state.get('flags', '?')}"


def run(args: list[str], *, cwd: Path | None = None, stdout=None, log=None, env: dict | None = None) -> None:
    """Fixed-argument subprocess (never a shell); output goes to `log`. Raises BuildError on a non-zero exit."""
    try:
        proc = subprocess.run(args, cwd=cwd, stdout=stdout if stdout is not None else log, stderr=log, env={**os.environ, **(env or {})})
    except OSError as exc:
        raise BuildError(f"{args[0]}: {exc}") from exc
    if proc.returncode != 0:
        raise BuildError(f"{' '.join(str(a) for a in args[:3])} failed (exit {proc.returncode})")


def resolve_tag(tag: str) -> str:
    """`latest` -> the newest release tag (`vX.Y.Z`); anything else is used as given."""
    if tag != "latest":
        return tag
    proc = subprocess.run(["git", "ls-remote", "--tags", "--sort=-version:refname", REPO], capture_output=True, text=True,
                          env={**os.environ, **GIT_ENV})
    if proc.returncode != 0:
        raise BuildError("git ls-remote failed: " + proc.stderr.strip()[:200])
    for line in proc.stdout.splitlines():
        parts = line.strip().split("\t", 1)
        match = _TAG_RE.search(parts[1]) if len(parts) == 2 else None
        if match:
            return match.group(1)
    raise BuildError("no release tag found in " + REPO)


def request(session) -> None:
    """Marks a build as queued (the caller enqueues the job). Refuses when a prerequisite tool is missing or a build runs."""
    from datamanager.services import tools

    if read_state().get("status") in ACTIVE:
        raise ValidationError("A Valhalla build is already running.")
    missing = [tools.BY_KEY[k].label for k in PREREQUISITES if not tools.detect(session, k, force=True).ok]
    if missing:
        raise ValidationError("Install first: " + ", ".join(missing))
    previous = read_state()
    _write_state({**previous, "status": "queued", "message": ""})


def build(session) -> dict:
    """Clone, configure and compile the configured tag. The previous build is removed only after the new one works."""
    state = read_state()
    previous_dir = state.get("dir")
    tag = str(settings_service.get(session, "tool.valhalla_tag"))
    jobs = int(settings_service.get(session, "tool.valhalla_jobs"))
    tools_dir().mkdir(parents=True, exist_ok=True)
    started = {"status": "building", "tag": tag, "flags": flags_hash(), "started_at": _now(), "message": ""}
    _write_state(started)
    target = None
    with open(log_file(), "w", encoding="utf-8") as log:
        try:
            resolved = resolve_tag(tag)
            started["resolved"] = resolved
            _write_state(started)
            target = tools_dir() / f"valhalla-{resolved}-{flags_hash()}"
            if target.exists():
                shutil.rmtree(target)
            log.write(f"== clone {REPO} {resolved}\n"); log.flush()
            run(["git", "clone", "--branch", resolved, "--single-branch", "--depth", "1", REPO, str(target)], log=log, env=GIT_ENV)
            log.write("== submodules\n"); log.flush()
            run(["git", "submodule", "update", "--init", "--recursive"], cwd=target, log=log, env=GIT_ENV)
            (target / "build").mkdir()
            log.write("== cmake\n"); log.flush()
            run(["cmake", *CMAKE_OPTIONS, ".."], cwd=target / "build", log=log)
            log.write(f"== make -j{jobs}\n"); log.flush()
            run(["make", "all", f"-j{jobs}"], cwd=target / "build", log=log)
            missing = [b for b in BINARIES if not (target / "build" / b).is_file()]
            if missing:
                raise BuildError("the build did not produce: " + ", ".join(missing))
        except (BuildError, OSError) as exc:
            if target is not None:
                shutil.rmtree(target, ignore_errors=True)
            _write_state({**started, "status": "failed", "message": str(exc), "finished_at": _now(), "dir": previous_dir if previous_dir and Path(previous_dir).exists() else None})
            raise
    done = {**started, "status": "built", "dir": str(target), "finished_at": _now()}
    _write_state(done)
    if previous_dir and Path(previous_dir) != target:
        shutil.rmtree(previous_dir, ignore_errors=True)
    return done


def _now() -> str:
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
