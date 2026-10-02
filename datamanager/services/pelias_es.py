"""The temporary Elasticsearch a Pelias run imports into (DESIGN.md "Pelias stage: temporary Elasticsearch").

One docker container per region run: the local image `dm-pelias-es:<version>` (the official image plus `analysis-icu`,
built once), run as the worker's uid with gid 0, bound to 127.0.0.1 on a free port, its data and snapshot directories **bind
mounts on the configured disk, never docker volumes**. Whenever the container stops (success, failure, cancel) it is removed
and both directories are deleted; a killed worker leaves them behind and the next run cleans them up (`cleanup_stale`).
Verified by the 2026-10-02 spike: everything ES writes is owned by the worker's uid, so a plain `rm -rf` works without sudo."""
import json
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

import requests

from datamanager.errors import ValidationError

IMAGE = "dm-pelias-es"
CONTAINER = "dm-pelias-es-"
LABEL = "dm-run"
MIN_MAP_COUNT = 262144  # Elasticsearch refuses to start below this (a host kernel setting)
HEALTH_TIMEOUT_S = 300
DOCKERFILE = "FROM elasticsearch:{version}\nRUN elasticsearch-plugin install --batch analysis-icu\n"
SNAPSHOT_REPO = "dm"


class EsError(Exception):
    pass


def image_tag(version: str) -> str:
    return f"{IMAGE}:{version}"


def docker_bin(session, force: bool = True) -> str:
    """Path of a working docker; the stage re-detects the tools it needs before starting (`force`), the Build page uses the cache."""
    from datamanager.services import tools

    status = tools.detect(session, "docker", force=force)
    if not status.ok:
        raise ValidationError(f"docker is not available ({status.message or status.status}); see Settings → Tools.")
    return status.path


def _docker(docker: str, args: list[str], *, input: str | None = None, check: bool = True, timeout: float | None = 600) -> subprocess.CompletedProcess:
    try:
        proc = subprocess.run([docker, *args], input=input, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise EsError(f"docker {args[0]}: {exc}") from exc
    if check and proc.returncode != 0:
        raise EsError(f"docker {' '.join(args[:2])} failed (exit {proc.returncode}): {(proc.stderr or proc.stdout).strip()[-400:]}")
    return proc


def max_map_count() -> int | None:
    try:
        return int(Path("/proc/sys/vm/max_map_count").read_text().strip())
    except (OSError, ValueError):
        return None


def environment_problems() -> list[str]:
    """Cheap checks that need no docker call: the kernel setting Elasticsearch needs."""
    count = max_map_count()
    if count is not None and count < MIN_MAP_COUNT:
        return [f"vm.max_map_count is {count}, Elasticsearch needs at least {MIN_MAP_COUNT} (sudo sysctl -w vm.max_map_count={MIN_MAP_COUNT})."]
    return []


def preflight(docker: str, version: str, base_dir: Path, need_gb: float = 20.0) -> None:
    """Everything a run needs from docker and the disk, checked before the first importer starts."""
    problems = environment_problems()
    if problems:
        raise EsError(problems[0])
    _docker(docker, ["info", "--format", "{{.ServerVersion}}"], timeout=30)
    base_dir.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(base_dir).free / 1e9
    if free < need_gb:
        raise EsError(f"Only {free:.0f} GB free where Elasticsearch keeps its data ({base_dir}); at least {need_gb:.0f} GB are needed.")
    ensure_image(docker, version)


def ensure_image(docker: str, version: str) -> str:
    """The local image with the ICU plugin, built once from the official image (needs the network that one time)."""
    tag = image_tag(version)
    if _docker(docker, ["image", "inspect", tag], check=False, timeout=60).returncode != 0:
        _docker(docker, ["build", "-t", tag, "-"], input=DOCKERFILE.format(version=version), timeout=1800)
    return tag


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def remove_dir(path: Path, docker: str | None = None) -> None:
    """Delete a bind-mounted directory. Files of another uid (should not happen: the container runs as the worker) are
    removed by a throw-away alpine container instead of failing the cleanup."""
    if not path.exists():
        return
    try:
        shutil.rmtree(path)
        return
    except PermissionError:
        pass
    if docker:
        _docker(docker, ["run", "--rm", "-v", f"{path}:/target", "alpine", "sh", "-c", "rm -rf /target/* /target/.[!.]* 2>/dev/null; true"], check=False, timeout=600)
    shutil.rmtree(path, ignore_errors=True)


class TemporaryElasticsearch:
    """`with TemporaryElasticsearch(...) as es:` starts the container and always removes it and its directories on exit."""

    def __init__(self, docker: str, version: str, heap: str, run_id: int | str, base_dir: Path):
        self.docker, self.version, self.heap, self.run_id = docker, version, heap, str(run_id)
        self.base_dir = Path(base_dir)
        self.data_dir, self.snapshots_dir = self.base_dir / "data", self.base_dir / "snapshots"
        self.name = f"{CONTAINER}{run_id}"
        self.port = 0
        self.started = False

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self) -> "TemporaryElasticsearch":
        try:
            self._start()
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _start(self) -> None:
        for directory in (self.data_dir, self.snapshots_dir):
            directory.mkdir(parents=True, exist_ok=True)
        self.port = free_port()
        self.started = True  # from here on `close` has something to remove
        _docker(self.docker, [
            "run", "-d", "--name", self.name, "--label", f"{LABEL}={self.run_id}", "--user", f"{os.getuid()}:0",
            "-p", f"127.0.0.1:{self.port}:9200",
            "-e", "discovery.type=single-node", "-e", f"ES_JAVA_OPTS=-Xms{self.heap} -Xmx{self.heap}",
            "-e", "xpack.security.enabled=false", "-e", "ingest.geoip.downloader.enabled=false", "-e", "path.repo=/snapshots",
            "-v", f"{self.data_dir}:/usr/share/elasticsearch/data", "-v", f"{self.snapshots_dir}:/snapshots",
            image_tag(self.version)])
        self.wait_ready()

    def wait_ready(self, timeout: float = HEALTH_TIMEOUT_S) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                answer = requests.get(f"{self.url}/_cluster/health", timeout=3)
                if answer.ok and answer.json().get("status") in ("green", "yellow"):
                    return
            except (requests.RequestException, ValueError):
                pass
            if not self.running():
                raise EsError("Elasticsearch stopped while starting: " + self.logs())
            time.sleep(1)
        raise EsError(f"Elasticsearch did not become ready within {timeout:.0f} s: " + self.logs())

    def running(self) -> bool:
        proc = _docker(self.docker, ["inspect", "-f", "{{.State.Running}}", self.name], check=False, timeout=30)
        return proc.returncode == 0 and proc.stdout.strip() == "true"

    def logs(self, lines: int = 8) -> str:
        proc = _docker(self.docker, ["logs", "--tail", str(lines), self.name], check=False, timeout=30)
        return ((proc.stdout or "") + (proc.stderr or "")).strip()[-600:]

    def close(self) -> None:
        """Remove the container, then the directories. Never raises: it runs in `finally` blocks."""
        if not self.started:
            remove_dir(self.base_dir, self.docker)
            return
        try:
            _docker(self.docker, ["rm", "-f", self.name], check=False, timeout=120)
        finally:
            remove_dir(self.base_dir, self.docker)
            self.started = False

    # ---- calls the stage needs --------------------------------------------------------------------------------

    def request(self, method: str, path: str, body: dict | None = None, timeout: float = 600) -> dict:
        try:
            answer = requests.request(method, f"{self.url}{path}", json=body, timeout=timeout)
        except requests.RequestException as exc:
            raise EsError(f"Elasticsearch {method} {path}: {exc}") from exc
        if not answer.ok:
            raise EsError(f"Elasticsearch {method} {path} answered {answer.status_code}: {answer.text[:300]}")
        return answer.json() if answer.content else {}

    def count(self, index: str) -> int:
        return int(self.request("GET", f"/{index}/_count", timeout=120).get("count", 0))

    def refresh(self, index: str) -> None:
        self.request("POST", f"/{index}/_refresh")

    def snapshot(self, index: str, snapshot: str) -> Path:
        """Snapshot one index into a filesystem repository on the bind-mounted directory; returns the repository directory."""
        self.request("PUT", f"/_snapshot/{SNAPSHOT_REPO}", {"type": "fs", "settings": {"location": "/snapshots/repo", "compress": True}})
        answer = self.request("PUT", f"/_snapshot/{SNAPSHOT_REPO}/{snapshot}?wait_for_completion=true",
                              {"indices": index, "include_global_state": False}, timeout=7200)
        state = answer.get("snapshot", {}).get("state")
        if state != "SUCCESS":
            raise EsError(f"Snapshot {snapshot} ended as {state}: {json.dumps(answer)[:300]}")
        return self.snapshots_dir / "repo"


def stale_candidates(es_work_dir: str, data_root: Path) -> dict[str, Path]:
    """run id -> the directory a run's Elasticsearch used: `<es_work_dir>/<run id>`, or `<data root>/work/<run id>/es` by default."""
    found: dict[str, Path] = {}
    if es_work_dir:
        base = Path(es_work_dir)
        if base.is_dir():
            found.update({child.name: child for child in base.iterdir() if child.is_dir() and child.name.isdigit()})
    else:
        work = Path(data_root) / "work"
        if work.is_dir():
            found.update({child.name: child / "es" for child in work.iterdir() if child.name.isdigit() and (child / "es").is_dir()})
    return found


def cleanup_stale(docker: str, active_run_ids: set[str], candidates: dict[str, Path]) -> list[str]:
    """Remove containers and directories of earlier runs that are no longer running (a killed worker never reached its
    `finally`); returns what was removed, for the report."""
    removed = []
    listing = _docker(docker, ["ps", "-a", "--filter", f"label={LABEL}", "--format", "{{.Names}} {{.Label \"" + LABEL + "\"}}"], check=False, timeout=60)
    for line in listing.stdout.splitlines():
        name, _, run_id = line.partition(" ")
        if name and run_id.strip() not in active_run_ids:
            _docker(docker, ["rm", "-f", name], check=False, timeout=120)
            removed.append(f"container {name}")
    for run_id, directory in candidates.items():
        if run_id not in active_run_ids and directory.exists():
            remove_dir(directory, docker)
            removed.append(f"directory {directory}")
    return removed
