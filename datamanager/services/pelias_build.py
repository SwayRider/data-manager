"""Cloning and installing the Pelias importer repositories into the tools dir (`DATA_ROOT/tools/pelias`), started from Settings → Tools.

Same shape as `valhalla_build`: a machine-local tool cache, not an asset. `build()` clones every repository at the configured
ref (`tool.pelias_ref`), runs `npm install` in each and records the commits in `tools/pelias/state.json` (log: `build.log`). The
recorded commits identify the build in fingerprints, so a new build makes the Pelias data outdated. The previous build is
removed only after the new one works. Importers run straight from their repository directory (`repo_dir`)."""
import json
import hashlib
import shutil
import uuid
from pathlib import Path

from datamanager.config import config
from datamanager.errors import ValidationError
from datamanager.services import settings as settings_service
from datamanager.services.valhalla_build import GIT_ENV, BuildError, _now, run  # same subprocess and error handling

BASE = "https://github.com/pelias"
REPOS = ("schema", "whosonfirst", "geonames", "openaddresses", "openstreetmap", "polylines", "csv-importer", "transit", "interpolation")
PREREQUISITES = ("git", "node", "npm", "libpostal")  # libpostal: the interpolation importer compiles node-postal against it
ACTIVE = ("queued", "building")


def tools_dir() -> Path:
    return Path(config.DATA_ROOT) / "tools" / "pelias"


def state_file() -> Path:
    return tools_dir() / "state.json"


def log_file() -> Path:
    return tools_dir() / "build.log"


def flags_hash() -> str:
    return hashlib.sha256(json.dumps([BASE, REPOS]).encode()).hexdigest()[:8]


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


def _root(state: dict | None = None) -> Path | None:
    state = state or read_state()
    return Path(state["dir"]) if state.get("dir") else None


def repo_dir(name: str, state: dict | None = None) -> Path | None:
    root = _root(state)
    return root / name if root else None


def detect(session) -> dict:
    """{status: ok|outdated|missing|building|error, path, version, message} from the record and the files on disk."""
    state = read_state()
    if state.get("status") in ACTIVE:
        return {"status": "building", "message": "Build " + state["status"] + ".", "path": None, "version": None}
    if state.get("status") == "failed":
        return {"status": "error", "message": "The last build failed: " + str(state.get("message", ""))[:200], "path": None, "version": None}
    root = _root(state)
    if state.get("status") != "built" or root is None:
        return {"status": "missing", "message": "Not built yet.", "path": None, "version": None}
    if state.get("flags") != flags_hash():  # before the file check: a repository added since the build is "outdated", not broken
        return {"status": "outdated", "message": "Built with another repository list: rebuild.", "path": str(root), "version": version(session)}
    missing = [r for r in REPOS if not (root / r / "node_modules").is_dir()]
    if missing:
        return {"status": "error", "message": "Missing in the build: " + ", ".join(missing), "path": str(root), "version": version(session)}
    wanted = str(settings_service.get(session, "tool.pelias_ref"))
    if wanted != state.get("ref"):
        return {"status": "outdated", "message": f"Built {state.get('ref')}, Settings ask for {wanted}: rebuild.", "path": str(root), "version": version(session)}
    return {"status": "ok", "message": "", "path": str(root), "version": version(session)}


def require(session, name: str) -> Path:
    """Directory of one importer repository, or ValidationError when Pelias is not built (Settings → Tools)."""
    found = detect(session)
    if found["status"] not in ("ok", "outdated"):
        raise ValidationError("The Pelias importers are not built: build them under Settings → Tools. " + found["message"])
    return Path(found["path"]) / name


def version(session=None) -> str:
    """Identifies the build for fingerprints: the commit of every repository (order of REPOS)."""
    commits = read_state().get("commits", {})
    return "-".join(commits.get(r, "?")[:7] for r in REPOS)


def request(session) -> None:
    """Marks a build as queued (the caller enqueues the job). Refuses when a prerequisite is missing, a build runs or the path has spaces."""
    from datamanager.services import tools

    if read_state().get("status") in ACTIVE:
        raise ValidationError("A Pelias build is already running.")
    missing = [tools.BY_KEY[k].label for k in PREREQUISITES if not tools.detect(session, k, force=True).ok]
    if missing:
        raise ValidationError("Install first: " + ", ".join(missing))
    if any(c.isspace() for c in str(tools_dir())):  # the schema importer runs `create_index.js` through an unquoted shell command
        raise ValidationError(f"The data root path contains spaces ({tools_dir()}): the Pelias schema importer cannot run from there.")
    _write_state({**read_state(), "status": "queued", "message": ""})


def build(session) -> dict:
    """Clone and install every repository at the configured ref. The previous build is removed only after the new one works."""
    state = read_state()
    previous_dir = state.get("dir")
    ref = str(settings_service.get(session, "tool.pelias_ref"))
    tools_dir().mkdir(parents=True, exist_ok=True)
    started = {"status": "building", "ref": ref, "flags": flags_hash(), "started_at": _now(), "message": ""}
    _write_state(started)
    target = tools_dir() / f"pelias-{_slug(ref)}-{flags_hash()}-{uuid.uuid4().hex[:6]}"
    commits: dict[str, str] = {}
    with open(log_file(), "w", encoding="utf-8") as log:
        try:
            target.mkdir()
            for name in REPOS:
                log.write(f"== clone {BASE}/{name} {ref}\n"); log.flush()
                run(["git", "clone", "--branch", ref, "--single-branch", "--depth", "1", f"{BASE}/{name}", str(target / name)], log=log, env=GIT_ENV)
                commits[name] = _head(target / name)
                log.write(f"== npm install {name}\n"); log.flush()
                run(["npm", "install", "--no-audit", "--no-fund"], cwd=target / name, log=log)
                run(["npm", "install", "--no-save", "--no-audit", "--no-fund", "pelias-config"], cwd=target / name, log=log)  # as the legacy pipeline
        except (BuildError, OSError) as exc:
            shutil.rmtree(target, ignore_errors=True)
            _write_state({**started, "status": "failed", "message": str(exc), "finished_at": _now(),
                          "dir": previous_dir if previous_dir and Path(previous_dir).exists() else None, "commits": state.get("commits", {})})
            raise
    done = {**started, "status": "built", "dir": str(target), "commits": commits, "finished_at": _now()}
    _write_state(done)
    if previous_dir and Path(previous_dir) != target:
        shutil.rmtree(previous_dir, ignore_errors=True)
    return done


def _head(repo: Path) -> str:
    import subprocess

    proc = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True)
    if proc.returncode != 0:
        raise BuildError(f"git rev-parse failed in {repo.name}")
    return proc.stdout.strip()


def _slug(text: str) -> str:
    return "".join(c if c.isalnum() or c in ".-" else "_" for c in text)
