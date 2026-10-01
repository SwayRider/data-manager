import json

import pytest

from datamanager.config import config
from datamanager.errors import ValidationError
from datamanager.models import Country, RegionOverlap
from datamanager.services import config_profiles as profiles
from datamanager.services import regions as svc

# Squares (lon0, lat0, lon1, lat1) at ~50°N: 1° lon ≈ 71 km, so "near" is within 100 km.
BOXES = {
    "aa": (0, 50, 1, 51),      # core
    "bb": (1.15, 50, 2.15, 51),  # ~11 km east: overlap and (10 km buffers) border
    "cc": (10, 50, 11, 51),    # far
}


def _write(iso):
    x0, y0, x1, y1 = BOXES[iso]
    path = config.DATA_ROOT + f"/geo-{iso}.geojson"
    with open(path, "w") as f:
        json.dump({"type": "Polygon", "coordinates": [[[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]]}, f)
    return f"geo-{iso}.geojson"


@pytest.fixture()
def cfg(db_session):
    for iso in ("aa", "bb", "cc"):
        db_session.add(Country(
            iso2=iso, name=iso.upper(), ne_geometry_ref=_write(iso), bbox_json=[0, 0, 1, 1],
            geofabrik_path=None if iso == "bb" else f"x/{iso}", wof_code=iso, srtm_bbox_json=[0, 1, 0, 1],
        ))
    db_session.commit()
    profile = profiles.create_profile(db_session, "dev")
    region = svc.create_region(db_session, profile.id, "r1")
    svc.assign_country(db_session, region, "aa")
    return profile, region


def _effective(db_session, profile, region):
    return {o["iso2"]: o for o in svc.overlap_summary(db_session, profile.id)["regions"][str(region.id)]["effective"]}


def test_near_country_detected_far_not_and_needs_config(db_session, cfg):
    profile, region = cfg
    eff = _effective(db_session, profile, region)
    assert set(eff) == {"bb"} and eff["bb"]["source"] == "auto"
    summary = svc.overlap_summary(db_session, profile.id)["regions"][str(region.id)]
    assert summary["needs_config"] == ["bb"]  # bb has no geofabrik path


def test_exclude_reset_and_force(db_session, cfg):
    profile, region = cfg
    svc.set_overlap_override(db_session, region, "bb", "exclude")
    assert _effective(db_session, profile, region) == {}
    assert svc.overlap_summary(db_session, profile.id)["regions"][str(region.id)]["excluded"] == ["bb"]
    svc.clear_overlap_override(db_session, region, "bb")
    assert set(_effective(db_session, profile, region)) == {"bb"}
    svc.set_overlap_override(db_session, region, "cc", "include")
    eff = _effective(db_session, profile, region)
    assert eff["cc"]["source"] == "forced"
    svc.set_overlap_override(db_session, region, "cc", "exclude")  # excluding a forced one drops it
    assert set(_effective(db_session, profile, region)) == {"bb"}


def test_override_validation(db_session, cfg):
    _, region = cfg
    with pytest.raises(ValidationError, match="core country"):
        svc.set_overlap_override(db_session, region, "aa", "exclude")
    with pytest.raises(ValidationError, match="not in the overlap"):
        svc.set_overlap_override(db_session, region, "cc", "exclude")
    with pytest.raises(ValidationError):
        svc.set_overlap_override(db_session, region, "cc", "bogus")
    with pytest.raises(ValidationError):
        svc.set_overlap_override(db_session, region, "zz", "include")


def test_border_pairs_and_other_region_core_is_overlap(db_session, cfg):
    profile, region = cfg
    r2 = svc.create_region(db_session, profile.id, "r2")
    svc.assign_country(db_session, r2, "cc")
    assert svc.overlap_summary(db_session, profile.id)["borders"] == []
    db_session.get(Country, "bb").geofabrik_path = "x/bb"
    db_session.commit()
    r3 = svc.create_region(db_session, profile.id, "r3")
    svc.assign_country(db_session, r3, "bb")
    summary = svc.overlap_summary(db_session, profile.id)
    assert ["r1", "r3"] in summary["borders"]
    assert "bb" in {o["iso2"] for o in summary["regions"][str(region.id)]["effective"]}  # core elsewhere


def test_region_buffer_and_cascade(db_session, cfg):
    _, region = cfg
    assert svc.region_buffer(db_session, region)["type"] in ("Polygon", "MultiPolygon")
    svc.set_overlap_override(db_session, region, "cc", "include")
    svc.delete_region(db_session, region)
    assert db_session.query(RegionOverlap).count() == 0


def _rows(db_session, region):
    return {(o.country_iso, o.mode) for o in db_session.query(RegionOverlap).filter_by(region_id=region.id)}


def test_auto_overlap_is_stored_and_reevaluated_on_core_changes(db_session, cfg):
    profile, region = cfg
    assert _rows(db_session, region) == {("bb", "auto")}  # stored when the core was set
    assert svc.effective_overlap(region) == ["bb"]

    # bb becomes core: it leaves the overlap
    db_session.get(Country, "bb").geofabrik_path = "x/bb"
    db_session.commit()
    svc.assign_country(db_session, region, "bb")
    assert _rows(db_session, region) == set()

    # core member removed: overlap follows the new core (aa is now near bb)
    svc.remove_country(db_session, region, "aa")
    assert _rows(db_session, region) == {("aa", "auto")}
    svc.remove_country(db_session, region, "bb")
    assert _rows(db_session, region) == set()  # empty core -> no overlap
    assert region.overlap_evaluated_at is not None


def test_overrides_survive_reevaluation_until_meaningless(db_session, cfg):
    profile, region = cfg
    svc.set_overlap_override(db_session, region, "bb", "exclude")
    svc.set_overlap_override(db_session, region, "cc", "include")
    svc.evaluate_overlap(db_session, region)
    assert _rows(db_session, region) == {("bb", "exclude"), ("cc", "include")}
    # removing the core makes the exclusion stale (bb no longer detected); the forced one stays
    svc.remove_country(db_session, region, "aa")
    assert _rows(db_session, region) == {("cc", "include")}


def test_unevaluated_region_is_evaluated_lazily(db_session, cfg):
    profile, region = cfg
    db_session.query(RegionOverlap).delete()
    region.overlap_evaluated_at = None
    db_session.commit()
    assert set(_effective(db_session, profile, region)) == {"bb"}
    assert svc.evaluate_all_overlap(db_session, only_missing=True) == 0


def test_core_ignores_far_overseas_parts(db_session):
    from shapely.geometry import MultiPolygon, box

    from datamanager.services import overlap

    home, near, far = box(0, 50, 1, 51), box(3, 50, 4, 51), box(-60, 4, -59, 5)
    core = overlap.mainland(MultiPolygon([home, near, far]))
    assert core.intersects(near) and core.intersects(home) and not core.intersects(far)
