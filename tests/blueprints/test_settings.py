import stat

import pytest

from datamanager import tools as registry
from datamanager.blueprints.settings import routes as settings_routes
from datamanager.db import SessionLocal
from datamanager.services import config_profiles as profiles
from datamanager.services import settings as settings_service
from datamanager.services import tools as tools_service

HX = {"HX-Request": "true"}


@pytest.fixture(autouse=True)
def fresh_cache():
    tools_service.clear_cache()
    yield
    tools_service.clear_cache()


@pytest.fixture()
def fake_tool(monkeypatch, tmp_path):
    tool = registry.ToolDef("faketool", "Fake tool", "tests", ("faketool-xyz",), apt="faketool-pkg")
    monkeypatch.setattr(tools_service, "TOOLS", (tool,))
    monkeypatch.setattr(tools_service, "BY_KEY", {"faketool": tool})
    monkeypatch.setattr(settings_routes, "BY_KEY", {"faketool": tool})
    bindir = tmp_path / "bin"
    bindir.mkdir()
    monkeypatch.setenv("PATH", str(bindir))
    return bindir


def _install(bindir):
    script = bindir / "faketool-xyz"
    script.write_text("#!/bin/sh\necho 'faketool 1.2.3'\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)


def test_page_shows_settings_and_tools(client):
    html = client.get("/settings/").get_data(as_text=True)
    assert "Global settings" in html and 'name="public.tiles_url"' in html and "Required tools" in html
    assert "Bootstrap configuration" in html and "osmium-tool" in html


def test_save_reload_and_reset(client):
    r = client.post("/settings/global", data={"run.max_workers": "6", "public.sprite_url": "https://s.test/sp"})
    html = r.get_data(as_text=True)
    assert r.status_code == 200 and "Saved." in html and 'value="6"' in html and "changed" in html
    assert 'value="6"' in client.get("/settings/").get_data(as_text=True)
    html = client.post("/settings/global", data={"run.max_workers": ""}).get_data(as_text=True)
    assert 'value="2"' in html


def test_invalid_is_422_and_keeps_input(client):
    r = client.post("/settings/global", data={"run.max_workers": "500"})
    html = r.get_data(as_text=True)
    assert r.status_code == 422 and "between 1 and 64" in html and 'value="500"' in html


def test_style_download_uses_global_public_urls_and_query_wins(client):
    cid = profiles.create_profile(SessionLocal(), "dev").id
    url = f"/configure/{cid}/style-light.json"
    default = client.get(url).get_json()
    assert "pmtiles://" in str(default["sources"])

    settings_service.save(SessionLocal(), {"public.tiles_url": "https://tiles.test/omt", "public.glyphs_url": "https://tiles.test/f/{fontstack}/{range}.pbf"})
    style = client.get(url).get_json()
    assert style["sources"]["protomaps"]["url"] == "https://tiles.test/omt"
    assert style["glyphs"].startswith("https://tiles.test/f/")
    assert client.get(url + "?tiles_url=https://q.test/x").get_json()["sources"]["protomaps"]["url"] == "https://q.test/x"


def test_redetect_picks_up_installed_tool_and_nav_warning(client, fake_tool):
    html = client.get("/settings/").get_data(as_text=True)
    assert "not found" in html and "sudo apt install faketool-pkg" in html and "1 required tool missing" in html
    assert 'title="Required tools not found: Fake tool"' in html

    _install(fake_tool)
    assert "not found" in client.get("/settings/").get_data(as_text=True)  # still cached
    part = client.post("/settings/tools/faketool/detect").get_data(as_text=True)
    assert "<html" not in part and "1.2.3" in part and "all required tools found" in part
    assert "Required tools not found" not in client.get("/settings/").get_data(as_text=True)

    (fake_tool / "faketool-xyz").unlink()
    assert "not found" in client.post("/settings/tools/detect").get_data(as_text=True)


def test_set_tool_path_and_errors(client, fake_tool, tmp_path):
    custom = tmp_path / "elsewhere"
    custom.write_text("#!/bin/sh\necho 'custom 9.9'\n")
    custom.chmod(custom.stat().st_mode | stat.S_IXUSR)
    html = client.post("/settings/tools/faketool/path", data={"path": str(custom)}).get_data(as_text=True)
    assert "9.9" in html and str(custom) in html
    assert "does not exist" in client.post("/settings/tools/faketool/path", data={"path": "/no/such/bin"}).get_data(as_text=True)
    assert client.post("/settings/tools/nope/detect").status_code == 404
    assert client.post("/settings/tools/nope/path", data={"path": "x"}).status_code == 404
