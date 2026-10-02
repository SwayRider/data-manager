import pytest
import requests

from datamanager.boundary_sources import SOURCES, SourceState
from datamanager.boundary_sources import official_polygons
from datamanager.boundary_sources.geonames_postal import has_postal_file
from datamanager.errors import ValidationError

OSM, OFFICIAL, POSTAL = SOURCES["osm_admin"], SOURCES["official_polygons"], SOURCES["geonames_postal"]


def test_osm_admin_default_from_curated():
    assert OSM.default("be", {"osm_locality_levels": [9], "osm_localadmin_levels": [8]}) == SourceState(True, {"levels": [9], "localadmin_levels": [8]})
    assert OSM.default("be", {"osm_locality_levels": [9]}) == SourceState(True, {"levels": [9], "localadmin_levels": []})
    assert OSM.default("fr", {}) == SourceState(False, {"levels": [], "localadmin_levels": []})


def test_osm_admin_municipality_levels():
    state = OSM.validate(OSM.from_form({"osm_admin_levels": "", "osm_localadmin_levels": "8"}), verify=True)
    assert state == SourceState(True, {"levels": [], "localadmin_levels": [8]})
    assert OSM.describe(state) == "municipalities L8"
    both = OSM.validate(OSM.from_form({"osm_admin_levels": "9", "osm_localadmin_levels": "8"}), verify=True)
    assert OSM.describe(both) == "OSM L9 + municipalities L8"
    assert OSM.form_values(both) == {"osm_admin_levels": "9", "osm_localadmin_levels": "8"}
    with pytest.raises(ValidationError) as exc:
        OSM.validate(OSM.from_form({"osm_localadmin_levels": "99"}), verify=False)
    assert exc.value.details["field"] == "osm_localadmin_levels"


def test_osm_admin_parses_sorts_and_dedupes():
    state = OSM.validate(OSM.from_form({"osm_admin_levels": "10, 9 9"}), verify=True)
    assert state == SourceState(True, {"levels": [9, 10], "localadmin_levels": []})
    assert OSM.validate(OSM.from_form({"osm_admin_levels": "  "}), verify=True).enabled is False


@pytest.mark.parametrize("text", ["abc", "4", "13", "9;10"])
def test_osm_admin_rejects_bad_levels(text):
    with pytest.raises(ValidationError) as exc:
        OSM.validate(OSM.from_form({"osm_admin_levels": text}), verify=False)
    assert exc.value.details["field"] == "osm_admin_levels"


def _form(url="https://example.org/data.json", fmt="geojson", name="naam"):
    return {"official_url": url, "official_format": fmt, "official_name_field": name}


def test_official_polygons_validation(monkeypatch):
    monkeypatch.setattr(official_polygons, "url_reachable", lambda url: True)
    ok = OFFICIAL.validate(OFFICIAL.from_form(_form()), verify=True)
    assert ok.enabled and ok.config["name_field"] == "naam"
    assert OFFICIAL.validate(OFFICIAL.from_form(_form(url="")), verify=True).enabled is False
    for bad, field in ((_form(url="ftp://x/y"), "official_url"), (_form(fmt="kml"), "official_format"),
                       (_form(name=""), "official_name_field")):
        with pytest.raises(ValidationError) as exc:
            OFFICIAL.validate(OFFICIAL.from_form(bad), verify=False)
        assert exc.value.details["field"] == field


def test_official_polygons_reachability(monkeypatch):
    monkeypatch.setattr(official_polygons, "url_reachable", lambda url: False)
    with pytest.raises(ValidationError, match="not downloadable"):
        OFFICIAL.validate(OFFICIAL.from_form(_form()), verify=True)
    assert OFFICIAL.validate(OFFICIAL.from_form(_form()), verify=False).enabled

    def boom(url):
        raise requests.ConnectionError()
    monkeypatch.setattr(official_polygons, "url_reachable", boom)
    with pytest.raises(ValidationError, match="Could not reach"):
        OFFICIAL.validate(OFFICIAL.from_form(_form()), verify=True)


def test_url_reachable_falls_back_to_ranged_get(monkeypatch):
    class R:
        def __init__(self, code): self.status_code = code
        def close(self): pass
    monkeypatch.setattr(requests, "head", lambda *a, **k: R(405))
    monkeypatch.setattr(requests, "get", lambda *a, **k: R(206))
    assert official_polygons.url_reachable("https://x") is True
    monkeypatch.setattr(requests, "get", lambda *a, **k: R(404))
    assert official_polygons.url_reachable("https://x") is False


def test_geonames_postal_default_off_and_coverage():
    assert POSTAL.default("be", {}).enabled is False
    assert has_postal_file("be") and has_postal_file("gb") and not has_postal_file("af")


def test_describe():
    assert OSM.describe(SourceState(True, {"levels": [9, 10]})) == "OSM L9,10"
    assert OFFICIAL.describe(SourceState(True, {"url": "https://api.pdok.nl/x"})) == "official (api.pdok.nl)"
    assert POSTAL.describe(SourceState(True)) == "postal" and POSTAL.describe(SourceState()) == "—"
