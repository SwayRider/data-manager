import json
from types import SimpleNamespace

import pytest

from datamanager.deploy import docker, s3_tiles
from datamanager.deploy.compose_single_machine import ComposeSingleMachineDriver
from datamanager.deploy.s3_tiles import validate_tiles_block
from datamanager.errors import DeployError
from tests.test_deploy_driver import make_package


class NoSuchKey(Exception):
    response = {"Error": {"Code": "404"}}


class FakeS3:
    """The part of the S3 API the transport uses, in memory. Listing is paged two entries at a time."""

    def __init__(self):
        self.objects, self.puts, self.uploads, self.aborted = {}, [], {}, 0

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise NoSuchKey()
        body, meta = self.objects[Key]
        return {"ContentLength": len(body), "Metadata": meta}

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise NoSuchKey()
        return {"Body": SimpleNamespace(read=lambda: self.objects[Key][0])}

    def put_object(self, Bucket, Key, Body, Metadata=None, ContentType=None):
        self.objects[Key] = (Body, Metadata or {})
        self.puts.append(Key)

    def delete_object(self, Bucket, Key):
        self.objects.pop(Key, None)

    def delete_objects(self, Bucket, Delete):
        for o in Delete["Objects"]:
            self.objects.pop(o["Key"], None)

    def create_multipart_upload(self, Bucket, Key, Metadata=None):
        self.uploads[Key] = {"meta": Metadata or {}, "parts": {}}
        return {"UploadId": Key}

    def upload_part(self, Bucket, Key, UploadId, PartNumber, Body):
        self.uploads[Key]["parts"][PartNumber] = Body
        return {"ETag": f"e{PartNumber}"}

    def complete_multipart_upload(self, Bucket, Key, UploadId, MultipartUpload):
        up = self.uploads.pop(Key)
        self.objects[Key] = (b"".join(up["parts"][p["PartNumber"]] for p in MultipartUpload["Parts"]), up["meta"])
        self.puts.append(Key)

    def abort_multipart_upload(self, Bucket, Key, UploadId):
        self.uploads.pop(Key, None)
        self.aborted += 1

    def list_objects_v2(self, Bucket, Prefix="", Delimiter=None, ContinuationToken=None):
        keys = sorted(k for k in self.objects if k.startswith(Prefix))
        if Delimiter:
            prefixes = sorted({Prefix + k[len(Prefix):].split(Delimiter)[0] + Delimiter for k in keys if Delimiter in k[len(Prefix):]})
            start = int(ContinuationToken or 0)
            page = prefixes[start:start + 2]
            out = {"CommonPrefixes": [{"Prefix": p} for p in page]}
            if start + 2 < len(prefixes):
                out["NextContinuationToken"] = str(start + 2)
            return out
        if ContinuationToken:  # like S3, the token is a position in the key order, not an offset: deleting while paging is safe
            keys = [k for k in keys if k > ContinuationToken]
        out = {"Contents": [{"Key": k} for k in keys[:2]]}
        if len(keys) > 2:
            out["NextContinuationToken"] = keys[1]
        return out


@pytest.fixture()
def s3(monkeypatch):
    fake = FakeS3()
    monkeypatch.setattr(s3_tiles, "make_client", lambda block: fake)
    monkeypatch.setattr(s3_tiles, "PART_SIZE", 64)
    monkeypatch.setattr(s3_tiles, "SINGLE_PUT_MAX", 100)
    return fake


@pytest.fixture()
def docker_calls(monkeypatch):
    class Calls(list):
        state: dict

    calls, state = Calls(), {"Status": "running", "Running": True}

    def fake(args, timeout=300, check=True):
        calls.append(args)
        if args[0] == "inspect":
            return SimpleNamespace(returncode=0, stdout=json.dumps(state), stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(docker, "run", fake)
    monkeypatch.setattr(docker, "sleep", lambda s: None)
    calls.state = state
    return calls


@pytest.fixture()
def driver(tmp_path, s3, docker_calls):
    block = {"transport": "s3", "endpoint": "http://127.0.0.1:39000", "bucket": "swayrider-tiles", "region": "garage",
             "credentials": {"access_key_env": "DM_S3_ACCESS_KEY", "secret_key_env": "DM_S3_SECRET_KEY"},
             "activate": {"type": "tilesservice-env", "env_file": str(tmp_path / "tiles-release.env"),
                          "compose_file": str(tmp_path / "compose.yml"), "service": "tilesservice", "container": "sw-dev-tilesservice"}}
    return ComposeSingleMachineDriver({"host": None, "classes": {"tiles": block}}, {"health_timeout": 0.05})


def _env(tmp_path):
    f = tmp_path / "tiles-release.env"
    return f.read_text().strip() if f.exists() else None


def test_validate_tiles_block():
    assert validate_tiles_block({"transport": "s3", "endpoint": "http://x", "bucket": "b",
                                 "credentials": {"access_key_env": "A", "secret_key_env": "B"}}) == []
    text = " | ".join(validate_tiles_block({"transport": "rsync", "endpoint": "x", "activate": {"type": "nope"}}))
    for expected in ("transport", "endpoint", "bucket", "access_key_env", "activate.type"):
        assert expected in text


def test_first_deploy_uploads_the_release_then_points_at_it(tmp_path, driver, s3, docker_calls):
    package = make_package(tmp_path, "r-1")
    result = driver.deploy_class(package, "tiles")
    assert result["status"] == "ok" and result["previous"] is None
    assert set(k for k in s3.objects) == {"releases/r-1/tiles.pmtiles", "releases/r-1/styles/swayrider/1/light.json",
                                          "releases/r-1/manifest.json", "current.json"}
    pointer = json.loads(s3.objects["current.json"][0])
    assert pointer["schema"] == 1 and pointer["release"] == "r-1" and pointer["prefix"] == "releases/r-1/"
    assert s3.puts[-2:] == ["releases/r-1/manifest.json", "current.json"]  # the manifest is the last object, the pointer after it
    body, meta = s3.objects["releases/r-1/tiles.pmtiles"]
    assert len(body) > 100 and meta["sha256"]  # multipart with the checksum recorded
    assert _env(tmp_path) == "PMTILES_URL=s3://swayrider-tiles/releases/r-1/tiles.pmtiles"
    assert ["compose", "-f", str(tmp_path / "compose.yml"), "up", "-d", "--no-deps", "--force-recreate", "tilesservice"] in docker_calls
    assert driver.describe_state(["tiles"])["tiles"]["current"] == "r-1"


def test_only_current_and_previous_stay_in_the_store(tmp_path, driver, s3):
    for n in (1, 2, 3):
        result = driver.deploy_class(make_package(tmp_path, f"r-{n}", str(n)), "tiles")
    state = driver.describe_state(["tiles"])["tiles"]
    assert (state["current"], state["previous"], state["releases"]) == ("r-3", "r-2", ["r-2", "r-3"])
    assert result["removed"] == ["r-1"] and not [k for k in s3.objects if k.startswith("releases/r-1/")]
    assert _env(tmp_path).endswith("r-3/tiles.pmtiles")


def test_identical_objects_are_not_uploaded_again(tmp_path, driver, s3):
    one, two = make_package(tmp_path, "r-1", "1"), make_package(tmp_path, "r-2", "2")
    driver.deploy_class(one, "tiles")
    driver.deploy_class(two, "tiles")
    s3.puts.clear()
    result = driver.deploy_class(one, "tiles")  # r-1 is still in the store as the previous release
    assert result["current"] == "r-1" and result["previous"] == "r-2"
    assert s3.puts == ["previous.json", "current.json"]  # nothing but the pointers


def test_deploying_the_current_release_again_is_skipped(tmp_path, driver):
    package = make_package(tmp_path, "r-1")
    driver.deploy_class(package, "tiles")
    assert driver.deploy_class(package, "tiles")["status"] == "skipped"


def test_corrupt_source_aborts_the_upload_and_never_points_at_it(tmp_path, driver, s3):
    package = make_package(tmp_path, "r-1")
    (package.path / "tiles/tiles.pmtiles").write_bytes(b"X" * 400)
    with pytest.raises(DeployError, match="sha256"):
        driver.deploy_class(package, "tiles")
    assert s3.aborted == 1 and "current.json" not in s3.objects and "releases/r-1/tiles.pmtiles" not in s3.objects


def test_unhealthy_tilesservice_switches_the_pointer_and_the_env_back(tmp_path, driver, s3, docker_calls):
    driver.deploy_class(make_package(tmp_path, "r-1", "1"), "tiles")
    docker_calls.state.update({"Status": "exited", "Running": False})
    with pytest.raises(DeployError, match="exited"):
        driver.deploy_class(make_package(tmp_path, "r-2", "2"), "tiles")
    assert json.loads(s3.objects["current.json"][0])["release"] == "r-1" and "previous.json" not in s3.objects
    assert _env(tmp_path) == "PMTILES_URL=s3://swayrider-tiles/releases/r-1/tiles.pmtiles"
    assert len([c for c in docker_calls if c[0] == "compose"]) == 3  # r-1, r-2, back to r-1


def test_failed_first_activation_removes_the_env_file(tmp_path, driver, s3, docker_calls):
    docker_calls.state.update({"Status": "exited", "Running": False})
    with pytest.raises(DeployError, match="nothing was live before"):
        driver.deploy_class(make_package(tmp_path, "r-1"), "tiles")
    assert "current.json" not in s3.objects and _env(tmp_path) is None


def test_rollback_goes_back_and_removes_the_rolled_back_release(tmp_path, driver, s3):
    driver.deploy_class(make_package(tmp_path, "r-1", "1"), "tiles")
    driver.deploy_class(make_package(tmp_path, "r-2", "2"), "tiles")
    result = driver.rollback_class("tiles")
    assert result["current"] == "r-1" and result["rolled_back"] == "r-2"
    assert json.loads(s3.objects["current.json"][0])["release"] == "r-1" and "previous.json" not in s3.objects
    assert not [k for k in s3.objects if k.startswith("releases/r-2/")]
    assert _env(tmp_path).endswith("releases/r-1/tiles.pmtiles")
    with pytest.raises(DeployError, match="no previous release"):
        driver.rollback_class("tiles")


def test_missing_credentials_are_reported_by_name_not_value(tmp_path, monkeypatch):
    monkeypatch.delenv("DM_S3_ACCESS_KEY", raising=False)
    monkeypatch.delenv("DM_S3_SECRET_KEY", raising=False)
    block = {"transport": "s3", "endpoint": "http://127.0.0.1:39000", "bucket": "b",
             "credentials": {"access_key_env": "DM_S3_ACCESS_KEY", "secret_key_env": "DM_S3_SECRET_KEY"}}
    with pytest.raises(DeployError, match="DM_S3_ACCESS_KEY"):
        s3_tiles.make_client(block)


def test_plan_reports_missing_package_files_and_bytes_to_copy(tmp_path, driver):
    package = make_package(tmp_path, "r-1")
    plan = driver.plan_class(package, "tiles")
    assert plan["bytes_to_copy"] == plan["bytes"] and not plan["problems"]
    (package.path / "tiles/manifest.json").unlink()
    assert any("manifest.json: missing" in p for p in driver.plan_class(package, "tiles")["problems"])
