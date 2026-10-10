import json
from types import SimpleNamespace

import pytest

from datamanager.deploy import docker
from datamanager.deploy.activators import ComposeRestart, PeliasRestore
from datamanager.deploy.base import ActivationContext
from datamanager.deploy.compose_single_machine import ComposeSingleMachineDriver
from datamanager.errors import DeployError
from tests.test_deploy_driver import make_package


class FakeDocker:
    """Records `docker` calls; `states` maps a container name to its State."""

    def __init__(self):
        self.calls, self.states = [], {}

    def __call__(self, args, timeout=300, check=True):
        self.calls.append(args)
        if args[0] == "inspect":
            name = args[-1]
            state = self.states.get(name, {"Status": "running", "Running": True})
            return SimpleNamespace(returncode=0 if state.get("Status") != "missing" else 1, stdout=json.dumps(state), stderr="")
        if args[0] == "compose" and "ps" in args:
            return SimpleNamespace(returncode=0, stdout=f"cid-{args[-1]}\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def restarted(self):
        return [c[1] for c in self.calls if c[0] == "restart"]

    def recreated(self):
        return [c[-1] for c in self.calls if c[0] == "compose" and "--force-recreate" in c]


@pytest.fixture()
def fake_docker(monkeypatch):
    fake = FakeDocker()
    monkeypatch.setattr(docker, "run", fake)
    monkeypatch.setattr(docker, "sleep", lambda s: None)
    return fake


def _ctx(tmp_path, class_="valhalla", regions=("benelux", "france"), settings=None, kept=()):
    return ActivationContext(class_, "r-2", tmp_path, regions, settings or {}, {"health_timeout": 0.05}, kept)


# ---- compose-restart ------------------------------------------------------------------------------------------------

def test_compose_restart_restarts_the_regions_of_the_release(tmp_path, fake_docker):
    settings = {"services": {"benelux": "sw-dev-valhalla-benelux", "france": "sw-dev-valhalla-france", "germany": "sw-dev-valhalla-germany"}}
    ctx = _ctx(tmp_path, settings=settings)
    ComposeRestart().activate(ctx)
    assert fake_docker.restarted() == ["sw-dev-valhalla-benelux", "sw-dev-valhalla-france"]
    ComposeRestart().check_health(ctx)


def test_compose_restart_all_and_missing_service(tmp_path, fake_docker):
    ComposeRestart().activate(_ctx(tmp_path, "geodata", (), {"services": {"all": "sw-dev-regionservice"}}))
    assert fake_docker.restarted() == ["sw-dev-regionservice"]
    with pytest.raises(DeployError, match="no service configured for region france"):
        ComposeRestart().activate(_ctx(tmp_path, settings={"services": {"benelux": "x"}}))


def test_health_fails_fast_for_a_container_that_exited(tmp_path, fake_docker):
    fake_docker.states["sw-dev-regionservice"] = {"Status": "exited", "Running": False}
    with pytest.raises(DeployError, match="exited"):
        ComposeRestart().check_health(_ctx(tmp_path, "geodata", (), {"services": {"all": "sw-dev-regionservice"}}))


def test_health_waits_for_the_health_check_then_times_out(tmp_path, fake_docker):
    fake_docker.states["c"] = {"Status": "running", "Running": True, "Health": {"Status": "starting"}}
    with pytest.raises(DeployError, match="not ready in time: c \\(running/starting\\)"):
        docker.wait_ready(["c"], 0.05)
    fake_docker.states["c"]["Health"]["Status"] = "healthy"
    docker.wait_ready(["c"], 0.05)


# ---- pelias-restore -------------------------------------------------------------------------------------------------

class FakeEs:
    def __init__(self):
        self.indices, self.calls = {}, []
        self.restore_failed = 0

    def __call__(self, method, url, json=None, timeout=None):
        path = url.split("39200", 1)[1]
        self.calls.append((method, path))
        status, body = 200, {}
        if method == "HEAD":
            status = 200 if path.lstrip("/") in self.indices else 404
        elif method == "POST" and "_restore" in path:
            index = path.split("/")[3]
            body = {"snapshot": {"shards": {"failed": self.restore_failed, "successful": 1}}}
            if not self.restore_failed:
                self.indices[index] = 100
        elif method == "GET" and path.endswith("_count"):
            body = {"count": self.indices.get(path.split("/")[1], 0)}
        elif method == "DELETE" and not path.startswith("/_snapshot"):
            if self.indices.pop(path.lstrip("/"), None) is None:
                status = 404
        return SimpleNamespace(status_code=status, ok=status < 400, content=b"x" if body else b"", json=lambda: body, text="")


@pytest.fixture()
def fake_es(monkeypatch):
    fake = FakeEs()
    monkeypatch.setattr("datamanager.deploy.activators.requests.request", fake)
    return fake


@pytest.fixture()
def pelias_driver(tmp_path, fake_docker, fake_es):
    config = {"host": None, "classes": {"pelias": {
        "root": str(tmp_path / "pelias"), "es_snapshots": str(tmp_path / "es"),
        "activate": {"type": "pelias-restore", "es_url": "http://localhost:39200",
                     "restart": {"region": ["sw-dev-pelias-{region}-pip", "sw-dev-pelias-{region}-api"], "shared": ["sw-dev-pelias-placeholder"]}}}}}
    return ComposeSingleMachineDriver(config, {"health_timeout": 0.05})


def test_pelias_restores_the_index_registers_the_repository_and_restarts(tmp_path, pelias_driver, fake_es, fake_docker):
    result = pelias_driver.deploy_class(make_package(tmp_path, "r-1"), "pelias")
    assert result["status"] == "ok" and "pelias_benelux-1" in fake_es.indices
    methods = [c for c in fake_es.calls if c[0] in ("PUT", "POST")]
    assert methods[0] == ("PUT", "/_snapshot/dm_r-1_benelux")
    assert methods[1] == ("POST", "/_snapshot/dm_r-1_benelux/pelias_benelux-1/_restore?wait_for_completion=true")
    assert fake_docker.restarted() == ["sw-dev-pelias-placeholder", "sw-dev-pelias-benelux-pip", "sw-dev-pelias-benelux-api"]


def test_pelias_skips_the_restore_when_the_index_is_already_there(tmp_path, pelias_driver, fake_es):
    fake_es.indices["pelias_benelux-1"] = 5
    pelias_driver.deploy_class(make_package(tmp_path, "r-1"), "pelias")
    assert not [c for c in fake_es.calls if c[0] in ("PUT", "POST")]


def test_failed_restore_switches_back_and_leaves_nothing_live(tmp_path, pelias_driver, fake_es):
    fake_es.restore_failed = 1
    with pytest.raises(DeployError, match="restoring pelias_benelux-1 failed"):
        pelias_driver.deploy_class(make_package(tmp_path, "r-1"), "pelias")
    assert not (tmp_path / "pelias/current").exists()


def test_empty_index_is_not_healthy(tmp_path, pelias_driver, fake_es, monkeypatch):
    fake_es.indices["pelias_benelux-1"] = 0
    with pytest.raises(DeployError, match="is empty"):
        pelias_driver.deploy_class(make_package(tmp_path, "r-1"), "pelias")


def test_removing_a_release_drops_its_index_but_not_one_a_kept_release_still_uses(tmp_path, pelias_driver, fake_es):
    # r-1 and r-2 are the same pelias build (same index name); r-3 is a new build
    one, two = make_package(tmp_path, "r-1"), make_package(tmp_path, "r-2")
    three = make_package(tmp_path, "r-3")
    (three.path / "pelias/benelux/pelias.json").write_text('{"schema": {"indexName": "pelias_benelux-2"}}')
    from datamanager.deploy.base import PartView
    import hashlib
    data = (three.path / "pelias/benelux/pelias.json").read_bytes()
    three = type(three)(three.tag, three.path, tuple(
        PartView(p.path, p.class_, p.region, p.kind, len(data), hashlib.sha256(data).hexdigest(), p.meta) if p.path.endswith("pelias.json") else p
        for p in three.parts))
    for package in (one, two):
        pelias_driver.deploy_class(package, "pelias")
    assert "pelias_benelux-1" in fake_es.indices
    result = pelias_driver.deploy_class(three, "pelias")  # r-1 is removed, r-2 (previous) still uses pelias_benelux-1
    assert result["removed"] == ["r-1"] and "pelias_benelux-1" in fake_es.indices
    assert ("DELETE", "/_snapshot/dm_r-1_benelux") in fake_es.calls
    pelias_driver.rollback_class("pelias")  # back to r-2; r-3 is removed together with its own index
    assert "pelias_benelux-2" not in fake_es.indices and "pelias_benelux-1" in fake_es.indices


def test_unreadable_pelias_json_is_a_clear_error(tmp_path):
    with pytest.raises(DeployError, match="schema.indexName"):
        PeliasRestore.indices(tmp_path, "r-1", ("benelux",))


def test_compose_mode_creates_the_services_so_the_first_deploy_works(tmp_path, fake_docker):
    settings = {"compose_file": "/infra/layer-10/compose.yaml",
                "services": {"benelux": "valhalla-benelux", "france": "valhalla-france"}}
    ctx = _ctx(tmp_path, settings=settings)
    ComposeRestart().activate(ctx)
    assert fake_docker.recreated() == ["valhalla-benelux", "valhalla-france"] and not fake_docker.restarted()
    assert ["compose", "-f", "/infra/layer-10/compose.yaml", "up", "-d", "--no-deps", "--force-recreate", "valhalla-benelux"] in fake_docker.calls
    fake_docker.states["cid-valhalla-france"] = {"Status": "exited", "Running": False}
    with pytest.raises(DeployError, match="cid-valhalla-france is exited"):
        ComposeRestart().check_health(ctx)  # health looks at the container compose created


def test_ensure_starts_release_independent_services_without_recreating_them(tmp_path, fake_docker):
    settings = {"compose_file": "/infra/layer-10/compose.yaml", "ensure": ["pelias-libpostal"],
                "services": {"benelux": "valhalla-benelux"}}
    ComposeRestart().activate(_ctx(tmp_path, regions=("benelux",), settings=settings))
    up = [c for c in fake_docker.calls if c[0] == "compose" and "up" in c]
    assert up[0] == ["compose", "-f", "/infra/layer-10/compose.yaml", "up", "-d", "--no-deps", "pelias-libpostal"]  # first, plain up
    assert "--force-recreate" not in up[0] and fake_docker.recreated() == ["valhalla-benelux"]
    fake_docker.calls.clear()
    ComposeRestart().activate(_ctx(tmp_path, regions=("benelux",), settings={"ensure": ["x"], "services": {"benelux": "c"}}))
    assert fake_docker.restarted() == ["c"] and not [c for c in fake_docker.calls if c[0] == "compose"]  # no compose file: nothing to ensure


def _driver(tmp_path, **extra):
    config = {"host": None, "classes": {"geodata": {"root": str(tmp_path / "g"), "activate": {
        "type": "compose-restart", "compose_file": "/infra/l20.yml", "services": {"all": "regionservice"},
        "ensure_after": {"compose_file": "/infra/l20.yml", "services": ["routerservice"]}}}}, **extra}
    return ComposeSingleMachineDriver(config, {"health_timeout": 0.05})


def test_base_services_are_ensured_first_and_supporting_services_after(tmp_path, fake_docker):
    driver = _driver(tmp_path, ensure={"compose_file": "/infra/l20.yml", "services": ["authservice", "mailservice"]})
    assert driver.ensure_base() == []
    assert [c[-1] for c in fake_docker.calls if "up" in c] == ["authservice", "mailservice"]
    fake_docker.calls.clear()
    result = driver.deploy_class(make_package(tmp_path, "r-1"), "geodata")
    up = [(c[-1], "--force-recreate" in c) for c in fake_docker.calls if c[0] == "compose" and "up" in c]
    assert up == [("regionservice", True), ("routerservice", False)]  # the class first, then what depends on it, never recreated
    assert result["warnings"] == []


def test_a_supporting_service_that_does_not_start_is_a_warning_not_a_failure(tmp_path, monkeypatch, fake_docker):
    real = docker.compose_ensure

    def failing(compose_file, service):
        if service in ("mailservice", "routerservice"):
            raise DeployError("no such image")
        real(compose_file, service)
    monkeypatch.setattr(docker, "compose_ensure", failing)
    driver = _driver(tmp_path, ensure={"compose_file": "/infra/l20.yml", "services": ["authservice", "mailservice"]})
    assert driver.ensure_base() == ["mailservice did not start: no such image"]
    result = driver.deploy_class(make_package(tmp_path, "r-1"), "geodata")
    assert result["status"] == "ok" and result["warnings"] == ["routerservice did not start: no such image"]
    assert (tmp_path / "g/current").is_symlink()


def test_ensure_blocks_are_validated():
    bad = {"ensure": {"services": ["a"]}, "classes": {"geodata": {"root": "/g", "activate": {"type": "none", "ensure_after": ["x"]}}}}
    text = " | ".join(ComposeSingleMachineDriver.validate_config(bad))
    assert "ensure:" in text and "ensure_after" in text


def test_unpacked_snapshots_are_removed_once_the_release_is_live(tmp_path, pelias_driver, fake_es):
    result = pelias_driver.deploy_class(make_package(tmp_path, "r-1"), "pelias")
    assert result["status"] == "ok" and result["warnings"] == []
    assert "pelias_benelux-1" in fake_es.indices  # the index lives in Elasticsearch now
    assert not (tmp_path / "es/r-1").exists()  # the ~165 GB of unpacked snapshot are not needed any more
    assert (tmp_path / "pelias/releases/r-1/benelux/pelias.json").is_file()  # the release itself stays
    assert ("DELETE", "/_snapshot/dm_r-1_benelux") in fake_es.calls


def test_a_gone_snapshot_with_a_gone_index_gives_a_clear_error_and_switches_back(tmp_path, pelias_driver, fake_es):
    one, two = make_package(tmp_path, "r-1", "1"), make_package(tmp_path, "r-2", "2")
    pelias_driver.deploy_class(one, "pelias")
    pelias_driver.deploy_class(two, "pelias")
    fake_es.indices.pop("pelias_benelux-1")  # r-1 is still on the target as previous, but its index was dropped by hand
    with pytest.raises(DeployError, match="unpacked snapshot .* is gone"):
        pelias_driver.deploy_class(one, "pelias")
    state = pelias_driver.describe_state(["pelias"])["pelias"]
    assert (state["current"], state["previous"]) == ("r-2", "r-1")  # switched back to what was live


def test_a_failing_cleanup_after_activation_is_a_warning(tmp_path, pelias_driver, fake_es, monkeypatch):
    real = fake_es.__call__

    def flaky(method, url, json=None, timeout=None):
        if method == "DELETE" and "_snapshot" in url and "dm_r-1_" in url:
            raise OSError("elasticsearch hiccup")
        return real(method, url, json=json, timeout=timeout)
    monkeypatch.setattr("datamanager.deploy.activators.requests.request", flaky)
    result = pelias_driver.deploy_class(make_package(tmp_path, "r-1"), "pelias")
    assert result["status"] == "ok" and "cleanup after activation failed" in result["warnings"][0]


def test_redeploying_the_previous_release_needs_no_snapshot_while_its_index_is_there(tmp_path, pelias_driver, fake_es):
    one, two = make_package(tmp_path, "r-1", "1"), make_package(tmp_path, "r-2", "2")
    pelias_driver.deploy_class(one, "pelias")
    pelias_driver.deploy_class(two, "pelias")
    result = pelias_driver.deploy_class(one, "pelias")  # the snapshots of r-1 are long gone, its index is not
    assert result["status"] == "ok" and result["current"] == "r-1" and result["previous"] == "r-2"


def test_deploying_the_current_release_again_still_starts_the_supporting_services(tmp_path, fake_docker):
    driver = _driver(tmp_path)
    package = make_package(tmp_path, "r-1")
    driver.deploy_class(package, "geodata")
    fake_docker.calls.clear()
    result = driver.deploy_class(package, "geodata")
    assert result["status"] == "skipped" and result["warnings"] == []
    assert [c[-1] for c in fake_docker.calls if "up" in c] == ["routerservice"]  # not recreated, nothing was copied
