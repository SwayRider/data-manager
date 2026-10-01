from types import SimpleNamespace

import pytest

from datamanager.db import SessionLocal
from datamanager.models import Country
from datamanager.services import config_profiles as profiles
from datamanager.services import regions as svc

HX = {"HX-Request": "true"}


@pytest.fixture()
def cfg(client):
    s = SessionLocal()
    s.add(Country(iso2="be", name="Belgium", ne_geometry_ref="x", bbox_json=[0, 0, 1, 1],
                  geofabrik_path="europe/belgium", wof_code="be", srtm_bbox_json=[0, 1, 0, 1]))
    s.commit()
    return SimpleNamespace(id=profiles.create_profile(s, "dev").id)


def test_map_tab_lists_regions_and_add_button(client, cfg):
    svc.create_region(SessionLocal(), cfg.id, "benelux")
    html = client.get(f"/configure/{cfg.id}/").get_data(as_text=True)
    assert "benelux" in html and 'class="swatch"' in html
    assert f"/configure/{cfg.id}/regions/new" in html


def test_create_edit_delete_region(client, cfg):
    assert "Add Region" in client.get(f"/configure/{cfg.id}/regions/new").get_data(as_text=True)
    r = client.post(f"/configure/{cfg.id}/regions/new", data={"name": "benelux", "color": "#123456"}, headers=HX)
    assert r.headers["HX-Redirect"].endswith(f"/configure/{cfg.id}/")
    region = svc.list_regions(SessionLocal(), cfg.id)[0]
    rid = region.id
    bad = client.post(f"/configure/{cfg.id}/regions/{rid}/edit", data={"name": "", "color": "#123456"}, headers=HX)
    assert bad.status_code == 422
    ok = client.post(f"/configure/{cfg.id}/regions/{rid}/edit", data={"name": "bnl", "color": "#654321"}, headers=HX)
    assert "HX-Redirect" in ok.headers
    assert svc.get_region(SessionLocal(), cfg.id, rid).name == "bnl"
    assert "HX-Redirect" in client.post(f"/configure/{cfg.id}/regions/{rid}/delete", headers=HX).headers
    assert svc.list_regions(SessionLocal(), cfg.id) == []


def test_assign_and_remove_country(client, cfg):
    rid = svc.create_region(SessionLocal(), cfg.id, "r").id
    url = f"/configure/{cfg.id}/regions/{rid}/countries/be"
    body = client.post(url).get_json()
    assert body["region"]["id"] == rid
    assert client.get(f"/configure/{cfg.id}/regions/assignments").get_json()["be"]["name"] == "r"
    assert client.delete(url).get_json()["region"] is None
    assert client.get(f"/configure/{cfg.id}/regions/assignments").get_json() == {}


def test_assign_unknown_country_is_422_and_other_config_404(client, cfg):
    rid = svc.create_region(SessionLocal(), cfg.id, "r").id
    assert client.post(f"/configure/{cfg.id}/regions/{rid}/countries/zz").status_code == 422
    other = profiles.create_profile(SessionLocal(), "other").id
    assert client.post(f"/configure/{other}/regions/{rid}/countries/be").status_code == 404


def test_overlap_routes(client, cfg):
    rid = svc.create_region(SessionLocal(), cfg.id, "r").id
    summary = client.get(f"/configure/{cfg.id}/regions/overlap").get_json()
    assert str(rid) in summary["regions"] and summary["borders"] == []
    assert client.get(f"/configure/{cfg.id}/regions/{rid}/buffer").status_code == 200
    url = f"/configure/{cfg.id}/regions/{rid}/overlap/be"
    assert client.post(url, data={"mode": "bogus"}).status_code == 422
    assert client.post(url, data={"mode": "include"}).get_json() == {"ok": True}
    assert client.delete(url).get_json() == {"ok": True}
    assert client.get(f"/configure/{cfg.id}/regions/999/buffer").status_code == 404


def test_tab_nav_lists_map_and_resolved(client, cfg):
    html = client.get(f"/configure/{cfg.id}/").get_data(as_text=True)
    assert 'class="tabs"' in html and ">Resolved</a>" in html
