import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

from datamanager.config import config
from datamanager.db import SessionLocal
from datamanager.models import Country
from datamanager.boundary_sources import official_polygons
from datamanager.services import address_sources as address_source_service
from datamanager.services import boundary_sources as boundary_source_service
from datamanager.services import geofabrik, openaddresses

HX = {"HX-Request": "true"}


def _add_country(iso2="ch", name="Switzerland", geofabrik_path=None, oa=()):
    ref = f"library/country-geometry/{iso2}.geojson"
    path = Path(config.DATA_ROOT) / ref
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]}))
    session = SessionLocal()
    session.add(Country(iso2=iso2, name=name, ne_geometry_ref=ref, bbox_json=[0, 0, 1, 1],
                        geofabrik_path=geofabrik_path, wof_code=iso2,
                        srtm_bbox_json=[0, 1, 0, 1]))
    session.commit()
    if oa:
        address_source_service.set_openaddresses_files(session.get(Country, iso2), list(oa))
        session.commit()


@pytest.fixture()
def geofabrik_ok(monkeypatch):
    monkeypatch.setattr(geofabrik, "extract_exists", lambda path: path != "europe/nope")
    monkeypatch.setattr(openaddresses, "find_missing", lambda files: [f for f in files if "missing" in f])


def test_geojson_empty(client):
    assert client.get("/countries/api/geojson").get_json() == {"type": "FeatureCollection", "features": []}


def test_geojson_flags_curated(client, tmp_data_root):
    _add_country("be", "Belgium", "europe/belgium")
    _add_country("ch")
    props = {f["id"]: f["properties"] for f in client.get("/countries/api/geojson").get_json()["features"]}
    assert props["be"]["curated"] is True and props["ch"]["curated"] is False


def test_details_modal_shows_fields(client, tmp_data_root):
    _add_country("be", "Belgium", "europe/belgium", ["be/a.csv", "be/b.csv"])
    html = client.get("/countries/be", headers=HX).get_data(as_text=True)
    assert "<dialog" in html and "Belgium" in html
    assert 'value="europe/belgium"' in html and "be/a.csv\nbe/b.csv" in html
    assert "europe/belgium-latest.osm.pbf" in html


def test_unknown_country_404(client):
    assert client.get("/countries/zz").status_code == 404


def test_save_configures_country_and_triggers_event(client, tmp_data_root, geofabrik_ok):
    _add_country("ch")
    r = client.post("/countries/ch", headers=HX, data={
        "geofabrik_path": " europe/switzerland/ ", "wof_code": "CH", "openaddresses_files": "ch/a.csv\n\nch/a.csv\nch/b.csv",
    })
    assert r.status_code == 200 and r.get_data() == b""
    assert json.loads(r.headers["HX-Trigger"])["country-updated"]["curated"] is True
    ch = SessionLocal().get(Country, "ch")
    assert (ch.geofabrik_path, ch.wof_code, address_source_service.openaddresses_files(ch)) == (
        "europe/switzerland", "ch", ["ch/a", "ch/b"])


@pytest.mark.parametrize("path, message", [("Europe/Bad Path", "must look like"), ("europe/nope", "Not found on Geofabrik")])
def test_save_rejects_bad_path(client, tmp_data_root, geofabrik_ok, path, message):
    _add_country("ch")
    r = client.post("/countries/ch", headers=HX, data={"geofabrik_path": path, "wof_code": "ch"})
    assert r.status_code == 422 and message in r.get_data(as_text=True)
    assert SessionLocal().get(Country, "ch").geofabrik_path is None


def test_unreachable_geofabrik_blocks_unless_skipped(client, tmp_data_root, monkeypatch):
    def boom(path):
        raise requests.ConnectionError()
    monkeypatch.setattr(geofabrik, "extract_exists", boom)
    _add_country("ch")
    data = {"geofabrik_path": "europe/switzerland", "wof_code": "ch"}
    assert client.post("/countries/ch", headers=HX, data=data).status_code == 422
    assert client.post("/countries/ch", headers=HX, data={**data, "skip_verify": "1"}).status_code == 200


def test_blank_path_unconfigures(client, tmp_data_root):
    _add_country("be", "Belgium", "europe/belgium")
    client.post("/countries/be", headers=HX, data={"geofabrik_path": "", "wof_code": "", "skip_verify": "1"})
    be = SessionLocal().get(Country, "be")
    assert be.geofabrik_path is None and be.wof_code == "be"


def test_save_rejects_unknown_openaddresses_files(client, tmp_data_root, geofabrik_ok):
    _add_country("ch")
    r = client.post("/countries/ch", headers=HX, data={
        "geofabrik_path": "europe/switzerland", "wof_code": "ch",
        "openaddresses_files": "ch/ok.csv\nch/missing1.csv\nch/missing2.csv",
    })
    body = r.get_data(as_text=True)
    assert r.status_code == 422 and "Not found on OpenAddresses: ch/missing1, ch/missing2" in body
    assert address_source_service.openaddresses_files(SessionLocal().get(Country, "ch")) == []


def test_unreachable_openaddresses_blocks_unless_skipped(client, tmp_data_root, monkeypatch):
    def boom(files):
        raise requests.ConnectionError()
    monkeypatch.setattr(openaddresses, "find_missing", boom)
    _add_country("ch")
    data = {"geofabrik_path": "", "wof_code": "ch", "openaddresses_files": "ch/a.csv"}
    assert client.post("/countries/ch", headers=HX, data=data).status_code == 422
    assert client.post("/countries/ch", headers=HX, data={**data, "skip_verify": "1"}).status_code == 200


def test_save_normalises_openaddresses_extensions(client, tmp_data_root, geofabrik_ok):
    _add_country("ch")
    client.post("/countries/ch", headers=HX, data={
        "geofabrik_path": "", "wof_code": "ch", "openaddresses_files": "ch/a.csv\nch/a\nch/b.geojson",
    })
    assert address_source_service.openaddresses_files(SessionLocal().get(Country, "ch")) == ["ch/a", "ch/b"]


def test_modal_lists_sources_and_osm_line(client, tmp_data_root):
    _add_country("ch")
    html = client.get("/countries/ch", headers=HX).get_data(as_text=True)
    assert "Address sources" in html and 'name="overture_enabled"' in html and 'name="openaddresses_files"' in html
    assert "available once a Geofabrik path is set" in html and "listed in Overture" in html
    _add_country("be", "Belgium", "europe/belgium")
    assert "from the Geofabrik extract" in client.get("/countries/be", headers=HX).get_data(as_text=True)


def test_overture_toggle_persists(client, tmp_data_root):
    _add_country("ch")
    base = {"geofabrik_path": "", "wof_code": "ch"}
    client.post("/countries/ch", headers=HX, data={**base, "overture_enabled": "1"})
    assert address_source_service.get_state(SessionLocal().get(Country, "ch"), "overture").enabled is True
    assert "checked" in client.get("/countries/ch", headers=HX).get_data(as_text=True)
    client.post("/countries/ch", headers=HX, data=base)
    assert address_source_service.get_state(SessionLocal().get(Country, "ch"), "overture").enabled is False


def test_modal_has_locality_boundaries_fieldset(client, tmp_data_root):
    _add_country("be", "Belgium", "europe/belgium")
    html = client.get("/countries/be", headers=HX).get_data(as_text=True)
    assert "Locality boundaries" in html and "Who's On First boundaries are used unchanged" in html
    for name in ("osm_admin_levels", "official_url", "official_format", "official_name_field", "geonames_postal_enabled"):
        assert f'name="{name}"' in html
    assert "available for this country" in html


def test_boundary_sources_persist(client, tmp_data_root, monkeypatch):
    monkeypatch.setattr(official_polygons, "url_reachable", lambda url: True)
    _add_country("nl", "Netherlands", "europe/netherlands")
    data = {"geofabrik_path": "", "wof_code": "nl", "skip_verify": "1", "osm_admin_levels": "10",
            "official_url": "https://api.pdok.nl/x", "official_format": "ogc-api", "official_name_field": "naam",
            "geonames_postal_enabled": "1"}
    assert client.post("/countries/nl", headers=HX, data=data).status_code == 200
    nl = SessionLocal().get(Country, "nl")
    assert boundary_source_service.get_state(nl, "osm_admin").config["levels"] == [10]
    assert boundary_source_service.get_state(nl, "official_polygons").config["format"] == "ogc-api"
    assert boundary_source_service.get_state(nl, "geonames_postal").enabled is True
    html = client.get("/countries/nl", headers=HX).get_data(as_text=True)
    assert 'value="10"' in html and 'value="https://api.pdok.nl/x"' in html and 'value="naam"' in html
    assert '<option value="ogc-api" selected>' in html


def test_boundary_validation_error_keeps_input_and_saves_nothing(client, tmp_data_root):
    _add_country("nl", "Netherlands", "europe/netherlands")
    r = client.post("/countries/nl", headers=HX,
                    data={"geofabrik_path": "", "wof_code": "nl", "osm_admin_levels": "99", "skip_verify": "1"})
    body = r.get_data(as_text=True)
    assert r.status_code == 422 and "between 5 and 12" in body and 'value="99"' in body
    assert boundary_source_service.get_state(SessionLocal().get(Country, "nl"), "osm_admin").enabled is False
