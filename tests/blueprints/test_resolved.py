import json
from types import SimpleNamespace

import pytest
import yaml

from datamanager.config import config
from datamanager.db import SessionLocal
from datamanager.models import Country
from datamanager.services import address_sources as oa_service
from datamanager.services import config_profiles as profiles
from datamanager.services import regions as svc

HX = {"HX-Request": "true"}


@pytest.fixture()
def cfg(client):
    s = SessionLocal()
    with open(config.DATA_ROOT + "/geo-aa.geojson", "w") as f:
        json.dump({"type": "Polygon", "coordinates": [[[0, 50], [1, 50], [1, 51], [0, 51], [0, 50]]]}, f)
    country = Country(iso2="aa", name="Aa", ne_geometry_ref="geo-aa.geojson", bbox_json=[0, 0, 1, 1],
                      geofabrik_path="europe/aa", wof_code="aa", srtm_bbox_json=[50, 51, 0, 1])
    s.add(country)
    oa_service.set_openaddresses_files(country, ["aa/one", "aa/two"])
    s.commit()
    profile = profiles.create_profile(s, "dev")
    region = svc.create_region(s, profile.id, "r1")
    svc.assign_country(s, region, "aa")
    return SimpleNamespace(id=profile.id, region_id=region.id)


def test_resolved_tab_full_and_partial(client, cfg):
    url = f"/configure/{cfg.id}/tabs/resolved"
    full = client.get(url).get_data(as_text=True)
    part = client.get(url, headers=HX).get_data(as_text=True)
    assert "<html" in full and "<html" not in part
    assert "2/2 files" in part and "europe/aa" in part and "config hash" in part
    assert 'class="tabs"' in full  # two tabs -> nav visible


def test_toggle_openaddresses_file_rerenders_card(client, cfg):
    url = f"/configure/{cfg.id}/regions/{cfg.region_id}/openaddresses/aa"
    html = client.post(url, data={"file": "aa/one", "included": "0"}).get_data(as_text=True)
    assert "1/2 files" in html and "<html" not in html
    assert "2/2 files" in client.post(url, data={"file": "aa/one", "included": "1"}).get_data(as_text=True)


def test_toggle_validation_and_other_config_404(client, cfg):
    url = f"/configure/{cfg.id}/regions/{cfg.region_id}/openaddresses/aa"
    assert client.post(url, data={"file": "nope", "included": "0"}).status_code == 422
    other = profiles.create_profile(SessionLocal(), "other").id
    assert client.post(f"/configure/{other}/regions/{cfg.region_id}/openaddresses/aa",
                       data={"file": "aa/one", "included": "0"}).status_code == 404


def test_downloads(client, cfg):
    y = client.get(f"/configure/{cfg.id}/resolved.yml")
    assert "attachment" in y.headers["Content-Disposition"]
    data = yaml.safe_load(y.get_data(as_text=True))
    assert data["regions"][0]["r1"]["core"]["osm"] == ["europe/aa"]
    j = client.get(f"/configure/{cfg.id}/resolved.json").get_json()
    assert j["regions"][0]["name"] == "r1"
