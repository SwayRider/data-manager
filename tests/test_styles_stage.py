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
    assert not run.report_json["warnings"]
    assert light["sources"][map_styles.SOURCE]["tiles"] == ["{{.TilesBaseURL}}/{{.Tileset}}/{z}/{x}/{y}"]
    assert "url" not in light["sources"][map_styles.SOURCE] and light["sources"][map_styles.SOURCE]["maxzoom"] == 15
    assert light["glyphs"] == "{{.TilesBaseURL}}/fonts/{fontstack}/{range}.pbf" and light["sprite"] == "{{.TilesBaseURL}}/sprites/light"
    assert stored["style-light"].meta_json["style_id"] == "swayrider" and stored["style-light"].meta_json["version"] == 1


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


def test_version_stays_for_unchanged_content_and_goes_up_for_changes(cfg):
    session = SessionLocal()
    first, _ = _run(cfg.id)
    runs.approve(session, first, "")
    again, _ = _run(cfg.id)
    versions = lambda run: {a.meta_json["version"] for a in assets.for_run(session, run.id)}  # noqa: E731
    assert versions(first) == {1} and versions(again) == {1}  # same content, same version
    map_styles.save_settings(session, cfg.id, "classic", "dark", {})
    changed, _ = _run(cfg.id)
    assert versions(changed) == {2}
    from datamanager.services import settings as settings_service
    settings_service.save(session, {"styles.id": "other"})
    renamed, _ = _run(cfg.id)
    assert versions(renamed) == {1}  # versions count per style id


def test_validate_flags_a_missing_font_or_sprite(cfg):
    from datamanager.stages import styles as stage

    style = map_styles.release_style(SessionLocal(), cfg.id, "light")
    assert stage.validate(style) == []
    style["layers"][0].setdefault("layout", {})["text-font"] = ["Comic Sans"]
    style["sprite"] = "{{.TilesBaseURL}}/sprites/nope"
    problems = stage.validate(style)
    assert any("Comic Sans" in p for p in problems) and any("nope" in p for p in problems)
