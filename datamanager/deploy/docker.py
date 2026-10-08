"""The few docker calls the activators need. Tests replace `run`."""
import json
import subprocess
import time

import requests

from datamanager.errors import DeployError

sleep = time.sleep


def docker_path() -> str:
    from datamanager.db import SessionLocal
    from datamanager.services import tools

    status = tools.detect(SessionLocal(), "docker", force=False)
    if not status.ok:
        raise DeployError(f"docker is not available ({status.message or status.status}); see Settings → Tools")
    return status.path


def run(args: list[str], timeout: float = 300, check: bool = True) -> subprocess.CompletedProcess:
    try:
        proc = subprocess.run([docker_path(), *args], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DeployError(f"docker {args[0]}: {exc}") from exc
    if check and proc.returncode != 0:
        raise DeployError(f"docker {' '.join(args[:2])} failed (exit {proc.returncode}): {(proc.stderr or proc.stdout).strip()[-300:]}")
    return proc


def restart(name: str) -> None:
    run(["restart", name])


def compose_recreate(compose_file: str, service: str) -> None:
    """Create or recreate one service of a compose file (without its dependencies). Unlike `restart` this also works
    before the container exists, which is the case at the first deploy: the compose files do not start a service whose
    data directory is missing."""
    run(["compose", "-f", compose_file, "up", "-d", "--no-deps", "--force-recreate", service], timeout=900)


def compose_container(compose_file: str, service: str) -> str:
    proc = run(["compose", "-f", compose_file, "ps", "-q", "-a", service], timeout=60, check=False)
    ident = (proc.stdout or "").strip().splitlines()
    if proc.returncode != 0 or not ident:
        raise DeployError(f"compose service {service} has no container after starting it")
    return ident[0]


def state(name: str) -> dict:
    proc = run(["inspect", "-f", "{{json .State}}", name], timeout=30, check=False)
    if proc.returncode != 0:
        return {"Status": "missing"}
    try:
        return json.loads(proc.stdout)
    except ValueError:
        return {"Status": "unknown"}


def logs(name: str, lines: int = 6) -> str:
    proc = run(["logs", "--tail", str(lines), name], timeout=30, check=False)
    return ((proc.stdout or "") + (proc.stderr or "")).strip()[-400:]


def wait_ready(names: list[str], timeout: float, urls: dict[str, str] | None = None) -> None:
    """Every container is running (and healthy when it has a health check); every URL answers 2xx. Fails fast when a
    container exits or restarts in a loop."""
    deadline = time.monotonic() + timeout
    pending = {n: "" for n in names}
    waiting_urls = dict(urls or {})
    while True:
        for name in list(pending):
            st = state(name)
            if st.get("Status") in ("missing", "exited", "dead") or st.get("Restarting"):
                raise DeployError(f"{name} is {st.get('Status')}: {logs(name)}")
            health = (st.get("Health") or {}).get("Status")
            if st.get("Running") and health in (None, "healthy"):
                del pending[name]
            else:
                pending[name] = f"{st.get('Status')}{'/' + health if health else ''}"
        for name, url in list(waiting_urls.items()):
            try:
                if requests.get(url, timeout=3).ok:
                    del waiting_urls[name]
            except requests.RequestException:
                pass
        if not pending and not waiting_urls:
            return
        if time.monotonic() >= deadline:
            raise DeployError("not ready in time: " + ", ".join([f"{n} ({s})" for n, s in pending.items()] + [f"{n} ({u})" for n, u in waiting_urls.items()]))
        sleep(1)
