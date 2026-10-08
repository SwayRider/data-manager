import gzip
import hashlib
import io
import os
import tarfile
from pathlib import Path

import pytest

from datamanager.deploy import base
from datamanager.deploy.base import Activator, PackageView, PartView
from datamanager.deploy.compose_single_machine import ComposeSingleMachineDriver, Target, target_of
from datamanager.errors import DeployError
from datamanager.models import Package, PackageItem


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _tar(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def make_package(tmp_path: Path, tag: str, version: str = "1") -> PackageView:
    """A small package with every kind of part the driver maps."""
    folder = tmp_path / "repo" / tag
    parts = []

    def add(class_, region, path, data, kind="file"):
        file = folder / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(data)
        parts.append(PartView(path, class_, region, kind, len(data), _sha(data)))

    add("geodata", None, "geodata/manifest.yml", f"tag: {tag}\n".encode(), kind="manifest")
    add("geodata", None, "geodata/contours/benelux-core.geojson", f"core{version}".encode())
    add("geodata", None, "geodata/border-crossings/benelux-france.csv", b"osm_id\n")
    add("valhalla", "benelux", "valhalla/benelux/valhalla_tiles.tar", f"tiles{version}".encode())
    add("valhalla", "benelux", "valhalla/benelux/admin.sqlite", b"admin")
    add("pelias", "benelux", "pelias/benelux/pelias.json", b'{"schema": {"indexName": "pelias_benelux-1"}}')
    add("pelias", "benelux", "pelias/benelux/interpolation/street.db", b"street")
    add("pelias", "benelux", "pelias/benelux/wof.tar.gz", _tar({"whosonfirst-data-admin-be.db": b"wof"}))
    add("pelias", "benelux", "pelias/benelux/benelux.es-snapshot.tar", _tar({"index-0": b"snap" + version.encode()}))
    add("pelias", None, "pelias/placeholder/store.sqlite3.gz", gzip.compress(b"placeholder-store"))
    return PackageView(tag, folder, tuple(parts))


@pytest.fixture()
def config(tmp_path):
    return {"driver": "compose-single-machine", "host": None, "activation_order": ["geodata", "valhalla", "pelias"],
            "classes": {"geodata": {"root": str(tmp_path / "geodata")},
                        "valhalla": {"root": str(tmp_path / "valhalla")},
                        "pelias": {"root": str(tmp_path / "pelias"), "es_snapshots": str(tmp_path / "es")}}}


@pytest.fixture()
def driver(config):
    return ComposeSingleMachineDriver(config)


class Recorder(Activator):
    calls: list = []
    fail_on: set = set()
    unhealthy: set = set()

    def activate(self, ctx):
        Recorder.calls.append(("activate", ctx.class_, ctx.tag, ctx.regions))
        if ctx.tag in Recorder.fail_on:
            raise DeployError("container did not start")

    def check_health(self, ctx):
        Recorder.calls.append(("health", ctx.class_, ctx.tag))
        if ctx.tag in Recorder.unhealthy:
            raise DeployError("not healthy")

    def release_removed(self, ctx):
        Recorder.calls.append(("removed", ctx.class_, ctx.tag))


@pytest.fixture()
def recorder(monkeypatch, config):
    Recorder.calls, Recorder.fail_on, Recorder.unhealthy = [], set(), set()
    monkeypatch.setitem(base.ACTIVATORS, "recorder", Recorder)
    for block in config["classes"].values():
        block["activate"] = {"type": "recorder"}
    return Recorder


# ---- configuration and mapping --------------------------------------------------------------------------------------

def test_validate_config_reports_problems(config):
    assert ComposeSingleMachineDriver.validate_config(config) == []
    bad = {"host": {"ssh": "x"}, "classes": {"pelias": {"root": "relative"}, "foo": {"root": "/x"},
                                              "geodata": {"root": "/g", "activate": {"type": "nope"}}},
           "activation_order": ["valhalla"]}
    problems = ComposeSingleMachineDriver.validate_config(bad)
    text = " | ".join(problems)
    for expected in ("host", "classes.pelias.root", "classes.pelias.es_snapshots", "classes.foo", "activate.type", "activation_order"):
        assert expected in text
    with pytest.raises(DeployError):
        ComposeSingleMachineDriver({"classes": {}})


@pytest.mark.parametrize("class_,path,kind,dest", [
    ("geodata", "geodata/contours/a.geojson", "file", "contours/a.geojson"),
    ("geodata", "geodata/manifest.yml", "file", "manifest.yml"),
    ("valhalla", "valhalla/benelux/admin.sqlite", "file", "benelux/admin.sqlite"),
    ("pelias", "pelias/benelux/wof.tar.gz", "wof", "benelux/wof/sqlite"),
    ("pelias", "pelias/benelux/benelux.es-snapshot.tar", "snapshot", "benelux"),
    ("pelias", "pelias/placeholder/store.sqlite3.gz", "gunzip", "placeholder/data/store.sqlite3"),
    ("pelias", "pelias/benelux/interpolation/street.db", "file", "benelux/interpolation/street.db"),
])
def test_target_layout(class_, path, kind, dest):
    assert target_of(class_, PartView(path, class_, None, "file", 1, "x")) == Target(kind, dest)


def test_package_view_from_model(db_session):
    package = Package(tag="r-1", path="/tmp/r-1", status="complete")
    package.items.append(PackageItem(class_="geodata", path="geodata/manifest.yml", kind="manifest", size_bytes=3,
                                     sha256="ab", meta_json={"generated": True}))
    db_session.add(package)
    db_session.commit()
    view = PackageView.from_package(package)
    assert view.tag == "r-1" and view.classes == ["geodata"] and view.parts[0].meta == {"generated": True}


# ---- deploy ---------------------------------------------------------------------------------------------------------

def test_deploy_copies_maps_and_switches_with_relative_symlink(tmp_path, driver, recorder):
    package = make_package(tmp_path, "r-1")
    steps = []
    result = driver.deploy_class(package, "pelias", step=steps.append)
    root = tmp_path / "pelias"
    assert result["status"] == "ok" and result["current"] == "r-1" and result["previous"] is None
    assert os.readlink(root / "current") == "releases/r-1" and not (root / "previous").exists()
    release = root / "releases" / "r-1"
    assert (release / "benelux/pelias.json").is_file() and (release / "benelux/interpolation/street.db").read_bytes() == b"street"
    assert (release / "benelux/wof/sqlite/whosonfirst-data-admin-be.db").read_bytes() == b"wof"
    assert (release / "placeholder/data/store.sqlite3").read_bytes() == b"placeholder-store"
    assert (tmp_path / "es/r-1/benelux/index-0").read_bytes() == b"snap1"
    assert not (release / "benelux/benelux.es-snapshot.tar").exists()
    assert not list((root / "releases").glob("*.partial"))
    assert recorder.calls[0] == ("activate", "pelias", "r-1", ("benelux",))
    assert [c[0] for c in recorder.calls] == ["activate", "health"]
    assert any(s.startswith("Copy ") for s in steps) and "Activate pelias" in steps


def test_manifest_is_the_last_file_written(tmp_path, driver):
    package = make_package(tmp_path, "r-1")
    steps = []
    driver.deploy_class(package, "geodata", step=steps.append)
    copies = [s for s in steps if s.startswith("Copy ")]
    assert copies[-1] == "Copy geodata/manifest.yml"


def test_only_current_and_previous_are_kept(tmp_path, driver, recorder):
    for n in (1, 2, 3):
        result = driver.deploy_class(make_package(tmp_path, f"r-{n}", str(n)), "pelias")
    root = tmp_path / "pelias"
    assert result["previous"] == "r-2" and result["removed"] == ["r-1"]
    assert sorted(p.name for p in (root / "releases").iterdir()) == ["r-2", "r-3"]
    assert os.readlink(root / "previous") == "releases/r-2"
    assert not (tmp_path / "es/r-1").exists() and (tmp_path / "es/r-2").exists()
    assert ("removed", "pelias", "r-1") in recorder.calls
    state = driver.describe_state(["pelias"])["pelias"]
    assert (state["current"], state["previous"], state["releases"]) == ("r-3", "r-2", ["r-2", "r-3"])


def test_deploying_the_current_release_again_is_skipped(tmp_path, driver):
    package = make_package(tmp_path, "r-1")
    driver.deploy_class(package, "geodata")
    assert driver.deploy_class(package, "geodata")["status"] == "skipped"


def test_redeploying_the_previous_release_swaps_without_copying(tmp_path, driver, recorder):
    one, two = make_package(tmp_path, "r-1", "1"), make_package(tmp_path, "r-2", "2")
    driver.deploy_class(one, "valhalla")
    driver.deploy_class(two, "valhalla")
    (one.path / "valhalla/benelux/valhalla_tiles.tar").unlink()  # not needed any more: r-1 is still on the target
    steps = []
    plan = driver.plan_class(one, "valhalla")
    assert plan["problems"]  # the package file is gone, so the plan refuses
    one.path.joinpath("valhalla/benelux/valhalla_tiles.tar").write_bytes(b"tiles1")
    result = driver.deploy_class(one, "valhalla", step=steps.append)
    assert result["current"] == "r-1" and result["previous"] == "r-2" and not [s for s in steps if s.startswith("Copy")]


# ---- failures ---------------------------------------------------------------------------------------------------------

def test_corrupt_source_aborts_before_anything_is_switched(tmp_path, driver):
    package = make_package(tmp_path, "r-1")
    (package.path / "geodata/contours/benelux-core.geojson").write_bytes(b"CORRUPT!")
    with pytest.raises(DeployError, match="sha256"):
        driver.deploy_class(package, "geodata")
    root = tmp_path / "geodata"
    assert not (root / "current").exists() and not (root / "releases/r-1").exists()


def test_copy_resumes_after_an_interruption(tmp_path, driver, monkeypatch):
    package = make_package(tmp_path, "r-1")
    real = ComposeSingleMachineDriver._copy_part
    copied = []

    def flaky(self, source, part, target, partial, tag, on_bytes):
        if len(copied) == 1:
            raise OSError("disk went away")
        copied.append(part.path)
        return real(self, source, part, target, partial, tag, on_bytes)

    monkeypatch.setattr(ComposeSingleMachineDriver, "_copy_part", flaky)
    with pytest.raises(DeployError, match="disk went away"):
        driver.deploy_class(package, "geodata")
    assert (tmp_path / "geodata/releases/r-1.partial").is_dir()
    monkeypatch.setattr(ComposeSingleMachineDriver, "_copy_part", real)
    second = []
    driver.deploy_class(package, "geodata", step=second.append)
    assert len([s for s in second if s.startswith("Copy ")]) == 2  # three parts, one was already done
    assert (tmp_path / "geodata/current/manifest.yml").is_file()


def test_full_verify_catches_a_corrupted_copy(tmp_path, driver):
    package = make_package(tmp_path, "r-1")
    partial = tmp_path / "geodata/releases/r-1.partial"
    driver._transfer(package, "geodata", partial, None, None)
    (partial / "contours/benelux-core.geojson").write_bytes(b"core1")  # same size, still fine
    driver._verify(package, "geodata", partial, None, None)
    (partial / "contours/benelux-core.geojson").write_bytes(b"XXXX1")  # same size, other content
    with pytest.raises(DeployError, match="sha256 mismatch"):
        driver._verify(package, "geodata", partial, None, None)
    driver.options["verify"] = "size"
    driver._verify(package, "geodata", partial, None, None)  # size mode does not read the files


def test_not_enough_free_space_refuses_the_deploy(tmp_path, driver, monkeypatch):
    import shutil
    from collections import namedtuple
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(shutil, "disk_usage", lambda p: usage(100, 99, 1))
    with pytest.raises(DeployError, match="not enough free space"):
        driver.deploy_class(make_package(tmp_path, "r-1"), "valhalla")
    assert not (tmp_path / "valhalla/current").exists()


def test_failed_activation_switches_back(tmp_path, driver, recorder):
    driver.deploy_class(make_package(tmp_path, "r-1", "1"), "valhalla")
    recorder.unhealthy = {"r-2"}
    with pytest.raises(DeployError, match="switched back to r-1"):
        driver.deploy_class(make_package(tmp_path, "r-2", "2"), "valhalla")
    root = tmp_path / "valhalla"
    assert os.readlink(root / "current") == "releases/r-1" and not (root / "previous").exists()
    # r-2 was activated and found unhealthy; then the services were brought back on r-1
    assert [c[:3] for c in recorder.calls[-4:]] == [("activate", "valhalla", "r-2"), ("health", "valhalla", "r-2"),
                                                    ("activate", "valhalla", "r-1"), ("health", "valhalla", "r-1")]
    # the failed release is not removed: a re-run can resume or the operator can look at it
    assert (root / "releases/r-2").is_dir()


def test_failed_first_activation_leaves_nothing_live(tmp_path, driver, recorder):
    recorder.fail_on = {"r-1"}
    with pytest.raises(DeployError, match="nothing was live before"):
        driver.deploy_class(make_package(tmp_path, "r-1"), "geodata")
    assert not (tmp_path / "geodata/current").exists()


def test_current_that_is_a_real_directory_is_never_replaced(tmp_path, driver):
    (tmp_path / "geodata/current").mkdir(parents=True)
    with pytest.raises(DeployError, match="not a symlink"):
        driver.deploy_class(make_package(tmp_path, "r-1"), "geodata")


# ---- rollback ---------------------------------------------------------------------------------------------------------

def test_rollback_goes_back_and_removes_the_rolled_back_release(tmp_path, driver, recorder):
    driver.deploy_class(make_package(tmp_path, "r-1", "1"), "pelias")
    driver.deploy_class(make_package(tmp_path, "r-2", "2"), "pelias")
    result = driver.rollback_class("pelias")
    root = tmp_path / "pelias"
    assert result["status"] == "rolled_back" and result["current"] == "r-1" and result["rolled_back"] == "r-2"
    assert os.readlink(root / "current") == "releases/r-1" and not (root / "previous").exists()
    assert sorted(p.name for p in (root / "releases").iterdir()) == ["r-1"]
    assert not (tmp_path / "es/r-2").exists()
    with pytest.raises(DeployError, match="no previous release"):
        driver.rollback_class("pelias")


def test_failed_rollback_keeps_the_current_release(tmp_path, driver, recorder):
    driver.deploy_class(make_package(tmp_path, "r-1", "1"), "valhalla")
    driver.deploy_class(make_package(tmp_path, "r-2", "2"), "valhalla")
    recorder.unhealthy = {"r-1"}
    with pytest.raises(DeployError, match="r-2 is current again"):
        driver.rollback_class("valhalla")
    root = tmp_path / "valhalla"
    assert os.readlink(root / "current") == "releases/r-2" and os.readlink(root / "previous") == "releases/r-1"


def test_prune_dry_run_and_leftover_partials(tmp_path, driver):
    driver.deploy_class(make_package(tmp_path, "r-1"), "geodata")
    (tmp_path / "geodata/releases/r-0.partial").mkdir()
    (tmp_path / "geodata/releases/r-old").mkdir()
    assert driver.prune_class("geodata", dry_run=True) == ["r-old", "r-0.partial"]
    assert (tmp_path / "geodata/releases/r-old").exists()
    driver.prune_class("geodata")
    assert sorted(p.name for p in (tmp_path / "geodata/releases").iterdir()) == ["r-1"]
