import json

import pytest

from datamanager.db import SessionLocal
from datamanager.services import config_profiles as profiles

HX = {"HX-Request": "true"}


@pytest.fixture()
def cid(client):
    return profiles.create_profile(SessionLocal(), "dev").id


def test_tab_full_and_partial(client, cid):
    url = f"/configure/{cid}/tabs/style"
    full = client.get(url).get_data(as_text=True)
    part = client.get(url, headers=HX).get_data(as_text=True)
    assert "<html" in full and "<html" not in part
    assert "Light (default)" in part and "Black (OLED)" in part and "label_town" in part and "style-map" in part


def test_save_and_reload(client, cid):
    url = f"/configure/{cid}/style/settings"
    html = client.post(url, data={"light_style": "white", "dark_style": "dark", "label_town": "7"}).get_data(as_text=True)
    assert 'value="7"' in html and 'value="white" selected' in html


def test_invalid_keeps_input_and_is_422(client, cid):
    r = client.post(f"/configure/{cid}/style/settings", data={"light_style": "light", "dark_style": "dark", "label_town": "30"})
    html = r.get_data(as_text=True)
    assert r.status_code == 422 and "between 0 and 22" in html and 'value="30"' in html


def test_preview_without_tiles_and_downloads(client, cid):
    info = client.get(f"/configure/{cid}/style/preview/light").get_json()
    assert info["layers"]["places_locality_town"] == ["town"] and info["style"]["sources"]["protomaps"]["url"]
    assert info["tiles_available"] is False
    assert "protomaps.github.io" not in info["style"]["glyphs"] + info["style"]["sprite"]  # vendored copies, works offline
    glyphs = info["style"]["glyphs"].replace("{fontstack}", "Noto Sans Regular").replace("{range}", "0-255")
    assert client.get(glyphs.split("localhost", 1)[-1]).status_code == 200
    assert client.get(info["style"]["sprite"].split("localhost", 1)[-1] + ".json").status_code == 200
    assert client.get(f"/configure/{cid}/style/preview/nope").status_code == 404
    client.post(f"/configure/{cid}/style/settings", data={"light_style": "light", "dark_style": "dark", "label_town": "7"})
    light = client.get(f"/configure/{cid}/style-light.json?tiles_url=pmtiles://https://t/x.pmtiles")
    assert "attachment" in light.headers["Content-Disposition"]
    style = json.loads(light.get_data(as_text=True))
    assert style["sources"]["protomaps"]["url"] == "pmtiles://https://t/x.pmtiles"
    assert next(l for l in style["layers"] if l["id"] == "places_locality_town")["minzoom"] == 7
    assert json.loads(client.get(f"/configure/{cid}/style-dark.json").get_data(as_text=True))["layers"]
    assert client.get("/configure/999/style-light.json").status_code == 404


def test_preview_serves_the_approved_tiles_with_range_requests(client, cid, tmp_path):
    from datamanager.services import downloads
    from tests.test_download_tiles_stage import make_pmtiles

    assert client.get("/configure/style/tiles.pmtiles").status_code == 404
    data = make_pmtiles()
    path = tmp_path / "20260930.pmtiles"
    path.write_bytes(data)
    session = SessionLocal()
    record = downloads.register_file(session, "tiles:planet", str(path))
    record.status = "approved"
    session.commit()

    info = client.get(f"/configure/{cid}/style/preview/light").get_json()
    assert info["tiles_available"] and info["style"]["sources"]["protomaps"]["url"].endswith("/configure/style/tiles.pmtiles")
    assert info["style"]["sources"]["protomaps"]["url"].startswith("pmtiles://http")
    part = client.get("/configure/style/tiles.pmtiles", headers={"Range": "bytes=0-6"})
    assert part.status_code == 206 and part.get_data() == b"PMTiles"
