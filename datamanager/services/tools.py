"""Detect the external tools listed in `datamanager/tools.py`.

Detection is cached per process (first look runs each tool's version command once); the UI's
re-detect buttons call `detect(..., force=True)` so a tool installed meanwhile is picked up without a
restart. Binaries are only run with fixed arguments, never through a shell."""
import datetime
import os
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass

from sqlalchemy.orm import Session

from datamanager.config import config
from datamanager.errors import ValidationError
from datamanager.services import settings as settings_service
from datamanager.tools import BY_KEY, TOOLS, ToolDef

VERSION_TIMEOUT_S = 5
_VERSION_RE = re.compile(r"(\d+(?:\.\d+)+|\d+)")


@dataclass(frozen=True)
class ToolStatus:
    key: str
    status: str  # ok | outdated | missing | error
    path: str | None = None
    version: str | None = None
    message: str = ""
    override: str | None = None
    checked_at: datetime.datetime | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"


_cache: dict[str, ToolStatus] = {}
_lock = threading.Lock()


def clear_cache() -> None:
    with _lock:
        _cache.clear()


def _version_tuple(text: str) -> tuple[int, ...] | None:
    match = _VERSION_RE.search(text)
    return tuple(int(p) for p in match.group(1).split(".")) if match else None


def _locate(tool: ToolDef, override: str | None) -> tuple[str | None, str]:
    """(path, problem) — path is None when nothing usable was found."""
    if override:
        if not os.path.isfile(override):
            return None, f"{override} does not exist"
        if tool.kind == "binary" and not os.access(override, os.X_OK):
            return None, f"{override} is not executable"
        return override, ""
    if tool.kind == "file":
        default = os.path.join(config.DATA_ROOT, tool.default_file or "")
        return (default, "") if os.path.isfile(default) else (None, "")
    for name in tool.binaries:
        found = shutil.which(name)
        if found:
            return found, ""
    return None, ""


def _run(tool: ToolDef, path: str, override: str | None, now: datetime.datetime) -> ToolStatus:
    try:
        proc = subprocess.run([path, *tool.version_args], capture_output=True, text=True, timeout=VERSION_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as exc:
        return ToolStatus(tool.key, "error", path, None, f"could not run: {exc}", override, now)
    output = (proc.stdout + "\n" + proc.stderr).strip()
    version = _version_tuple(output)
    if proc.returncode != 0 and version is None:
        return ToolStatus(tool.key, "error", path, None, f"version check failed (exit {proc.returncode})", override, now)
    text = ".".join(map(str, version)) if version else None
    if tool.min_version and version and version < tool.min_version:
        need = ".".join(map(str, tool.min_version))
        return ToolStatus(tool.key, "outdated", path, text, f"version {need} or newer needed", override, now)
    return ToolStatus(tool.key, "ok", path, text, "", override, now)


def _detect_library(tool: ToolDef, now: datetime.datetime) -> ToolStatus:
    """A system library: version and location from pkg-config (or ldconfig), and its data files in one of `tool.data_dirs`."""
    version = path = None
    if shutil.which("pkg-config") and tool.pkg_config:
        try:
            proc = subprocess.run(["pkg-config", "--modversion", tool.pkg_config], capture_output=True, text=True, timeout=VERSION_TIMEOUT_S)
            if proc.returncode == 0 and proc.stdout.strip():
                version = proc.stdout.strip()
                libdir = subprocess.run(["pkg-config", "--variable=libdir", tool.pkg_config], capture_output=True, text=True, timeout=VERSION_TIMEOUT_S)
                path = libdir.stdout.strip() or None
        except (OSError, subprocess.SubprocessError):
            pass
    if version is None and shutil.which("ldconfig"):
        try:
            listing = subprocess.run(["ldconfig", "-p"], capture_output=True, text=True, timeout=VERSION_TIMEOUT_S).stdout
        except (OSError, subprocess.SubprocessError):
            listing = ""
        for line in listing.splitlines():
            if f"lib{tool.key}.so" in line:
                path = line.rsplit("=>", 1)[-1].strip()
                version = "?"
                break
    if version is None:
        return ToolStatus(tool.key, "missing", None, None, "", None, now)
    if tool.data_dirs:
        candidates = [os.environ.get(d[1:], "") if d.startswith("$") else d for d in tool.data_dirs]
        if not any(c and os.path.exists(os.path.join(c, tool.data_marker)) for c in candidates):
            return ToolStatus(tool.key, "error", path, version, "installed, but its data files were not found in " + ", ".join(c for c in candidates if c)
                              + " (download them, see below)", None, now)
    return ToolStatus(tool.key, "ok", path, version, "", None, now)


def _detect_now(session: Session, tool: ToolDef) -> ToolStatus:
    now = datetime.datetime.now(datetime.UTC)
    if tool.kind == "library":
        return _detect_library(tool, now)
    override = settings_service.get_raw(session, settings_service.TOOLPATH_PREFIX + tool.key) or None
    path, problem = _locate(tool, override)
    if path is None:
        return ToolStatus(tool.key, "error" if problem else "missing", None, None, problem, override, now)
    if tool.kind == "file":
        return ToolStatus(tool.key, "ok", path, None, "", override, now)
    return _run(tool, path, override, now)


def _detect_built(session: Session, tool: ToolDef) -> ToolStatus:
    from datamanager.services.built_tools import BUILDERS  # lazy: the builders use this module for the prerequisites

    found = BUILDERS[tool.key].detect(session)
    return ToolStatus(tool.key, found["status"], found["path"], found["version"], found["message"], None, datetime.datetime.now(datetime.UTC))


def detect(session: Session, key: str, force: bool = False) -> ToolStatus:
    tool = BY_KEY[key]
    if tool.kind == "built":  # reads a record and the files: cheap, and the worker changes it, so never cached
        return _detect_built(session, tool)
    with _lock:
        cached = _cache.get(key)
    if cached is not None and not force:
        return cached
    status = _detect_now(session, tool)
    with _lock:
        _cache[key] = status
    return status


def detect_all(session: Session, force: bool = False) -> list[tuple[ToolDef, ToolStatus]]:
    return [(tool, detect(session, tool.key, force)) for tool in TOOLS]


def set_path(session: Session, key: str, path: str) -> ToolStatus:
    """Save (or clear, when blank) a custom binary path and re-detect that tool."""
    if key not in BY_KEY:
        raise ValidationError(f"Unknown tool: {key}")
    if BY_KEY[key].kind != "binary" and BY_KEY[key].kind != "file":
        raise ValidationError(f"{BY_KEY[key].label} has no custom path.")
    path = path.strip()
    if len(path) > 500:
        raise ValidationError("Path is too long.")
    settings_service.set_raw(session, settings_service.TOOLPATH_PREFIX + key, path or None)
    session.commit()
    return detect(session, key, force=True)


def problems(session: Session) -> dict[str, list[str]]:
    """Labels of tools that are not ok, split into 'required' and 'optional'."""
    result: dict[str, list[str]] = {"required": [], "optional": []}
    for tool, status in detect_all(session):
        if not status.ok:
            result["required" if tool.required else "optional"].append(tool.label)
    return result
