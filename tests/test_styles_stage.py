import json

import pytest

from datamanager.db import SessionLocal
from datamanager.jobs import tasks
from datamanager.services import assets, map_styles, runs
from datamanager.services import config_profiles as profiles
from datamanager.stages import status as stage_status


@pytest.fixture()
def cfg(app):
    return profiles.create_profile(SessionLocal(), "dev")


def _run(profile_id):
    session = SessionLocal()
    run = runs.create_run(session, "styles", profile_id)
    return run, tasks.run_stage(run.id)


def test_stage_writes_both_styles_and_awaits_review(cfg):
    run, result = _run(cfg.id)
    session = SessionLocal()
    session.refresh(run)
    assert result["status"] == "awaiting_review"
    stored = {a.name: a for a in assets.for_run(session, run.id)}
    assert set(stored) == {"style-light", "style-dark"} and {a.status for a in stored.values()} == {"produced"}
    light = json.loads(assets.abs_path(stored["style-light"]).read_text())
    assert light["version"] == 8 and map_styles.SOURCE in light["sources"]
    assert stored["style-dark"].meta_json["base_style"] == "dark"
    assert [e["layers"] > 50 for e in run.report_json["styles"]] == [True, True]
    assert any("placeholder" in w for w in run.report_json["warnings"])  # default Public URL is the example one


def test_label_zoom_override_reaches_the_file(cfg):
    session = SessionLocal()
    map_styles.save_settings(session, cfg.id, "light", "dark", {"village": 14})
    run, _ = _run(cfg.id)
    asset = next(a for a in assets.for_run(session, run.id) if a.name == "style-light")
    layers = {l["id"]: l for l in json.loads(assets.abs_path(asset).read_text())["layers"]}
    assert layers["places_locality_village"]["minzoom"] == 14


def test_status_follows_the_style_settings(cfg):
    session = SessionLocal()
    resolved = {"regions": [], "border_regions": []}
    assert stage_status.compute(session, cfg.id, resolved, {})["styles"].state == "todo"
    run, _ = _run(cfg.id)
    runs.approve(session, run, "")
    assert stage_status.compute(session, cfg.id, resolved, {})["styles"].state == "ok"
    map_styles.save_settings(session, cfg.id, "classic", "dark", {})
    assert stage_status.compute(session, cfg.id, resolved, {})["styles"].state == "outdated"
