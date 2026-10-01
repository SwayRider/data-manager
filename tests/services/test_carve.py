import json

import pytest

from datamanager.config import config
from datamanager.errors import ValidationError
from datamanager.models import Country
from datamanager.services import carve
from datamanager.services import config_profiles as profiles
from datamanager.services import regions as region_service

# aa is 2° wide; bb starts 0.7° (~50 km) east of it, inside the 100 km zone (~1.4° at 50°N), so it is
# overlap until aa is carved to its western half (zone then ends near lon 2.4, before bb).
BOXES = {"aa": (0, 50, 2, 51), "bb": (2.7, 50, 3.7, 51)}
WEST_HALF = {"type": "Polygon", "coordinates": [[[-1, 49], [1, 49], [1, 52], [-1, 52], [-1, 49]]]}


def _write(iso):
    x0, y0, x1, y1 = BOXES[iso]
    with open(config.DATA_ROOT + f"/geo-{iso}.geojson", "w") as f:
        json.dump({"type": "Polygon", "coordinates": [[[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]]}, f)
    return f"geo-{iso}.geojson"


@pytest.fixture()
def setup(db_session):
    for iso in BOXES:
        db_session.add(Country(iso2=iso, name=iso.upper(), ne_geometry_ref=_write(iso), bbox_json=[0, 0, 1, 1],
                               geofabrik_path=f"europe/{iso}", wof_code=iso.upper(), srtm_bbox_json=[50, 51, 0, 1]))
    db_session.commit()
    profile = profiles.create_profile(db_session, "dev")
    region = region_service.create_region(db_session, profile.id, "r1")
    region_service.assign_country(db_session, region, "aa")
    return profile, region


def test_carve_changes_overlap_and_is_reversible(db_session, setup):
    profile, region = setup
    assert region_service.effective_overlap(region) == ["bb"]

    carve.set_carve(db_session, profile.id, "aa", WEST_HALF)
    db_session.refresh(region)
    assert region_service.effective_overlap(region) == []  # the zone now starts at the kept part

    carve.clear_carve(db_session, profile.id, "aa")
    db_session.refresh(region)
    assert region_service.effective_overlap(region) == ["bb"]


def test_carve_display_geometry(db_session, setup):
    profile, _ = setup
    carve.set_carve(db_session, profile.id, "aa", WEST_HALF)
    kept = carve.kept_geojson(db_session, profile.id)["aa"]
    xs = [x for ring in kept["coordinates"] for x, _ in ring]
    assert min(xs) == 0 and max(xs) == 1


def test_carve_is_per_configuration(db_session, setup):
    profile, _ = setup
    other = profiles.create_profile(db_session, "other")
    carve.set_carve(db_session, profile.id, "aa", WEST_HALF)
    assert carve.kept_geojson(db_session, other.id) == {}


def test_carve_validation(db_session, setup):
    profile, _ = setup
    far = {"type": "Polygon", "coordinates": [[[20, 20], [21, 20], [21, 21], [20, 21], [20, 20]]]}
    with pytest.raises(ValidationError, match="does not cover"):
        carve.set_carve(db_session, profile.id, "aa", far)
    with pytest.raises(ValidationError, match="polygon"):
        carve.set_carve(db_session, profile.id, "aa", {"type": "Point", "coordinates": [0, 0]})
    with pytest.raises(ValidationError):
        carve.set_carve(db_session, profile.id, "aa", None)
    with pytest.raises(ValidationError, match="Unknown"):
        carve.set_carve(db_session, profile.id, "zz", WEST_HALF)


def test_carve_is_in_resolved_config_and_hash(db_session, setup):
    from datamanager.services import resolve

    profile, _ = setup
    before = resolve.resolve_config(db_session, profile.id).regions[0]
    assert before.core[0].carve is None
    carve.set_carve(db_session, profile.id, "aa", WEST_HALF)
    after = resolve.resolve_config(db_session, profile.id).regions[0]
    assert after.core[0].carve is not None and after.hash != before.hash
