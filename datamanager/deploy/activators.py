"""Activators for the file classes: restart containers (valhalla, geodata) and restore Elasticsearch indices (pelias).

`activate` blocks of a deploy configuration (`RELEASE-CONTRACT.md` §3.2):
  {"type": "compose-restart", "services": {"benelux": "valhalla-benelux", ...}}      per region, or {"all": "regionservice"}
  {"type": "pelias-restore", "es_url": "http://localhost:39200", "restart": {"region": ["pelias-{region}-pip", ...],
                                                                            "shared": ["pelias-placeholder"]}}
With `"compose_file": "<path>"` in the block the names are compose services and are (re)created with `docker compose up -d
--no-deps --force-recreate` (needed at the first deploy, when the containers do not exist yet); without it they are
container names and `docker restart` is used, which also re-resolves the bind-mounted `current` symlink."""
import json
from pathlib import Path

import requests

from datamanager.deploy import docker
from datamanager.deploy.base import ACTIVATORS, ActivationContext, Activator
from datamanager.errors import DeployError

SNAPSHOT_MOUNT = "/usr/share/elasticsearch/snapshots"  # where the Elasticsearch container sees `es_snapshots`


def _restart(ctx: ActivationContext, name: str) -> None:
    """`compose_file` in the activate block = the names are compose services, (re)created with compose; otherwise they are
    container names and are restarted (`docker restart` re-resolves the bind-mounted `current` symlink)."""
    compose = ctx.settings.get("compose_file")
    docker.compose_recreate(compose, name) if compose else docker.restart(name)


def _containers(ctx: ActivationContext, names: list[str]) -> list[str]:
    compose = ctx.settings.get("compose_file")
    return [docker.compose_container(compose, n) for n in names] if compose else names


class ComposeRestart(Activator):
    def _containers(self, ctx: ActivationContext) -> list[str]:
        services = ctx.settings.get("services") or {}
        names = []
        if "all" in services:
            names.append(services["all"])
        else:
            for region in ctx.regions:
                if region not in services:
                    raise DeployError(f"{ctx.class_}: no service configured for region {region}")
                names.append(services[region])
        return names

    def activate(self, ctx: ActivationContext) -> None:
        for name in self._containers(ctx):
            _restart(ctx, name)

    def check_health(self, ctx: ActivationContext) -> None:
        docker.wait_ready(_containers(ctx, self._containers(ctx)), float(ctx.options["health_timeout"]), ctx.settings.get("ready_urls"))


class PeliasRestore(Activator):
    """Restore the release's indices (skipped when present), then restart the services that use them. `pelias.json`
    pins the concrete index name (`schema.indexName`), so there is no alias to switch."""

    def _es(self, ctx: ActivationContext, method: str, path: str, body: dict | None = None, timeout: float = 3600,
            missing_ok: bool = False):
        url = (ctx.settings.get("es_url") or "http://localhost:39200").rstrip("/") + path
        try:
            answer = requests.request(method, url, json=body, timeout=timeout)
        except requests.RequestException as exc:
            raise DeployError(f"Elasticsearch {method} {path}: {exc}") from exc
        if answer.status_code == 404 and missing_ok:
            return None
        if not answer.ok:
            raise DeployError(f"Elasticsearch {method} {path} answered {answer.status_code}: {answer.text[:300]}")
        return answer.json() if answer.content else {}

    @staticmethod
    def indices(root: Path, tag: str, regions) -> dict[str, str]:
        """region -> index name pinned by the release's pelias.json."""
        found = {}
        for region in regions:
            try:
                found[region] = json.loads((root / "releases" / tag / region / "pelias.json").read_text())["schema"]["indexName"]
            except (OSError, ValueError, KeyError) as exc:
                raise DeployError(f"pelias {tag}/{region}: cannot read schema.indexName from pelias.json ({exc})") from exc
        return found

    def _containers(self, ctx: ActivationContext) -> list[str]:
        restart = ctx.settings.get("restart") or {}
        names = []
        if (ctx.root / "releases" / ctx.tag / "placeholder").is_dir():
            names += restart.get("shared", [])
        for region in ctx.regions:
            names += [template.format(region=region) for template in restart.get("region", [])]
        return names

    def activate(self, ctx: ActivationContext) -> None:
        for region, index in self.indices(ctx.root, ctx.tag, ctx.regions).items():
            if self._es(ctx, "HEAD", f"/{index}", missing_ok=True) is not None:
                continue  # the same index came with an earlier release (a repackaged build)
            repo = f"dm_{ctx.tag}_{region}"
            self._es(ctx, "PUT", f"/_snapshot/{repo}", {"type": "fs", "settings": {
                "location": f"{SNAPSHOT_MOUNT}/{ctx.tag}/{region}", "readonly": True}})
            answer = self._es(ctx, "POST", f"/_snapshot/{repo}/{index}/_restore?wait_for_completion=true",
                              {"indices": index, "include_global_state": False})
            if (answer.get("snapshot", {}).get("shards", {}).get("failed", 1)):
                raise DeployError(f"{region}: restoring {index} failed: {json.dumps(answer)[:300]}")
        for name in self._containers(ctx):
            _restart(ctx, name)

    def check_health(self, ctx: ActivationContext) -> None:
        for region, index in self.indices(ctx.root, ctx.tag, ctx.regions).items():
            self._es(ctx, "GET", f"/_cluster/health/{index}?wait_for_status=yellow&timeout=60s", timeout=90)
            count = int(self._es(ctx, "GET", f"/{index}/_count", timeout=120).get("count", 0))
            if count == 0:
                raise DeployError(f"{region}: index {index} is empty")
        docker.wait_ready(_containers(ctx, self._containers(ctx)), float(ctx.options["health_timeout"]), ctx.settings.get("ready_urls"))

    def release_removed(self, ctx: ActivationContext) -> None:
        """Drop the indices and snapshot repositories of a release that is removed, except an index that a release
        that stays still pins (a repackaged build has the same index name in two releases)."""
        pinned = set()
        for kept in ctx.kept:
            kept_regions = tuple(p.name for p in (ctx.root / "releases" / kept).iterdir() if (p / "pelias.json").is_file())
            pinned |= set(self.indices(ctx.root, kept, kept_regions).values())
        for region, index in self.indices(ctx.root, ctx.tag, ctx.regions).items():
            if index not in pinned:
                self._es(ctx, "DELETE", f"/{index}", missing_ok=True)
            self._es(ctx, "DELETE", f"/_snapshot/dm_{ctx.tag}_{region}", missing_ok=True)


class TilesserviceEnv(Activator):
    """tilesservice reads one `PMTILES_URL` at startup (no reload on `current.json` yet): write it into the env file that
    the compose file includes, then recreate the container (a restart does not re-read an env file).
      {"type": "tilesservice-env", "env_file": ".../layer-20/tiles-release.env", "compose_file": ".../layer-20/compose.yml",
       "service": "tilesservice", "container": "sw-dev-tilesservice", "ready_urls": {"tiles": "http://localhost:34005/..."}}"""

    @staticmethod
    def _write_env(ctx: ActivationContext) -> None:
        env_file = Path(ctx.settings["env_file"])
        url = f"s3://{ctx.options['bucket']}/releases/{ctx.tag}/tiles.pmtiles"
        tmp = env_file.with_name(env_file.name + ".tmp")
        tmp.write_text(f"PMTILES_URL={url}\n")
        tmp.replace(env_file)

    def activate(self, ctx: ActivationContext) -> None:
        self._write_env(ctx)
        docker.compose_recreate(ctx.settings["compose_file"], ctx.settings.get("service", "tilesservice"))

    def check_health(self, ctx: ActivationContext) -> None:
        container = docker.compose_container(ctx.settings["compose_file"], ctx.settings.get("service", "tilesservice"))
        docker.wait_ready([container], float(ctx.options["health_timeout"]), ctx.settings.get("ready_urls"))

    def abandon(self, ctx: ActivationContext) -> None:
        Path(ctx.settings["env_file"]).unlink(missing_ok=True)


ACTIVATORS["tilesservice-env"] = TilesserviceEnv
ACTIVATORS["compose-restart"] = ComposeRestart
ACTIVATORS["pelias-restore"] = PeliasRestore
