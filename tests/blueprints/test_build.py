import json

import pytest

from datamanager.blueprints.build import routes
from datamanager.config import config
from datamanager.db import SessionLocal
from datamanager.models import Country, DownloadRecord
from datamanager.services import config_profiles as profiles
from datamanager.services import downloads, runs
from datamanager.services import settings as settings_service
from datamanager.services import regions as region_service

HX = {"HX-Request": "true"}


@pytest.fixture()
def cfg(app):
    session = SessionLocal()
    with open(config.DATA_ROOT + "/geo-aa.geojson", "w") as f:
        json.dump({"type": "Polygon", "coordinates": [[[0, 50], [1, 50], [1, 51], [0, 51], [0, 50]]]}, f)
    session.add(Country(iso2="aa", name="Aa", ne_geometry_ref="geo-aa.geojson", bbox_json=[0, 0, 1, 1],
                        geofabrik_path="europe/aa", wof_code="aa", srtm_bbox_json=[50, 51, 0, 1]))
    session.commit()
    profile = profiles.create_profile(session, "dev")
    region = region_service.create_region(session, profile.id, "r1")
    region_service.assign_country(session, region, "aa")
    return profile


def test_index_lists_stage_and_runs(client, cfg):
    html = client.get("/build/").get_data(as_text=True)
    assert "Download planet" in html and "No runs yet" in html and "<html" in html
    assert 'disabled' not in html.split("stage-card")[1].split("</section>")[0]


def test_run_disabled_without_geofabrik_countries(client, app):
    profiles.create_profile(SessionLocal(), "empty")
    html = client.get("/build/").get_data(as_text=True)
    assert "no core country with a Geofabrik path" in html
    assert client.post("/build/run/download-osm").status_code == 422


def test_start_creates_run_and_enqueues(client, cfg, monkeypatch):
    queued = []
    monkeypatch.setattr(routes, "enqueue_run", lambda run_id: queued.append(run_id) or "job-1")
    config_id = cfg.id  # the request tears down the scoped session, detaching cfg
    resp = client.post(f"/build/run/download-osm?config={config_id}")
    assert resp.status_code == 302
    run = runs.list_runs(SessionLocal())[0]
    assert queued == [run.id] and run.rq_job_id == "job-1" and run.status == "queued"
    assert resp.headers["Location"].endswith(f"/build/runs/{run.id}")
    assert client.post(f"/build/run/unknown?config={config_id}").status_code == 404


def test_broker_down_leaves_a_failed_run(client, cfg, monkeypatch):
    def boom(run_id):
        raise ConnectionError("redis down")

    monkeypatch.setattr(routes, "enqueue_run", boom)
    client.post(f"/build/run/download-osm?config={cfg.id}")
    run = runs.list_runs(SessionLocal())[0]
    assert run.status == "failed" and "redis down" in run.error_message


def _awaiting_run(with_record=True):
    session = SessionLocal()
    run = runs.create_run(session, "download-osm", None)
    record = None
    if with_record:
        import datetime
        record = DownloadRecord(source_key="osm:europe/aa", version_label="20260101T000000Z", url="http://x", filename="f",
                                local_path="downloads/osm/europe/aa/x/f", size_bytes=5, content_hash="ab" * 32,
                                fetched_at=datetime.datetime(2026, 1, 1), run_id=run.id)
        session.add(record)
        session.commit()
    runs.mark_running(session, run)
    runs.finish(session, run, {"summary": {"sources": 1, "downloaded": 1, "unchanged": 0, "failed": 0, "bytes": 5},
                               "files": [{"source_key": "osm:europe/aa", "status": "downloaded", "version": "20260101T000000Z",
                                          "size_bytes": 5, "sha256": "ab" * 32, "header": {"ok": True}}],
                               "record_ids": [record.id] if record else []})
    return run


def test_run_page_full_and_partial_with_review_form(client, app):
    run = _awaiting_run()
    full = client.get(f"/build/runs/{run.id}").get_data(as_text=True)
    part = client.get(f"/build/runs/{run.id}", headers=HX).get_data(as_text=True)
    assert "<html" in full and "<html" not in part
    assert "Validation report" in part and "Approve" in part and "header ok" in part
    assert "hx-trigger" not in part  # not active -> no polling
    assert client.get("/build/runs/999").status_code == 404


def test_active_run_polls(client, app):
    run = runs.create_run(SessionLocal(), "download-osm", None)
    assert "hx-trigger=\"every 2s\"" in client.get(f"/build/runs/{run.id}", headers=HX).get_data(as_text=True)


def test_approve_and_reject_over_http(client, app):
    run = _awaiting_run()
    html = client.post(f"/build/runs/{run.id}/approve", data={"note": "ok"}, headers=HX).get_data(as_text=True)
    assert "Approved" in html and "ok" in html
    assert downloads.resolve_version(SessionLocal(), "osm:europe/aa") is not None
    again = client.post(f"/build/runs/{run.id}/reject", headers=HX)
    assert again.status_code == 422 and "awaiting review" in again.get_data(as_text=True)

    other = _awaiting_run(with_record=False)
    assert "Rejected" in client.post(f"/build/runs/{other.id}/reject", headers=HX).get_data(as_text=True)


def test_downloads_page_pin_delete_and_cleanup(client, app):
    run = _awaiting_run()
    runs.approve(SessionLocal(), run)
    record = downloads.versions(SessionLocal(), "osm:europe/aa")[0]
    page = client.get("/build/downloads").get_data(as_text=True)
    assert "osm:europe/aa" in page and "in use" in page and "<html" in page

    pinned = client.post(f"/build/downloads/{record.id}/pin", data={"pinned": "1"}, headers=HX).get_data(as_text=True)
    assert "pinned" in pinned and "<html" not in pinned
    refused = client.post(f"/build/downloads/{record.id}/delete", headers=HX)
    assert refused.status_code == 422 and "in use" in refused.get_data(as_text=True)

    preview = client.post("/build/downloads/cleanup", data={"keep": "1"}, headers=HX).get_data(as_text=True)
    assert "Would delete 0 version(s)" in preview
    assert client.post("/build/downloads/cleanup", data={"keep": "0"}, headers=HX).status_code == 200  # 0 -> default
    assert client.post("/build/downloads/999/pin", headers=HX).status_code == 404


def test_polygons_stage_card_run_page_and_review(client, cfg):
    from datamanager.jobs import tasks

    html = client.get("/build/").get_data(as_text=True)
    assert "Region polygons" in html
    config_id = cfg.id
    session = SessionLocal()
    run = runs.create_run(session, "polygons", config_id)
    tasks.run_stage(run.id)

    full = client.get(f"/build/runs/{run.id}").get_data(as_text=True)
    part = client.get(f"/build/runs/{run.id}", headers=HX).get_data(as_text=True)
    assert "polymap.js" in full and "polymap.js" not in part and "<html" not in part
    for text in (full, part):
        assert 'id="poly-map"' in text and 'id="poly-data"' in text and "r1-overlap" in text and "initPolyMap" in text
    assert "Approve" in part and "km²" in part

    approved = client.post(f"/build/runs/{run.id}/approve", headers=HX).get_data(as_text=True)
    assert "Approved" in approved and 'id="poly-map"' in approved
    assert client.get("/build/static/polymap.js").status_code == 200


def test_polygons_disabled_without_core(client, app):
    profiles.create_profile(SessionLocal(), "empty")
    html = client.get("/build/").get_data(as_text=True)
    assert "No region has a core country." in html
    assert client.post("/build/run/polygons").status_code == 422


def test_osm_extract_card_disabled_until_inputs_are_approved(client, cfg):
    html = client.get(f"/build/?config={cfg.id}").get_data(as_text=True)
    card = next(s for s in html.split("stage-card")[1:] if "</span> OSM extract</h3>" in s).split("</section>")[0]
    assert "disabled" in card and "no approved country extract of europe/aa" in card
    assert client.post(f"/build/run/osm-extract?config={cfg.id}").status_code == 422


def test_osm_extract_report_renders(client, cfg):
    session = SessionLocal()
    run = runs.create_run(session, "osm-extract", cfg.id)
    part = {"asset_id": 1, "size_bytes": 2000, "sha256": "ab" * 32, "counts": {"nodes": 5, "ways": 1, "relations": 0},
            "bbox": [0, 50, 1, 51], "replication_timestamp": "2026-09-01T00:00:00Z", "last_timestamp": None}
    report = {"summary": {"regions": 1, "failed": 0, "warnings": 1, "bytes": 2000},
              "regions": [{"name": "r1", "status": "success", "warnings": ["europe/aa is 90 days old."], "core": part, "pbf": part,
                           "inputs": [{"iso2": "aa", "role": "core", "source_key": "osm:europe/aa", "version": "20260101T000000Z",
                                       "fetched_at": "2026-01-01", "carved": True, "pinned": False}]}]}
    runs.mark_running(session, run)
    runs.finish(session, run, report)
    html = client.get(f"/build/runs/{run.id}").get_data(as_text=True)
    assert "europe/aa is 90 days old." in html and "osm:europe/aa" in html and "carved" in html


def _card(html, title):
    return next(s for s in html.split("stage-card")[1:] if f"</span> {title}</h3>" in s).split("</section>")[0]


def test_planet_card_has_skip_options_and_extract_waits_for_an_approved_planet(client, cfg):
    html = client.get(f"/build/?config={cfg.id}").get_data(as_text=True)
    planet = _card(html, "Download planet")
    assert "use_existing" in planet and "local_file" in planet and "No approved planet yet" in planet and "disabled" not in planet
    extract = _card(html, "Extract countries")
    assert "disabled" in extract and "No approved planet" in extract
    assert "Download OSM extracts (Geofabrik)" not in html  # only shown for the geofabrik source


def test_geofabrik_source_shows_its_download_card(client, cfg):
    settings_service.save(SessionLocal(), {"osm.source": "geofabrik"})
    assert "Download OSM extracts (Geofabrik)" in client.get(f"/build/?config={cfg.id}").get_data(as_text=True)


def test_start_planet_run_passes_the_skip_options(client, cfg, monkeypatch):
    monkeypatch.setattr(routes, "enqueue_run", lambda run_id: "job-1")
    config_id = cfg.id
    resp = client.post(f"/build/run/download-planet?config={config_id}", data={"use_existing": "1", "local_file": " /data/planet.pbf "})
    assert resp.status_code == 302
    run = runs.list_runs(SessionLocal())[0]
    assert run.stage_key == "download-planet" and run.params_json == {"use_existing": True, "local_file": "/data/planet.pbf"}


def test_extract_countries_enabled_once_a_planet_is_approved(client, cfg):
    import datetime
    session = SessionLocal()
    session.add(DownloadRecord(source_key="planet:osm", version_label="20260920T000000Z", url="x", filename="p", local_path="p",
                               size_bytes=1, content_hash="h", fetched_at=datetime.datetime(2026, 9, 20), status="approved",
                               data_timestamp="2026-09-19T00:00:00Z"))
    session.commit()
    html = client.get(f"/build/?config={cfg.id}").get_data(as_text=True)
    assert "disabled" not in _card(html, "Extract countries")
    assert "Current planet: 20260920T000000Z, data of 2026-09-19" in html


def test_planet_and_country_reports_render(client, cfg):
    config_id = cfg.id  # the request below tears down the scoped session
    session = SessionLocal()
    planet = runs.create_run(session, "download-planet", config_id)
    runs.mark_running(session, planet)
    runs.finish(session, planet, {
        "summary": {"status": "imported", "version": "20260920T000000Z", "size_bytes": 5000, "data_timestamp": "2026-09-19T00:00:00Z",
                    "age_days": 11, "md5_ok": None, "pruned": 1, "kept": 2},
        "file": {"url": "file:///data/planet.pbf", "filename": "planet.pbf", "sha256": "ab" * 32, "etag": None,
                 "upstream_modified": None, "fetched_at": "2026-10-01 10:00", "record_status": "fetched", "previous_version": None},
        "warnings": ["The planet's data is 11 days old."], "record_ids": [1]})
    planet_id = planet.id
    html = client.get(f"/build/runs/{planet_id}").get_data(as_text=True)
    assert "imported" in html and "file:///data/planet.pbf" in html and "11 days old" in html
    extract = runs.create_run(session, "extract-countries", config_id)
    runs.mark_running(session, extract)
    runs.finish(session, extract, {
        "summary": {"countries": 2, "extracted": 1, "unchanged": 1, "failed": 0, "bytes": 3000, "warnings": 1,
                    "planet_version": "20260920T000000Z", "planet_data_timestamp": "2026-09-19T00:00:00Z"},
        "countries": [{"path": "europe/aa", "status": "extracted", "size_bytes": 3000, "counts": {"nodes": 5, "ways": 1, "relations": 0},
                       "bbox": [0, 50, 1, 51], "warnings": ["The extract is not sorted."]},
                      {"path": "europe/bb", "status": "unchanged"}],
        "record_ids": [], "asset_ids": []})
    extract_id = extract.id
    html = client.get(f"/build/runs/{extract_id}").get_data(as_text=True)
    assert "europe/aa" in html and "unchanged" in html and "not sorted" in html and "from planet 20260920T000000Z" in html


def test_cards_show_a_status_indicator_and_a_legend(client, cfg):
    html = client.get(f"/build/?config={cfg.id}").get_data(as_text=True)
    assert "stage-legend" in html and "outdated, run again" in html
    assert 'class="stage-dot dot-todo"' in _card(html, "Download planet")  # nothing downloaded yet
    assert 'class="stage-dot dot-blocked"' in _card(html, "Extract countries")
    assert "No approved planet" in _card(html, "Extract countries")


def test_running_review_and_failed_indicators_link_to_the_run(client, cfg):
    config_id = cfg.id
    session = SessionLocal()
    running = runs.create_run(session, "polygons", config_id)
    running_id = running.id
    html = client.get(f"/build/?config={config_id}").get_data(as_text=True)
    card = _card_any(html, "Region polygons")
    assert f'href="/build/runs/{running_id}"' in card and "dot-running" in card
    session = SessionLocal()  # the request tore the previous scoped session down
    running = runs.get_run(session, running_id)
    runs.mark_running(session, running)
    runs.finish(session, running, {"polygons": [], "summary": None})
    card = _card_any(client.get(f"/build/?config={config_id}").get_data(as_text=True), "Region polygons")
    assert f'href="/build/runs/{running_id}"' in card and "dot-review" in card
    todo = _card_any(client.get(f"/build/?config={config_id}").get_data(as_text=True), "Download planet")
    assert "<a " not in todo.split("</h3>")[0] and "dot-todo" in todo  # no run: a plain indicator, not a link


def _card_any(html, title):
    return next(s for s in html.split("stage-card")[1:] if f"</a> {title}</h3>" in s or f"</span> {title}</h3>" in s).split("</section>")[0]
