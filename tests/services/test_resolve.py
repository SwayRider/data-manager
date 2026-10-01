import json

import pytest
import yaml

from datamanager.config import config
from datamanager.errors import ValidationError
from datamanager.models import Country
from datamanager.services import address_sources as oa_service
from datamanager.services import config_profiles as profiles
from datamanager.services import regions as region_service
from datamanager.services import resolve

# Squares (lon0, lat0, lon1, lat1): aa core, bb ~11 km east (overlap + border), cc far away.
BOXES = {"aa": (0, 50, 1, 51), "bb": (1.15, 50, 2.15, 51), "cc": (10, 50, 11, 51)}


def _write(iso):
    x0, y0, x1, y1 = BOXES[iso]
    with open(config.DATA_ROOT + f"/geo-{iso}.geojson", "w") as f:
        json.dump({"type": "Polygon", "coordinates": [[[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]]}, f)
    return f"geo-{iso}.geojson"


@pytest.fixture()
def setup(db_session):
    for iso in BOXES:
        country = Country(
            iso2=iso, name=iso.upper(), ne_geometry_ref=_write(iso), bbox_json=[0, 0, 1, 1],
            geofabrik_path=None if iso == "bb" else f"europe/country-{iso}", wof_code=iso.upper(),
            srtm_bbox_json=[50, 51, int(BOXES[iso][0]), int(BOXES[iso][2] + 0.99)],
        )
        db_session.add(country)
        oa_service.set_openaddresses_files(country, [f"{iso}/one", f"{iso}/two"] if iso == "aa" else [])
    db_session.commit()
    profile = profiles.create_profile(db_session, "dev")
    region = region_service.create_region(db_session, profile.id, "r1")
    region_service.assign_country(db_session, region, "aa")
    return profile, region


def test_resolves_core_overlap_srtm_and_warnings(db_session, setup):
    profile, _ = setup
    resolved = resolve.resolve_config(db_session, profile.id)
    r = resolved.regions[0]
    assert [c.iso2 for c in r.core] == ["aa"]
    assert [c.iso2 for c in r.overlap] == ["bb"]
    assert r.core[0].openaddresses == ["aa/one", "aa/two"]
    # bb has no Geofabrik path: listed, warned, and skipped from srtm
    assert list(r.srtm) == ["country-aa"]
    assert any("not configured" in w and "BB" in w for w in r.warnings)
    assert r.srtm_tiles == resolve.count_srtm_tiles([[50, 51, 0, 1]]) == 4


def test_overlap_country_with_path_is_in_srtm(db_session, setup):
    profile, _ = setup
    bb = db_session.get(Country, "bb")
    bb.geofabrik_path = "europe/country-bb"
    db_session.commit()
    r = resolve.resolve_config(db_session, profile.id).regions[0]
    assert set(r.srtm) == {"country-aa", "country-bb"}


def test_exclusions_change_files_and_hash_and_stale_ignored(db_session, setup):
    profile, region = setup
    before = resolve.resolve_config(db_session, profile.id).regions[0]
    assert before.hash == resolve.resolve_config(db_session, profile.id).regions[0].hash

    region_service.set_openaddresses_file(db_session, region, "aa", "aa/one", included=False)
    after = resolve.resolve_config(db_session, profile.id).regions[0]
    assert after.core[0].openaddresses == ["aa/two"] and after.core[0].openaddresses_all == ["aa/one", "aa/two"]
    assert after.hash != before.hash

    region_service.set_openaddresses_file(db_session, region, "aa", "aa/one", included=True)
    assert resolve.resolve_config(db_session, profile.id).regions[0].hash == before.hash

    # a file that disappears from the catalog no longer matters
    region_service.set_openaddresses_file(db_session, region, "aa", "aa/two", included=False)
    oa_service.set_openaddresses_files(db_session.get(Country, "aa"), ["aa/one"])
    db_session.commit()
    assert resolve.resolve_config(db_session, profile.id).regions[0].core[0].openaddresses == ["aa/one"]


def test_exclusion_validation(db_session, setup):
    _, region = setup
    with pytest.raises(ValidationError, match="not part"):
        region_service.set_openaddresses_file(db_session, region, "cc", "cc/one", False)
    with pytest.raises(ValidationError, match="Unknown OpenAddresses file"):
        region_service.set_openaddresses_file(db_session, region, "aa", "aa/nope", False)


def test_empty_region_warns_and_legacy_shape(db_session, setup):
    profile, _ = setup
    region_service.create_region(db_session, profile.id, "empty")
    resolved = resolve.resolve_config(db_session, profile.id)
    empty = next(r for r in resolved.regions if r.name == "empty")
    assert "No core countries" in empty.warnings

    legacy = resolve.to_legacy_dict(resolved)
    r1 = next(entry["r1"] for entry in legacy["regions"] if "r1" in entry)
    assert r1["core"] == {"osm": ["europe/country-aa"], "wof": ["AA"], "openaddresses": ["aa/one.csv", "aa/two.csv"]}
    assert r1["overlap"] == {"osm": [], "wof": [], "openaddresses": []}
    assert r1["srtm"] == [{"country-aa": [50, 51, 0, 1]}]
    yaml.safe_dump(legacy)  # serialisable
