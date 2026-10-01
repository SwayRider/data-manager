import pytest

from datamanager.errors import ValidationError
from datamanager.models import GlobalSetting
from datamanager.services import settings as svc


def test_defaults_when_nothing_saved(db_session):
    assert svc.get(db_session, "run.max_workers") == 2
    assert svc.get(db_session, "download.osm") == "https://download.geofabrik.de/"
    urls = svc.public_urls(db_session)
    assert urls["tiles_url"].startswith("pmtiles://") and "sprite" not in urls
    groups = svc.all_values(db_session)
    assert all(e["is_default"] for g in groups for e in g["settings"])


def test_save_reset_and_default_deletes_row(db_session):
    svc.save(db_session, {"run.max_workers": "6", "public.sprite_url": "https://x.test/sprite"})
    assert svc.get(db_session, "run.max_workers") == 6
    assert svc.public_urls(db_session)["sprite"] == "https://x.test/sprite"
    changed = {e["def"].key for g in svc.all_values(db_session) for e in g["settings"] if not e["is_default"]}
    assert changed == {"run.max_workers", "public.sprite_url"}

    svc.save(db_session, {"run.max_workers": "", "public.sprite_url": ""})  # blank resets
    assert db_session.query(GlobalSetting).count() == 0
    svc.save(db_session, {"run.max_workers": "2"})  # equal to the default: no row
    assert db_session.query(GlobalSetting).count() == 0


@pytest.mark.parametrize("key,value,message", [
    ("public.tiles_url", "ftp://x", "must be a URL"),
    ("public.tiles_url", "not a url", "must be a URL"),
    ("public.glyphs_url", "https://x.test/fonts", "{fontstack}"),
    ("run.max_workers", "0", "between 1 and 64"),
    ("run.max_workers", "many", "whole number"),
    ("run.java_xmx", "lots", "invalid format"),
    ("nope.key", "1", "Unknown setting"),
])
def test_validation(db_session, key, value, message):
    with pytest.raises(ValidationError, match=message):
        svc.save(db_session, {key: value})
    assert db_session.query(GlobalSetting).count() == 0


def test_invalid_value_saves_nothing_from_the_batch(db_session):
    with pytest.raises(ValidationError):
        svc.save(db_session, {"run.max_workers": "4", "run.java_xmx": "bad"})
    assert svc.get(db_session, "run.max_workers") == 2


def test_srtm_allows_s3_but_others_do_not(db_session):
    svc.save(db_session, {"download.srtm": "s3://bucket/skadi/"})
    with pytest.raises(ValidationError):
        svc.save(db_session, {"download.osm": "s3://bucket/"})


def test_planet_source_settings_have_defaults_that_are_not_geofabrik(db_session):
    planet = svc.get(db_session, "download.planet")
    assert planet.startswith("https://") and "geofabrik" not in planet and planet.endswith("planet-latest.osm.pbf")
    assert svc.get(db_session, "download.planet_keep") == 2
    assert svc.get(db_session, "download.connections") == 4
    assert svc.get(db_session, "osm.source") == "planet"
    assert "geofabrik" in svc.get(db_session, "download.country_polys")


def test_planet_settings_are_validated(db_session):
    with pytest.raises(ValidationError):
        svc.save(db_session, {"osm.source": "ftp"})
    with pytest.raises(ValidationError):
        svc.save(db_session, {"download.planet_keep": "0"})
    with pytest.raises(ValidationError):
        svc.save(db_session, {"download.connections": "99"})
    svc.save(db_session, {"download.planet": "https://planet.openstreetmap.org/pbf/planet-latest.osm.pbf", "osm.source": "geofabrik"})
    assert svc.get(db_session, "osm.source") == "geofabrik"
