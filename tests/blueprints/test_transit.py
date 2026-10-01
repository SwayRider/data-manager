import json
from types import SimpleNamespace

import pytest

from datamanager.config import config
from datamanager.db import SessionLocal
from datamanager.models import Country
from datamanager.services import config_profiles as profiles
from datamanager.services import regions as svc

HX = {"HX-Request": "true"}


@pytest.fixture()
def cfg(client):
    s = SessionLocal()
    with open(config.DATA_ROOT + "/geo-aa.geojson", "w") as f:
        json.dump({"type": "Polygon", "coordinates": [[[0, 50], [1, 50], [1, 51], [0, 51], [0, 50]]]}, f)
    s.add(Country(iso2="aa", name="Aa", ne_geometry_ref="geo-aa.geojson", bbox_json=[0, 0, 1, 1],
                  geofabrik_path="europe/aa", wof_code="aa", srtm_bbox_json=[50, 51, 0, 1]))
    s.commit()
    profile = profiles.create_profile(s, "dev")
    region = svc.create_region(s, profile.id, "r1")
    svc.assign_country(s, region, "aa")
    return SimpleNamespace(id=profile.id, region_id=region.id)


def _add(client, cfg, **data):
    return client.post(f"/configure/{cfg.id}/regions/{cfg.region_id}/gtfs", data=data)


def test_transit_tab_full_and_partial(client, cfg):
    url = f"/configure/{cfg.id}/tabs/transit"
    full = client.get(url).get_data(as_text=True)
    part = client.get(url, headers=HX).get_data(as_text=True)
    assert "<html" in full and "<html" not in part
    assert "0 feeds" in part and "Add feed" in part
    assert "/tabs/transit" in full  # nav entry


def test_add_and_show_on_resolved_tab(client, cfg):
    r = _add(client, cfg, url="https://a.example/g.zip", label="A")
    html = r.get_data(as_text=True)
    assert r.status_code == 200 and "1 feed<" in html and "https://a.example/g.zip" in html and "<html" not in html
    resolved = client.get(f"/configure/{cfg.id}/tabs/resolved").get_data(as_text=True)
    assert "https://a.example/g.zip" in resolved
    assert "gtfs_feeds" in client.get(f"/configure/{cfg.id}/resolved.yml").get_data(as_text=True)


def test_invalid_add_is_422_and_keeps_input(client, cfg):
    r = _add(client, cfg, url="ftp://x/y", label="Keep me")
    html = r.get_data(as_text=True)
    assert r.status_code == 422 and "http://" in html and 'value="ftp://x/y"' in html and 'value="Keep me"' in html


def test_delete_and_404s(client, cfg):
    _add(client, cfg, url="https://a.example/g.zip")
    feed_id = svc.get_region(SessionLocal(), cfg.id, cfg.region_id).gtfs_feeds[0].id
    base = f"/configure/{cfg.id}/regions/{cfg.region_id}/gtfs"
    assert client.post(f"{base}/{feed_id + 99}/delete").status_code == 404
    other = profiles.create_profile(SessionLocal(), "other").id
    assert client.post(f"/configure/{other}/regions/{cfg.region_id}/gtfs/{feed_id}/delete").status_code == 404
    assert client.post(f"/configure/{other}/regions/{cfg.region_id}/gtfs", data={"url": "https://z/g"}).status_code == 404
    html = client.post(f"{base}/{feed_id}/delete").get_data(as_text=True)
    assert "0 feeds" in html
