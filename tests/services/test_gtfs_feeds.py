import json

import pytest
import yaml

from datamanager.config import config
from datamanager.errors import ValidationError
from datamanager.models import Country, RegionGtfsFeed
from datamanager.services import config_profiles as profiles
from datamanager.services import regions as svc
from datamanager.services import resolve


@pytest.fixture()
def region(db_session):
    with open(config.DATA_ROOT + "/geo-aa.geojson", "w") as f:
        json.dump({"type": "Polygon", "coordinates": [[[0, 50], [1, 50], [1, 51], [0, 51], [0, 50]]]}, f)
    db_session.add(Country(iso2="aa", name="Aa", ne_geometry_ref="geo-aa.geojson", bbox_json=[0, 0, 1, 1],
                           geofabrik_path="europe/aa", wof_code="AA", srtm_bbox_json=[50, 51, 0, 1]))
    db_session.commit()
    profile = profiles.create_profile(db_session, "dev")
    r = svc.create_region(db_session, profile.id, "r1")
    svc.assign_country(db_session, r, "aa")
    return r


def test_add_trims_and_lists_in_order(db_session, region):
    svc.add_gtfs_feed(db_session, region, "  https://a.example/gtfs.zip ", " NMBS ")
    svc.add_gtfs_feed(db_session, region, "http://b.example/g.zip")
    assert [(f.url, f.label) for f in region.gtfs_feeds] == [
        ("https://a.example/gtfs.zip", "NMBS"), ("http://b.example/g.zip", None)]


@pytest.mark.parametrize("url", ["", "ftp://x/y.zip", "not a url", "https://"])
def test_bad_url_rejected(db_session, region, url):
    with pytest.raises(ValidationError, match="http"):
        svc.add_gtfs_feed(db_session, region, url)


def test_duplicate_and_length_rejected(db_session, region):
    svc.add_gtfs_feed(db_session, region, "https://a.example/g.zip")
    with pytest.raises(ValidationError, match="already"):
        svc.add_gtfs_feed(db_session, region, "https://a.example/g.zip")
    with pytest.raises(ValidationError, match="Label"):
        svc.add_gtfs_feed(db_session, region, "https://c.example/g.zip", "x" * 101)
    with pytest.raises(ValidationError, match="URL"):
        svc.add_gtfs_feed(db_session, region, "https://c.example/" + "x" * 500)


def test_remove_and_foreign_feed(db_session, region):
    other = svc.create_region(db_session, region.config_profile_id, "r2")
    feed = svc.add_gtfs_feed(db_session, region, "https://a.example/g.zip")
    with pytest.raises(ValidationError, match="Unknown feed"):
        svc.remove_gtfs_feed(db_session, other, feed.id)
    svc.remove_gtfs_feed(db_session, region, feed.id)
    assert region.gtfs_feeds == []


def test_region_delete_cascades(db_session, region):
    svc.add_gtfs_feed(db_session, region, "https://a.example/g.zip")
    db_session.delete(region)
    db_session.commit()
    assert db_session.query(RegionGtfsFeed).count() == 0


def test_resolve_hash_and_legacy(db_session, region):
    cid = region.config_profile_id
    base = resolve.resolve_config(db_session, cid).regions[0]
    assert base.gtfs_feeds == []
    assert "gtfs_feeds" not in resolve.to_legacy_dict(resolve.resolve_config(db_session, cid))["regions"][0]["r1"]

    svc.add_gtfs_feed(db_session, region, "https://b.example/g.zip", "B")
    svc.add_gtfs_feed(db_session, region, "https://a.example/g.zip")
    resolved = resolve.resolve_config(db_session, cid)
    r = resolved.regions[0]
    assert [f["url"] for f in r.gtfs_feeds] == ["https://b.example/g.zip", "https://a.example/g.zip"]
    assert r.hash != base.hash

    legacy = resolve.to_legacy_dict(resolved)["regions"][0]["r1"]
    assert legacy["gtfs_feeds"] == ["https://b.example/g.zip", "https://a.example/g.zip"]
    yaml.safe_dump(resolve.to_legacy_dict(resolved))
    json.dumps(resolve.to_dict(resolved))

    # order does not change the hash; removing a feed does
    assert resolve._hash(r) == r.hash
    r.gtfs_feeds.reverse()
    assert resolve._hash(r) == r.hash
    svc.remove_gtfs_feed(db_session, region, region.gtfs_feeds[0].id)
    assert resolve.resolve_config(db_session, cid).regions[0].hash != r.hash
