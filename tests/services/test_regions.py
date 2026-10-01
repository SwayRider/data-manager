import pytest

from datamanager.errors import ValidationError
from datamanager.models import Country, RegionCountry
from datamanager.services import config_profiles as profiles
from datamanager.services import regions as svc


def _country(session, iso, curated=True):
    session.add(Country(
        iso2=iso, name=iso.upper(), ne_geometry_ref="x", bbox_json=[0, 0, 1, 1],
        geofabrik_path=f"europe/{iso}" if curated else None, wof_code=iso, srtm_bbox_json=[0, 1, 0, 1],
    ))
    session.commit()


@pytest.fixture()
def cfg(db_session):
    for iso in ("be", "nl"):
        _country(db_session, iso)
    _country(db_session, "ch", curated=False)
    return profiles.create_profile(db_session, "dev")


def test_create_assigns_distinct_palette_colors(db_session, cfg):
    a = svc.create_region(db_session, cfg.id, "benelux")
    b = svc.create_region(db_session, cfg.id, "france")
    assert a.color != b.color and a.color in svc.PALETTE


def test_name_rules_and_color_validation(db_session, cfg):
    svc.create_region(db_session, cfg.id, "Benelux")
    with pytest.raises(ValidationError):
        svc.create_region(db_session, cfg.id, "benelux")  # NOCASE duplicate
    with pytest.raises(ValidationError):
        svc.create_region(db_session, cfg.id, "  ")
    with pytest.raises(ValidationError) as exc:
        svc.create_region(db_session, cfg.id, "x", color="red")
    assert exc.value.details["field"] == "color"
    other = profiles.create_profile(db_session, "other")
    svc.create_region(db_session, other.id, "benelux")  # same name, other config


def test_update_region(db_session, cfg):
    r = svc.create_region(db_session, cfg.id, "a")
    svc.update_region(db_session, r, "b", "#ABCDEF")
    assert (r.name, r.color) == ("b", "#abcdef")


def test_assign_remove_and_rules(db_session, cfg):
    r1 = svc.create_region(db_session, cfg.id, "r1")
    r2 = svc.create_region(db_session, cfg.id, "r2")
    svc.assign_country(db_session, r1, "BE")
    svc.assign_country(db_session, r1, "be")  # idempotent
    assert list(svc.country_assignments(db_session, cfg.id)) == ["be"]
    with pytest.raises(ValidationError, match="already in region r1"):
        svc.assign_country(db_session, r2, "be")
    with pytest.raises(ValidationError, match="not configured"):
        svc.assign_country(db_session, r1, "ch")
    svc.remove_country(db_session, r1, "be")
    assert svc.country_assignments(db_session, cfg.id) == {}
    svc.assign_country(db_session, r2, "be")


def test_same_country_in_different_configs(db_session, cfg):
    other = profiles.create_profile(db_session, "other")
    svc.assign_country(db_session, svc.create_region(db_session, cfg.id, "r"), "be")
    svc.assign_country(db_session, svc.create_region(db_session, other.id, "r"), "be")


def test_cascades(db_session, cfg):
    r = svc.create_region(db_session, cfg.id, "r")
    svc.assign_country(db_session, r, "be")
    svc.delete_region(db_session, r)
    assert db_session.query(RegionCountry).count() == 0
    r = svc.create_region(db_session, cfg.id, "r")
    svc.assign_country(db_session, r, "nl")
    profiles.delete_profile(db_session, cfg)
    assert db_session.query(RegionCountry).count() == 0
