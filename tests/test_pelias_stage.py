import datetime
import gzip
import http.server
import json
import stat
import threading
import zipfile
from pathlib import Path

import pytest

from datamanager.config import config
from datamanager.db import SessionLocal
from datamanager.jobs import tasks
from datamanager.models import Country, DownloadRecord
from datamanager.services import assets, pelias_build, pelias_data, pelias_es, runs
from datamanager.services import config_profiles as profiles
from datamanager.services import regions as region_service
from datamanager.stages import status as stage_status
from datamanager.stages.registry import default_registry

# ---- fakes: a docker script that only records its calls and importer scripts that record theirs ------------------

FAKE_DOCKER = """#!/bin/sh
echo "$@" >> "$FAKE_DOCKER_LOG"
case "$1" in
  info) echo 29.0.0 ;;
  image) exit 0 ;;
  run) echo fakecontainerid; [ -n "$FAKE_DOCKER_RUN_FAILS" ] && exit 1; exit 0 ;;
  ps) [ -f "$FAKE_DOCKER_PS" ] && cat "$FAKE_DOCKER_PS"; exit 0 ;;
  inspect) echo true ;;
  *) exit 0 ;;
esac
"""

FAKE_IMPORTER = """#!/bin/sh
name=$(basename "$(dirname "$PWD")")
echo "$(basename "$PWD") $PELIAS_CONFIG" >> "$CALLS"
cp "$PELIAS_CONFIG" "$CALLS_DIR/$(basename "$PWD")-$(basename "$PELIAS_CONFIG")"
[ "$(basename "$PWD")" = "$FAIL_IMPORTER" ] && { echo "boom in $FAIL_IMPORTER"; exit 1; }
[ "$(basename "$PWD")" = "polylines" ] && find "$(dirname "$(dirname "$PELIAS_CONFIG")")" \\( -type f -o -type l \\) | sort > "$CALLS_DIR/layout.txt"
exit 0
"""


def _script(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


@pytest.fixture()
def env(tmp_path, monkeypatch):
    calls_dir = tmp_path / "calls"
    calls_dir.mkdir()
    for key, value in (("FAKE_DOCKER_LOG", tmp_path / "docker.log"), ("CALLS", calls_dir / "calls.log"), ("CALLS_DIR", calls_dir),
                       ("FAKE_DOCKER_PS", tmp_path / "docker.ps")):
        monkeypatch.setenv(key, str(value))
    docker = _script(tmp_path / "bin" / "docker", FAKE_DOCKER)
    tools_dir = tmp_path / "pelias-tools"
    for name in pelias_data.IMPORTERS:
        _script(tools_dir / name / "bin" / ("create_index" if name == "schema" else "start"), FAKE_IMPORTER)
    monkeypatch.setattr(pelias_es, "docker_bin", lambda session, force=True: str(docker))
    monkeypatch.setattr(pelias_es, "max_map_count", lambda: 1048576)
    monkeypatch.setattr(pelias_build, "detect", lambda session: {"status": "ok", "message": "", "path": str(tools_dir), "version": "abc"})
    monkeypatch.setattr(pelias_build, "require", lambda session, name: tools_dir / name)
    monkeypatch.setattr(pelias_build, "version", lambda session=None: "commits-1")
    # no real Elasticsearch: the container lifecycle is real (against the fake docker), the HTTP calls are replaced
    state = {"count": 0}
    monkeypatch.setattr(pelias_es.TemporaryElasticsearch, "wait_ready", lambda self, timeout=0: None)
    order = []
    state["order"] = order
    monkeypatch.setattr(pelias_es.TemporaryElasticsearch, "count",
                        lambda self, index: order.append("count") or state.__setitem__("count", state["count"] + 100) or state["count"])
    monkeypatch.setattr(pelias_es.TemporaryElasticsearch, "refresh", lambda self, index: order.append("refresh"))

    def snapshot(self, index, name):
        repo = self.snapshots_dir / "repo"
        repo.mkdir(parents=True, exist_ok=True)
        (repo / "index-0").write_text(f"{index}/{name}")
        (self.data_dir / "marker").write_text("x")
        return repo

    monkeypatch.setattr(pelias_es.TemporaryElasticsearch, "snapshot", snapshot)
    result = SimpleEnv(tmp_path, calls_dir, tools_dir, monkeypatch)
    result.es_calls = state["order"]
    return result


class SimpleEnv:
    def __init__(self, tmp_path, calls_dir, tools_dir, monkeypatch):
        self.tmp, self.calls_dir, self.tools_dir, self.monkeypatch = tmp_path, calls_dir, tools_dir, monkeypatch

    def docker_log(self) -> list[str]:
        path = self.tmp / "docker.log"
        return path.read_text().splitlines() if path.exists() else []

    def importer_calls(self) -> list[str]:
        path = self.calls_dir / "calls.log"
        return [line.split()[0] for line in path.read_text().splitlines()] if path.exists() else []


# ---- inputs -------------------------------------------------------------------------------------------------------------

def _download(session, key, content: bytes, filename: str, approved=True):
    label = "20260101T000000Z"
    directory = Path(config.DATA_ROOT) / "downloads" / key.replace(":", "/") / label
    directory.mkdir(parents=True, exist_ok=True)
    file = directory / filename
    file.write_bytes(content)
    record = DownloadRecord(source_key=key, version_label=label, url="x", filename=filename, local_path=str(file.relative_to(config.DATA_ROOT)),
                            size_bytes=len(content), content_hash=f"h-{key}", status="approved" if approved else "fetched",
                            fetched_at=datetime.datetime(2026, 1, 1))
    session.add(record)
    session.commit()
    return record


def _zip(name: str) -> bytes:
    import io

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(f"{name}.txt", "1\tPlace\n")
    return buffer.getvalue()


def _asset(session, run, asset_type, name, filename, content=b"data", config_id=None):
    directory = assets.asset_dir("test", run.id)
    file = directory / filename
    file.write_bytes(content)
    asset = assets.create(session, run.id, config_id, asset_type, name, file)
    asset.status = "approved"
    session.commit()
    return asset


@pytest.fixture()
def cfg(app, env):
    session = SessionLocal()
    for iso in ("aa", "bb"):
        session.add(Country(iso2=iso, name=iso.upper(), ne_geometry_ref="x", bbox_json=[0, 0, 1, 1], geofabrik_path=f"europe/{iso}", wof_code=iso,
                            srtm_bbox_json=[50, 51, 0, 1]))
    session.commit()
    from datamanager.country_sources import SourceState
    from datamanager.services import address_sources as address_service

    address_service.set_state(session.get(Country, "aa"), "openaddresses", SourceState(True, {"files": ["aa/countrywide", "aa/missing"]}))
    session.commit()
    profile = profiles.create_profile(session, "dev")
    region = region_service.create_region(session, profile.id, "Region One")
    region_service.assign_country(session, region, "aa")
    region_service.assign_country(session, region, "bb")
    run = runs.create_run(session, "osm-extract", profile.id)
    _asset(session, run, "osm-pbf", "region-one", "region-one.osm.pbf", b"pbf", profile.id)
    _asset(session, run, "valhalla-polylines", "region-one", "polylines.0sv.gz", gzip.compress(b"poly"), profile.id)
    for iso in ("aa", "bb"):
        _download(session, f"wof:admin-{iso}", b"wofdb-" + iso.encode(), f"whosonfirst-data-admin-{iso}-latest.db.bz2")
        _download(session, f"geonames:{iso}", _zip(iso.upper()), f"{iso.upper()}.zip")
    import bz2

    for key in ("wof:admin-aa", "wof:admin-bb"):  # real bz2 content: the stage unpacks it
        record = session.query(DownloadRecord).filter_by(source_key=key).one()
        downloads_file = Path(config.DATA_ROOT) / record.local_path
        downloads_file.write_bytes(bz2.compress(b"sqlite-" + key.encode()))
    _download(session, "wof:postalcode-aa", bz2.compress(b"postal-aa"), "whosonfirst-data-postalcode-aa-latest.db.bz2")
    _download(session, "openaddresses:aa/countrywide", gzip.compress(b'{"type":"Feature"}\n'), "source.geojson.gz")
    return profile


def _resolved(profile_id):
    from datamanager.services import resolve

    return resolve.to_dict(resolve.resolve_config(SessionLocal(), profile_id))


def _run(profile_id, params=None):
    run = runs.create_run(SessionLocal(), "pelias", profile_id, params=params)
    result = tasks.run_stage(run.id)
    session = SessionLocal()
    session.refresh(run)
    return run, result


# ---- tests -------------------------------------------------------------------------------------------------------------

def test_plan_lists_inputs_warnings_and_blocked_reasons(cfg):
    from datamanager.stages.pelias import plan_inputs

    plan = plan_inputs(SessionLocal(), cfg.id, _resolved(cfg.id))
    assert plan.problems == [] and plan.regions[0].fingerprint
    region = plan.regions[0]
    assert [c.iso2 for c in region.countries] == ["aa", "bb"] and list(region.openaddresses) == ["aa/countrywide"]
    assert any("aa/missing" in w for w in region.warnings) and any("postal-code bundle" in w and "bb" in w for w in region.warnings)
    SessionLocal().query(DownloadRecord).filter_by(source_key="geonames:bb").update({"status": "fetched"})
    SessionLocal().commit()
    assert any("GeoNames file for bb" in p for p in plan_inputs(SessionLocal(), cfg.id, _resolved(cfg.id)).problems)


def test_blocked_when_environment_is_not_ready(cfg, env):
    from datamanager.stages.pelias import plan_inputs

    env.monkeypatch.setattr(pelias_es, "max_map_count", lambda: 65530)
    env.monkeypatch.setattr(pelias_build, "detect", lambda session: {"status": "missing", "message": "Not built yet.", "path": None, "version": None})
    problems = plan_inputs(SessionLocal(), cfg.id, _resolved(cfg.id)).problems
    assert any("vm.max_map_count" in p for p in problems) and any("importers are not built" in p for p in problems)


def test_imports_in_order_with_laid_out_data_and_removes_the_container(cfg, env):
    run, result = _run(cfg.id)
    report = run.report_json
    assert result["status"] == "awaiting_review", report
    assert env.importer_calls() == ["schema", "whosonfirst", "geonames", "geonames", "openaddresses", "openstreetmap", "polylines"]
    entry = report["pelias"][0]
    assert entry["result"] == "built" and entry["index"] == f"pelias_region-one-{run.id}" and [i["name"] for i in entry["importers"]][-1] == "polylines"
    # the data the importers read
    layout = (env.calls_dir / "layout.txt").read_text()
    for part in ("wof/sqlite/whosonfirst-data-admin-aa-latest.db", "wof/sqlite/whosonfirst-data-admin-bb-latest.db",
                 "wof/sqlite/whosonfirst-data-postalcode-aa-latest.db", "geonames/aa/AA.zip", "geonames/bb/BB.zip",
                 "openaddresses/aa/countrywide.geojson", "osm/region-one.osm.pbf", "polylines/polylines.0sv"):
        assert part in layout, part
    assert "postalcode-bb" not in layout
    assert env.es_calls[:4] == ["refresh", "count", "refresh", "count"]  # always refreshed before counting
    load = json.loads((env.calls_dir / "geonames-geonames-aa.json").read_text())
    assert load["imports"]["geonames"]["countryCode"] == "AA" and load["imports"]["geonames"]["datapath"].endswith("geonames/aa")
    assert load["schema"]["indexName"] == entry["index"] and load["imports"]["openaddresses"]["files"] == ["aa/countrywide"]
    assert "token" not in json.dumps(load["imports"]["openaddresses"])
    # container lifecycle: started with bind mounts and a uid, removed afterwards, directories gone
    runs_ = [line for line in env.docker_log() if line.startswith("run ")]
    assert len(runs_) == 1 and "--user" in runs_[0] and ":/usr/share/elasticsearch/data" in runs_[0] and "-v" in runs_[0] and "volume" not in runs_[0]
    assert any(line == f"rm -f dm-pelias-es-{run.id}" for line in env.docker_log())
    assert not (Path(config.DATA_ROOT) / "work" / str(run.id)).exists()
    # assets
    runs.approve(SessionLocal(), runs.get_run(SessionLocal(), run.id))
    snapshot = assets.current(SessionLocal(), cfg.id, "pelias-index-snapshot", "region-one")
    assert snapshot.meta_json["index_name"] == entry["index"] and snapshot.meta_json["docs"] == entry["docs"]
    prod = json.loads(assets.abs_path(assets.current(SessionLocal(), cfg.id, "pelias-config", "region-one")).read_text())
    assert prod["esclient"]["hosts"][0]["host"] == "elasticsearch" and prod["imports"]["whosonfirst"]["datapath"] == "/data/whosonfirst"
    import tarfile

    with tarfile.open(assets.abs_path(assets.current(SessionLocal(), cfg.id, "pelias-wof", "region-one"))) as archive:
        names = archive.getnames()
        assert "whosonfirst-data-admin-aa-latest.db" in names and archive.getmember("whosonfirst-data-admin-aa-latest.db").isfile()
    with tarfile.open(assets.abs_path(snapshot)) as archive:
        assert "index-0" in archive.getnames()


def test_patched_wof_replaces_the_plain_download(cfg, env):
    session = SessionLocal()
    run = runs.create_run(session, "wof-patch", cfg.id)
    patched = _asset(session, run, "wof-patched-sqlite", "bb", "whosonfirst-data-admin-bb-latest.db", b"PATCHED")
    _run(cfg.id)
    # the layout listing shows a link; the patched file is what it points at
    assert "whosonfirst-data-admin-bb-latest.db" in (env.calls_dir / "layout.txt").read_text()
    assert patched.id and SessionLocal().query(DownloadRecord).filter_by(source_key="wof:admin-bb").count() == 1


def test_importer_failure_cleans_up_and_keeps_no_assets(cfg, env):
    env.monkeypatch.setenv("FAIL_IMPORTER", "openstreetmap")
    run, result = _run(cfg.id)
    assert result["status"] == "failed"
    entry = run.report_json["pelias"][0]
    assert entry["status"] == "failed" and any("boom in openstreetmap" in line for line in entry["log"])
    assert env.importer_calls()[-1] == "openstreetmap"  # polylines never ran
    assert any(line.startswith("rm -f dm-pelias-es-") for line in env.docker_log())
    assert not (Path(config.DATA_ROOT) / "work" / str(run.id)).exists()
    assert assets.current(SessionLocal(), cfg.id, "pelias-index-snapshot", "region-one") is None


def test_container_start_failure_leaves_no_directories(cfg, env):
    env.monkeypatch.setenv("FAKE_DOCKER_RUN_FAILS", "1")
    run, result = _run(cfg.id)
    assert result["status"] == "failed" and "docker run" in run.report_json["pelias"][0]["message"]
    assert any(line.startswith("rm -f dm-pelias-es-") for line in env.docker_log())
    assert not (Path(config.DATA_ROOT) / "work" / str(run.id)).exists()


def test_stale_container_and_directory_of_a_dead_run_are_removed(cfg, env):
    stale = Path(config.DATA_ROOT) / "work" / "999" / "es" / "data"
    stale.mkdir(parents=True)
    (stale / "left").write_text("x")
    (env.tmp / "docker.ps").write_text("dm-pelias-es-999 999\n")
    run, result = _run(cfg.id)
    assert result["status"] == "awaiting_review"
    assert "rm -f dm-pelias-es-999" in env.docker_log() and not stale.exists()
    assert any("dm-pelias-es-999" in item for item in run.report_json["cleaned_up"])


def test_unchanged_region_is_skipped_and_status_follows(cfg, env):
    session = SessionLocal()
    assert stage_status.compute(session, cfg.id, _resolved(cfg.id), {})["pelias"].state == "todo"
    run, _ = _run(cfg.id)
    runs.approve(SessionLocal(), runs.get_run(SessionLocal(), run.id))
    assert stage_status.compute(SessionLocal(), cfg.id, _resolved(cfg.id), {})["pelias"].state == "ok"
    before = len([line for line in env.docker_log() if line.startswith("run ")])
    run, _ = _run(cfg.id)
    assert run.report_json["pelias"][0]["result"] == "unchanged"
    assert len([line for line in env.docker_log() if line.startswith("run ")]) == before
    runs.approve(SessionLocal(), runs.get_run(SessionLocal(), run.id))
    env.monkeypatch.setattr(pelias_build, "version", lambda session=None: "commits-2")
    assert stage_status.compute(SessionLocal(), cfg.id, _resolved(cfg.id), {})["pelias"].state == "outdated"


def test_registry_pulls_in_both_producers():
    order = default_registry.resolve_order(["pelias"])
    assert order.index("osm-extract") < order.index("pelias") and order.index("valhalla") < order.index("pelias")


def test_build_page_card_and_regions_param(cfg, client):
    html = client.get("/build/").get_data(as_text=True)
    assert "Pelias index" in html


# ---- the Elasticsearch calls against a stub server ----------------------------------------------------------------------

class StubEs(http.server.BaseHTTPRequestHandler):
    snapshot_state = "SUCCESS"

    def _answer(self, body, status=200):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._answer({"count": 42} if self.path.endswith("/_count") else {"status": "green"})

    def do_POST(self):
        self._answer({})

    def do_PUT(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        if "wait_for_completion" in self.path:
            self._answer({"snapshot": {"state": StubEs.snapshot_state}})
        else:
            self._answer({"acknowledged": True})

    def log_message(self, *args):
        pass


def test_es_calls_against_a_stub_server(tmp_path):
    server = http.server.HTTPServer(("127.0.0.1", 0), StubEs)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        es = pelias_es.TemporaryElasticsearch("docker", "7.17.28", "1g", 1, tmp_path / "es")
        es.port = server.server_address[1]
        es.wait_ready(timeout=5)
        assert es.count("idx") == 42
        es.refresh("idx")
        assert es.snapshot("idx", "snap") == tmp_path / "es" / "snapshots" / "repo"
        StubEs.snapshot_state = "PARTIAL"
        with pytest.raises(pelias_es.EsError, match="PARTIAL"):
            es.snapshot("idx", "snap")
    finally:
        StubEs.snapshot_state = "SUCCESS"
        server.shutdown()


# ---- Overture and GTFS ---------------------------------------------------------------------------------------------------

def _overture_inputs(session, with_gtfs=True):
    from datamanager.country_sources import SourceState
    from datamanager.services import address_sources as address_service

    address_service.set_state(session.get(Country, "aa"), "overture", SourceState(enabled=True))
    session.commit()
    _download(session, "overture:region-one-places", b"id,name,lat,lon\n1,Cafe,49,6\n", "places.csv")
    if with_gtfs:
        region = region_service.list_regions(session, session.query(Country).first() and 1)[0]
        feed = region_service.add_gtfs_feed(session, region, "http://x/feed.zip", "Feed")
        _download(session, f"gtfs:region-one-{feed.id}", _zip("stops"), "feed.zip")
        return feed


def test_overture_and_gtfs_are_imported_after_polylines(cfg, env):
    session = SessionLocal()
    feed = _overture_inputs(session)
    run, result = _run(cfg.id)
    assert result["status"] == "awaiting_review", run.report_json
    assert env.importer_calls()[-3:] == ["polylines", "csv-importer", "transit"]
    entry = run.report_json["pelias"][0]
    assert [i["name"] for i in entry["importers"]][-2:] == ["csv-importer", "transit"]
    layout = (env.calls_dir / "layout.txt").read_text()
    assert "csv/overture-places.csv" in layout and "csv/overture-addresses.csv" not in layout
    assert any("No approved Overture addresses" in w for w in entry["warnings"])
    load = json.loads((env.calls_dir / "csv-importer-pelias.json").read_text())
    assert load["imports"]["csv"]["files"] == ["overture-places.csv"]
    feeds = json.loads((env.calls_dir / "transit-pelias.json").read_text())["imports"]["transit"]["feeds"]
    assert feeds[0]["layerId"] == "stops" and feeds[0]["filename"] == f"region-one-{feed.id}-stops.txt"
    assert "url" not in feeds[0]  # the transit importer rejects an empty url and then exits 0 without importing


def test_failing_overture_importer_is_a_warning_not_a_failure(cfg, env):
    _overture_inputs(SessionLocal(), with_gtfs=False)
    env.monkeypatch.setenv("FAIL_IMPORTER", "csv-importer")
    run, result = _run(cfg.id)
    entry = run.report_json["pelias"][0]
    assert result["status"] == "awaiting_review" and entry["result"] == "built"
    assert any("csv-importer failed" in w for w in entry["warnings"]) and entry["importers"][-1]["error"]
    assert assets.current(SessionLocal(), cfg.id, "pelias-index-snapshot", "region-one") is None  # still unapproved


def test_an_optional_importer_that_adds_nothing_is_a_warning(cfg, env):
    _overture_inputs(SessionLocal())
    env.monkeypatch.setattr(pelias_es.TemporaryElasticsearch, "count", lambda self, index: 7)  # nothing is ever added
    run, result = _run(cfg.id)
    entry = run.report_json["pelias"][0]
    assert result["status"] == "awaiting_review" and entry["result"] == "built"
    assert any("transit added no documents" in w for w in entry["warnings"]) and any("csv-importer added no documents" in w for w in entry["warnings"])


def test_production_config_points_at_the_interpolation_service_import_config_does_not(tmp_path):
    from datamanager.services import pelias_data

    layout = pelias_data.Layout(tmp_path, "benelux")
    args = dict(index="pelias_benelux-1", es_host="elasticsearch", es_port=9200, wof_codes=["BE"], openaddresses=[])
    prod = pelias_data.render_config(layout, prod=True, **args)
    load = pelias_data.render_config(layout, prod=False, **args)
    assert prod["api"]["services"]["interpolation"] == {"url": "http://pelias-benelux-interpolation:4300"}  # what the API reads
    assert prod["interpolation"]["client"] == {"adapter": "null"}
    assert "interpolation" not in load["api"]["services"]
    assert load["interpolation"]["client"] == {"adapter": "null"}
