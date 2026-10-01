from types import SimpleNamespace

import pytest

from datamanager.db import SessionLocal
from datamanager.services import config_profiles as svc

HX = {"HX-Request": "true"}


@pytest.fixture()
def profile(client):
    created = svc.create_profile(SessionLocal(), "dev-mini", "Small setup")
    # Requests tear down the scoped session, so hand tests a detached snapshot.
    return SimpleNamespace(id=created.id)


def test_no_profiles_shows_empty_state(client):
    html = client.get("/configure/").get_data(as_text=True)
    assert "No configurations yet" in html
    assert "New Configuration" in html


def test_index_redirects_to_first_then_cookie(client, profile):
    other = svc.create_profile(SessionLocal(), "aaa")  # sorts first
    assert client.get("/configure/").headers["Location"].endswith(f"/configure/{other.id}/")
    client.set_cookie("last_config", str(profile.id))
    assert client.get("/configure/").headers["Location"].endswith(f"/configure/{profile.id}/")


def test_dropdown_lists_profiles_and_marks_current(client, profile):
    svc.create_profile(SessionLocal(), "other")
    html = client.get(f"/configure/{profile.id}/").get_data(as_text=True)
    assert ">dev-mini</option>" in html and ">other</option>" in html
    assert "selected" in html and "Small setup" in html


def test_full_page_vs_htmx_partial(client, profile):
    url = f"/configure/{profile.id}/tabs/map"
    full = client.get(url).get_data(as_text=True)
    part = client.get(url, headers=HX).get_data(as_text=True)
    assert "<html" in full and "<html" not in part
    assert "region-panel" in part


def test_unknown_tab_or_config_404(client, profile):
    assert client.get(f"/configure/{profile.id}/tabs/nope").status_code == 404
    assert client.get("/configure/999/").status_code == 404


def test_create_success_redirects_and_persists(client):
    response = client.post("/configure/new", data={"name": "prod", "description": "d"}, headers=HX)
    assert response.status_code == 200
    (created,) = svc.list_profiles(SessionLocal())
    assert response.headers["HX-Redirect"].endswith(f"/configure/{created.id}/")


def test_create_validation_errors(client, profile):
    empty = client.post("/configure/new", data={"name": " "}, headers=HX)
    assert empty.status_code == 422 and "Name is required" in empty.get_data(as_text=True)
    dup = client.post("/configure/new", data={"name": "DEV-MINI"}, headers=HX)
    assert dup.status_code == 422 and "already exists" in dup.get_data(as_text=True)


def test_new_and_edit_forms(client, profile):
    assert "<dialog" in client.get("/configure/new").get_data(as_text=True)
    assert 'value="dev-mini"' in client.get(f"/configure/{profile.id}/edit").get_data(as_text=True)


def test_edit_delete_duplicate(client, profile):
    r = client.post(f"/configure/{profile.id}/edit", data={"name": "renamed", "description": "x"}, headers=HX)
    assert r.status_code == 200
    r = client.post(f"/configure/{profile.id}/duplicate", headers=HX)
    assert "HX-Redirect" in r.headers
    assert [p.name for p in svc.list_profiles(SessionLocal())] == ["renamed", "renamed (copy)"]
    r = client.post(f"/configure/{profile.id}/delete", headers=HX)
    assert r.headers["HX-Redirect"].endswith("/configure/")
    assert [p.name for p in svc.list_profiles(SessionLocal())] == ["renamed (copy)"]
