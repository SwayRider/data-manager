import datetime
import json
import shutil
from collections import namedtuple
from pathlib import Path

import pytest

from datamanager.config import config as app_config
from datamanager.errors import PackageError
from datamanager.models import Asset, ConfigProfile, DownloadRecord, Package, PackageLabel, Region
from datamanager.services import assets as asset_service
from datamanager.services import packages

STYLE_META = lambda name: {"style_id": "swayrider", "style_label": "SwayRider", "version": 3, "mode": name.removeprefix("style-")}  # noqa: E731
SNAPSHOT_META = {"index_name": "pelias_benelux-29", "snapshot_name": "pelias_benelux-29", "snapshot_repository": "pelias_repo", "docs": 12}
Env = namedtuple("Env", "session config_id repo")


def _asset(session, config_id, asset_type, name, rel, content=b"x", status="approved", meta=None):
    file = Path(app_config.DATA_ROOT) / rel
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_bytes(content)
    asset = Asset(asset_type=asset_type, name=name, config_profile_id=config_id, path=rel,
                  content_hash=asset_service.sha256_of(file), size_bytes=len(content), status=status, meta_json=meta or {})
    session.add(asset)
    session.commit()
    return asset


@pytest.fixture()
def env(db_session, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    monkeypatch.setattr(app_config, "PACKAGE_ROOT", str(repo))
    cfg = ConfigProfile(name="dev-mini")
    db_session.add(cfg)
    db_session.flush()
    db_session.add(Region(config_profile_id=cfg.id, name="benelux", color="#ff0000"))
    db_session.commit()
    cid, a = cfg.id, "library/assets"
    for t, n, f in [("valhalla-tiles", "benelux", "valhalla/9/benelux/tiles.tar"),
                    ("valhalla-admin", "benelux", "valhalla/9/benelux/admin.sqlite"),
                    ("valhalla-timezones", "benelux", "valhalla/9/benelux/tz_world.sqlite"),
                    ("pelias-index-snapshot", "benelux", "pelias/29/benelux/benelux.es-snapshot.tar"),
                    ("pelias-config", "benelux", "pelias/29/benelux/pelias.json"),
                    ("pelias-wof", "benelux", "pelias/29/benelux/wof.tar.gz"),
                    ("pelias-interpolation-street-db", "benelux", "pelias-interpolation/25/benelux/street.db"),
                    ("pelias-interpolation-address-db", "benelux", "pelias-interpolation/25/benelux/address.db"),
                    ("region-outline", "benelux-core", "border/8/benelux-core.geojson"),
                    ("region-outline", "benelux-extended", "border/8/benelux-extended.geojson"),
                    ("style", "style-light", "styles/6/style-light.json"),
                    ("style", "style-dark", "styles/6/style-dark.json")]:
        _asset(db_session, cid, t, n, f"{a}/{f}", content=f"{t}:{n}".encode(),
               meta=STYLE_META(n) if t == "style" else SNAPSHOT_META if t == "pelias-index-snapshot" else None)
    pm = Path(app_config.DATA_ROOT) / "downloads/tiles/planet/20261004T000000Z/20261004.pmtiles"
    pm.parent.mkdir(parents=True)
    pm.write_bytes(b"PMTiles-fixture")
    db_session.add(DownloadRecord(
        source_key="tiles:planet", version_label="20261004T000000Z", url="http://x", filename=pm.name,
        local_path=str(pm.relative_to(app_config.DATA_ROOT)), size_bytes=pm.stat().st_size,
        content_hash=asset_service.sha256_of(pm), fetched_at=datetime.datetime(2026, 10, 4), status="approved"))
    ph = Path(app_config.DATA_ROOT) / "downloads/placeholder/store/20261005T165647Z/store.sqlite3.gz"
    ph.parent.mkdir(parents=True)
    ph.write_bytes(b"placeholder-store")
    db_session.add(DownloadRecord(
        source_key="placeholder:store", version_label="20261005T165647Z", url="http://x", filename=ph.name,
        local_path=str(ph.relative_to(app_config.DATA_ROOT)), size_bytes=ph.stat().st_size,
        content_hash=asset_service.sha256_of(ph), fetched_at=datetime.datetime(2026, 10, 5), status="approved"))
    db_session.commit()
    return Env(db_session, cid, repo)


def test_create_package_copies_hashes_and_tags(env):
    pkg = packages.create_package(env.session, env.config_id, labels={"candidate": ""}, note="first", created_by="tester")
    assert pkg.status == "complete" and pkg.tag.startswith("r-") and pkg.tag.endswith("-1")
    folder = env.repo / pkg.tag
    doc = json.loads((folder / "package.json").read_text())
    assert doc["config"]["name"] == "dev-mini" and doc["regions"] == ["benelux"]
    assert set(doc["classes"]) == {"tiles", "valhalla", "pelias", "geodata"}
    assert (folder / "valhalla/benelux/valhalla_tiles.tar").read_bytes() == b"valhalla-tiles:benelux"
    assert (folder / "tiles/tiles.pmtiles").read_bytes() == b"PMTiles-fixture"
    assert not list(env.repo.glob("*.partial"))
    assert packages.verify_package(env.session, pkg.tag) == []
    labels = {(l.key, l.origin) for l in pkg.labels}
    assert ("config", "auto") in labels and ("candidate", "user") in labels
    assert pkg.size_bytes == sum(i.size_bytes for i in pkg.items)
    assert packages.create_package(env.session, env.config_id).tag.endswith("-2")


def test_subset_of_classes(env):
    pkg = packages.create_package(env.session, env.config_id, classes=["valhalla"])
    assert {i.class_ for i in pkg.items} == {"valhalla"}


def test_missing_or_unapproved_inputs_are_refused(env):
    env.session.query(Asset).filter_by(asset_type="valhalla-admin").update({"status": "produced"})
    env.session.commit()
    with pytest.raises(PackageError, match="valhalla-admin"):
        packages.create_package(env.session, env.config_id)
    assert not env.repo.exists() or not list(env.repo.iterdir())  # nothing half-written


def test_purged_source_file_is_reported(env):
    (Path(app_config.DATA_ROOT) / "library/assets/valhalla/9/benelux/tiles.tar").unlink()
    with pytest.raises(PackageError, match="is gone"):
        packages.create_package(env.session, env.config_id, classes=["valhalla"])


def test_source_changed_during_packaging_fails_cleanly(env):
    (Path(app_config.DATA_ROOT) / "library/assets/valhalla/9/benelux/tiles.tar").write_bytes(b"tampered")
    with pytest.raises(PackageError, match="source changed"):
        packages.create_package(env.session, env.config_id, classes=["valhalla"])
    assert not list(env.repo.glob("*.partial"))
    assert env.session.query(Package).one().status == "failed"


def test_verify_detects_tamper_and_extra_files(env):
    pkg = packages.create_package(env.session, env.config_id, classes=["valhalla"])
    folder = env.repo / pkg.tag
    (folder / "valhalla/benelux/admin.sqlite").write_bytes(b"corrupt!")
    (folder / "stray.txt").write_text("x")
    problems = packages.verify_package(env.session, pkg.tag)
    assert any("admin.sqlite" in p for p in problems) and any("stray.txt" in p for p in problems)


def test_free_space_check(env, monkeypatch):
    monkeypatch.setattr(shutil, "disk_usage", lambda p: shutil._ntuple_diskusage(100, 100, 0))
    with pytest.raises(PackageError, match="Not enough space"):
        packages.create_package(env.session, env.config_id, classes=["valhalla"])


def test_reindex_rebuilds_rows_and_labels(env):
    pkg = packages.create_package(env.session, env.config_id, classes=["valhalla"], labels={"for": "q4"})
    packages.edit_labels(env.session, pkg.tag, labels={"for": "q4", "promoted": "dev-mini"}, note="n", protected=True)
    tag = pkg.tag
    env.session.query(Package).delete()
    env.session.commit()
    env.session.expunge_all()  # the bulk delete cascaded in the DB; drop the stale objects
    (env.repo / "r-20990101-1.partial").mkdir()
    result = packages.reindex(env.session)
    assert result == {"added": 1, "updated": 0, "partials_removed": 1}
    again = packages.get(env.session, tag)
    assert again.protected and again.note == "n" and len(again.items) == 3
    assert {(l.key, l.value) for l in again.labels if l.origin == "user"} == {("for", "q4"), ("promoted", "dev-mini")}
    assert any(l.key == "config" and l.origin == "auto" for l in again.labels)


def test_delete_and_prune_respect_protection(env):
    tags = [packages.create_package(env.session, env.config_id, classes=["valhalla"]).tag for _ in range(4)]
    packages.edit_labels(env.session, tags[0], protected=True)
    with pytest.raises(PackageError, match="protected"):
        packages.delete(env.session, tags[0])
    assert packages.prune(env.session, keep=2) == [tags[1]]  # dry run: oldest unprotected beyond the newest two
    assert (env.repo / tags[1]).exists()
    packages.prune(env.session, keep=2, dry_run=False)
    assert not (env.repo / tags[1]).exists() and (env.repo / tags[0]).exists()
    assert env.session.query(PackageLabel).filter_by(package_id=None).count() == 0


def test_reserved_tag_keys_are_rejected_as_labels(env):
    for key in ("date", "config", "created_by", "tool.valhalla", "resolved_hash.benelux"):
        with pytest.raises(PackageError, match="Reserved tag"):
            packages.create_package(env.session, env.config_id, ["valhalla"], labels={key: "x"})
    assert not env.repo.exists() or not list(env.repo.iterdir())
    pkg = packages.create_package(env.session, env.config_id, ["valhalla"], labels={"for": "q4"})
    with pytest.raises(PackageError, match="Reserved tag"):
        packages.edit_labels(env.session, pkg.tag, labels={"config": "other"})
    auto = {l.key: l.value for l in pkg.labels if l.origin == "auto"}
    assert auto["config"] == "dev-mini" and len(auto["date"]) == 10


def test_tiles_class_ships_styles_glyphs_sprites_and_a_manifest(env):
    pkg = packages.create_package(env.session, env.config_id, ["tiles"])
    folder = env.repo / pkg.tag
    assert (folder / "tiles/styles/swayrider/3/light.json").read_bytes() == b"style:style-light"
    assert (folder / "tiles/styles/swayrider/3/dark.json").exists()
    assert (folder / "tiles/glyphs/Noto Sans Regular/0-255.pbf").exists() and (folder / "tiles/sprites/light@2x.png").exists()
    manifest = json.loads((folder / "tiles/manifest.json").read_text())
    assert manifest["schema"] == 1 and manifest["release"] == pkg.tag
    assert manifest["tileset"] == {"name": "planet", "build": "20261004", "date": "2026-10-04", "schema_version": None, "file": "tiles.pmtiles"}
    assert manifest["styles"] == [{"id": "swayrider", "label": "SwayRider", "version": "3", "default": True,
                                   "variants": {"light": "styles/swayrider/3/light.json", "dark": "styles/swayrider/3/dark.json"}}]
    doc = json.loads((folder / "package.json").read_text())
    part = next(p for p in doc["classes"]["tiles"]["parts"] if p["path"] == "tiles/manifest.json")
    assert part["kind"] == "manifest" and part["meta"]["generated"] is True
    assert packages.verify_package(env.session, pkg.tag) == []  # generated and vendored parts are hashed like the rest


def test_styles_without_a_release_version_block_the_tiles_class(env):
    for a in env.session.query(Asset).filter_by(asset_type="style"):
        a.meta_json = {}
    env.session.commit()
    problems = packages.plan(env.session, env.config_id, ["tiles"]).problems
    assert any("no style id/version" in p for p in problems)


def test_pelias_class_ships_the_placeholder_store_and_restore_names(env):
    pkg = packages.create_package(env.session, env.config_id, ["pelias"])
    folder = env.repo / pkg.tag
    assert (folder / "pelias/placeholder/store.sqlite3.gz").read_bytes() == b"placeholder-store"
    parts = {p["path"]: p for p in json.loads((folder / "package.json").read_text())["classes"]["pelias"]["parts"]}
    assert parts["pelias/placeholder/store.sqlite3.gz"]["source"]["download_id"]
    meta = parts["pelias/benelux/benelux.es-snapshot.tar"]["meta"]
    assert (meta["index_name"], meta["snapshot_name"], meta["snapshot_repository"]) == ("pelias_benelux-29", "pelias_benelux-29", "pelias_repo")
    assert packages.verify_package(env.session, pkg.tag) == []


def test_missing_or_purged_placeholder_store_blocks_the_pelias_class(env):
    record = env.session.query(DownloadRecord).filter_by(source_key="placeholder:store").one()
    (Path(app_config.DATA_ROOT) / record.local_path).unlink()
    assert any("Placeholder store is gone" in p for p in packages.plan(env.session, env.config_id, ["pelias"]).problems)
    record.status = "rejected"
    env.session.commit()
    assert any("no approved placeholder:store" in p for p in packages.plan(env.session, env.config_id, ["pelias"]).problems)


def test_geodata_class_ships_a_regionservice_manifest(env):
    import hashlib

    import yaml

    pkg = packages.create_package(env.session, env.config_id, ["geodata"])
    folder = env.repo / pkg.tag
    doc = yaml.safe_load((folder / "geodata/manifest.yml").read_text())
    assert doc["tag"] == pkg.tag and doc["started-at"] <= doc["completed-at"]
    core = doc["regions"]["benelux"]["contour"]["core"]
    assert core["local-file"] == core["remote-file"] == "contours/benelux-core.geojson" and core["hash-type"] == "md5"
    assert core["hash"] == hashlib.md5((folder / "geodata/contours/benelux-core.geojson").read_bytes()).hexdigest()
    assert set(doc["regions"]["benelux"]["contour"]) == {"core", "extended"} and doc["shared"] == {}
    part = next(p for p in json.loads((folder / "package.json").read_text())["classes"]["geodata"]["parts"] if p["path"] == "geodata/manifest.yml")
    assert part["kind"] == "manifest" and part["meta"]["generated"] is True
    assert packages.verify_package(env.session, pkg.tag) == []


def test_geodata_manifest_lists_border_crossings(tmp_path):
    import datetime as dt

    (tmp_path / "geodata/contours").mkdir(parents=True)
    (tmp_path / "geodata/border-crossings").mkdir(parents=True)
    parts = []
    for rel, region, kind, name in [("geodata/contours/a-core.geojson", "a", "region-outline", "a-core"),
                                    ("geodata/contours/a-extended.geojson", "a", "region-outline", "a-extended"),
                                    ("geodata/border-crossings/a-b.csv", None, "border-crossings", "a-b")]:
        (tmp_path / rel).write_text(rel)
        parts.append({"path": rel, "region": region, "meta": {"asset_type": kind, "name": name}})
    doc = packages.geodata_manifest(tmp_path, "r-1", parts, dt.datetime(2026, 1, 1), dt.datetime(2026, 1, 2))
    assert doc["shared"]["border-crossings"]["a-b"]["remote-file"] == "border-crossings/a-b.csv"
    assert doc["regions"]["a"]["contour"]["extended"]["local-file"] == "contours/a-extended.geojson"


def test_deployed_packages_cannot_be_deleted_or_pruned(env):
    tags = [packages.create_package(env.session, env.config_id, ["valhalla"]).tag for _ in range(3)]
    env.session.add(PackageLabel(package_id=packages.get(env.session, tags[0]).id, key=packages.LIVE_LABEL, value="dev-mini", origin="auto"))
    env.session.commit()
    with pytest.raises(PackageError, match="deployed to dev-mini"):
        packages.delete(env.session, tags[0])
    assert packages.prune(env.session, keep=1) == [tags[1]]  # the deployed oldest one is skipped
    with pytest.raises(PackageError, match="Reserved tag"):
        packages.edit_labels(env.session, tags[1], labels={"live": "dev-mini"})
