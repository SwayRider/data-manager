import datetime
from collections import namedtuple
from pathlib import Path

import pytest

from datamanager.config import config as app_config
from datamanager.errors import PackageError, ValidationError
from datamanager.models import Asset, BuildRun, ConfigProfile, DownloadRecord, Region
from datamanager.services import assets as asset_service
from datamanager.services import cleanup, downloads, packages
from datamanager.services import settings as settings_service

Env = namedtuple("Env", "session config_id tag repo")
DATA = lambda rel: Path(app_config.DATA_ROOT) / rel  # noqa: E731


def _asset(session, config_id, asset_type, name, rel, content=b"x", status="approved", run_id=None):
    file = DATA(rel)
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_bytes(content)
    asset = Asset(asset_type=asset_type, name=name, config_profile_id=config_id, path=rel, produced_by_run_id=run_id,
                  content_hash=asset_service.sha256_of(file), size_bytes=len(content), status=status)
    session.add(asset)
    session.flush()
    return asset


def _download(session, key, rel, content=b"d", status="approved", pinned=False):
    file = DATA(rel)
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_bytes(content)
    record = DownloadRecord(source_key=key, version_label=f"2026100{len(content) % 9}T000000Z", url="http://x", filename=file.name,
                            local_path=rel, size_bytes=len(content), content_hash=asset_service.sha256_of(file),
                            fetched_at=datetime.datetime(2026, 10, 4), status=status, pinned=pinned)
    session.add(record)
    session.flush()
    return record


@pytest.fixture()
def env(db_session, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    monkeypatch.setattr(app_config, "PACKAGE_ROOT", str(repo))
    s = db_session
    cfg = ConfigProfile(name="dev-mini")
    s.add(cfg)
    s.flush()
    s.add(Region(config_profile_id=cfg.id, name="benelux", color="#ff0000"))
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
                    ("style", "style-light", "styles/6/style-light.json"), ("style", "style-dark", "styles/6/style-dark.json")]:
        _asset(s, cid, t, n, f"{a}/{f}", content=f"{t}:{n}".encode())
    # superseded snapshot of an older run, intermediates that no package holds, and user input
    _asset(s, cid, "pelias-index-snapshot", "benelux", f"{a}/pelias/23/benelux/benelux.es-snapshot.tar", b"old-snapshot-23!")
    _asset(s, None, "country-pbf", "belgium", f"{a}/country-pbf/5/belgium.osm.pbf", b"belgium-pbf")
    _asset(s, cid, "carve-polygon", "carve-be", f"{a}/polygons/3/carve-be.poly", b"poly")
    _asset(s, cid, "overlap-polygon", "benelux-overlap", f"{a}/polygons/3/benelux-overlap.poly", b"poly2")
    s.commit()
    pm = _download(s, "tiles:planet", "downloads/tiles/planet/20261004T000000Z/p.pmtiles", b"PMTiles-fixture")
    _download(s, "planet:osm", "downloads/planet/osm/20261001T000000Z/planet.pbf", b"planet-bytes")
    _download(s, "wof:be", "downloads/wof/be/20261001T000000Z/be.db", b"wofbe")
    s.commit()
    unpacked = DATA("library/srtm/9/N50/N50E004.hgt")
    unpacked.parent.mkdir(parents=True)
    unpacked.write_bytes(b"hgt" * 10)
    pkg = packages.create_package(s, cid)
    return Env(s, cid, pkg.tag, repo)


def _verified(env):
    assert packages.verify_package(env.session, env.tag) == []


def test_cleanup_needs_a_verified_package(env):
    with pytest.raises(PackageError, match="not verified"):
        cleanup.plan(env.session, env.tag)
    _verified(env)
    assert cleanup.plan(env.session, env.tag).items


def test_defaults_come_from_the_settings(env):
    ticked = cleanup.defaults(env.session)
    assert ticked["country_pbf"] and ticked["pelias_snapshots"] and not ticked["planet"] and not ticked["srtm_downloads"]
    settings_service.save(env.session, {"cleanup.default.planet": "delete"})
    assert cleanup.defaults(env.session)["planet"] is True


def test_dry_run_lists_without_touching(env):
    _verified(env)
    the_plan = cleanup.plan(env.session, env.tag, ["pelias_snapshots", "country_pbf", "small_assets"])
    cats = the_plan.by_category()
    assert cats["pelias_snapshots"]["count"] == 2 and cats["country_pbf"]["count"] == 1
    assert not any("carve" in i.label for i in the_plan.items)  # user input is in no category
    assert DATA("library/assets/country-pbf/5/belgium.osm.pbf").exists()


def test_apply_purges_files_but_keeps_rows_and_current_assets(env):
    _verified(env)
    result = cleanup.apply(env.session, env.tag, ["pelias_snapshots", "country_pbf", "small_assets", "valhalla"])
    assert result.freed > 0 and not result.errors and result.purged >= 8
    snap = asset_service.current(env.session, env.config_id, "pelias-index-snapshot", "benelux")
    assert snap is not None and asset_service.is_purged(snap) and not asset_service.abs_path(snap).exists()
    assert snap.meta_json["purged"]["package"] == env.tag and snap.content_hash
    assert not asset_service.usable(snap)  # a purged result is rebuilt, never skipped
    assert DATA("library/assets/polygons/3/carve-be.poly").exists()  # carve-outs are never offered
    overlap = env.session.query(Asset).filter_by(asset_type="overlap-polygon").one()
    assert asset_service.is_purged(overlap) and not DATA(overlap.path).exists()
    with pytest.raises(ValidationError, match="purged by the cleanup"):
        asset_service.input_path(env.session.query(Asset).filter_by(asset_type="country-pbf").one())
    again = cleanup.apply(env.session, env.tag, ["pelias_snapshots"])
    assert again.purged == 0  # nothing left to do


def test_current_asset_not_in_the_package_is_skipped(env):
    _verified(env)
    newer = _asset(env.session, env.config_id, "pelias-index-snapshot", "benelux",
                   "library/assets/pelias/30/benelux/benelux.es-snapshot.tar", b"built-after-packaging")
    env.session.commit()
    result = cleanup.apply(env.session, env.tag, ["pelias_snapshots"])
    assert any("not in" in s for s in result.skipped)
    assert DATA(newer.path).exists()
    assert not DATA("library/assets/pelias/23/benelux/benelux.es-snapshot.tar").exists()  # superseded: no coverage needed
    assert not DATA("library/assets/pelias/29/benelux/benelux.es-snapshot.tar").exists()  # superseded now, and it is packaged


def test_downloads_are_purged_with_coverage_and_pins(env):
    _verified(env)
    pinned = env.session.query(DownloadRecord).filter_by(source_key="wof:be").one()
    pinned.pinned = True
    env.session.commit()
    result = cleanup.apply(env.session, env.tag, ["pelias_sources", "tiles", "planet"])
    assert any("pinned" in s for s in result.skipped)
    tiles = env.session.query(DownloadRecord).filter_by(source_key="tiles:planet").one()
    assert tiles.purged_at is not None and tiles.purged_package == env.tag and not downloads.abs_path(tiles).exists()
    with pytest.raises(ValidationError, match="purged by the cleanup"):
        downloads.input_path(tiles)
    assert DATA("downloads/wof/be/20261001T000000Z/be.db").exists()


def test_unpacked_srtm_and_leftovers(env):
    _verified(env)
    failed = BuildRun(stage_key="pelias", status="failed")
    review = BuildRun(stage_key="pelias", status="awaiting_review")
    env.session.add_all([failed, review])
    env.session.flush()
    _asset(env.session, env.config_id, "wof-patched-sqlite", "be", "library/assets/wof/50/be.sqlite", status="produced", run_id=failed.id)
    keep = _asset(env.session, env.config_id, "wof-patched-sqlite", "nl", "library/assets/wof/51/nl.sqlite", status="produced", run_id=review.id)
    DATA("work/777").mkdir(parents=True)
    (DATA("work/777") / "x").write_text("scratch")
    env.session.commit()
    result = cleanup.apply(env.session, env.tag, ["srtm_unpacked", "leftovers"])
    assert not DATA("library/srtm/9").exists() and not DATA("work/777").exists()
    assert not DATA("library/assets/wof/50/be.sqlite").exists() and DATA(keep.path).exists()  # awaiting review stays
    assert env.session.query(Asset).filter_by(name="be", asset_type="wof-patched-sqlite").count() == 0
    assert result.deleted >= 3


def test_cleanup_waits_for_running_runs(env):
    _verified(env)
    env.session.add(BuildRun(stage_key="pelias", status="running"))
    env.session.commit()
    assert cleanup.plan(env.session, env.tag).busy
    with pytest.raises(PackageError, match="queued or running"):
        cleanup.apply(env.session, env.tag, ["country_pbf"])
    assert DATA("library/assets/country-pbf/5/belgium.osm.pbf").exists()


def test_old_versions_are_deleted_not_purged(env):
    _verified(env)
    for i in range(3):
        _download(env.session, "gtfs:x", f"downloads/gtfs/x/v{i}/f.zip", b"z" * (i + 1))
    env.session.commit()
    plan = cleanup.plan(env.session, env.tag, ["old_versions"])
    assert plan.items and all(i.kind == "delete-download" for i in plan.items)
    cleanup.apply(env.session, env.tag, ["old_versions"])
    assert env.session.query(DownloadRecord).filter(DownloadRecord.source_key == "gtfs:x").count() == 2  # newest two stay


def test_build_status_shows_purged_results_not_up_to_date(env):
    from datamanager.stages import status as stage_status

    _verified(env)
    assert stage_status._own_purged(env.session, env.config_id, "pelias") is None  # files still there
    cleanup.apply(env.session, env.tag, ["pelias_snapshots", "country_pbf", "small_assets", "valhalla"])
    for key in ("pelias", "valhalla", "styles", "extract-countries"):
        state = stage_status._own_purged(env.session, env.config_id, key)
        assert state is not None and state.state == "purged" and env.tag in state.detail, key
    assert stage_status._own_purged(env.session, env.config_id, "osm-extract") is None  # nothing of it was removed
    assert stage_status._own_purged(env.session, env.config_id, "no-such-stage") is None


def test_stage_with_a_cleaned_up_input_is_blocked_with_the_producer(env):
    from datamanager.stages import status as stage_status

    _verified(env)
    assert stage_status._purged_input(env.session, env.config_id, "osm-extract") is None
    cleanup.apply(env.session, env.tag, ["country_pbf"])
    reason = stage_status._purged_input(env.session, env.config_id, "osm-extract")
    assert "country-pbf" in reason and "extract-countries" in reason and env.tag in reason
    assert stage_status._purged_input(env.session, env.config_id, "valhalla") is None


def test_new_package_is_refused_while_results_are_cleaned_up(env):
    _verified(env)
    cleanup.apply(env.session, env.tag, ["valhalla"])
    problems = packages.plan(env.session, env.config_id, ["valhalla"]).problems
    assert problems and all("removed by the cleanup" in p for p in problems)


def test_package_verify_and_cleanup_are_not_green_once_the_results_are_cleaned_up(env):
    from datamanager.stages import status as stage_status

    _verified(env)
    assert stage_status._package(env.session, env.config_id).state == "ok"
    assert stage_status._package_verify(env.session, env.config_id).state == "ok"
    cleanup.apply(env.session, env.tag, ["valhalla", "pelias_snapshots"])
    env.session.add(BuildRun(stage_key="cleanup", config_profile_id=env.config_id, status="approved", params_json={"tag": env.tag}))
    env.session.flush()
    for fn in (stage_status._package, stage_status._package_verify, stage_status._cleanup):
        state = fn(env.session, env.config_id)
        assert state.state == "purged" and env.tag in state.detail, fn.__name__
