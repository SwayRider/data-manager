import pytest

from datamanager import map_styles
from datamanager.errors import ValidationError
from datamanager.services import config_profiles as profiles
from datamanager.services import map_styles as svc
from datamanager.services import resolve


def _mini_style():
    def place(id_, filter_, **extra):
        return {"id": id_, "type": "symbol", "source": "protomaps", "source-layer": "places", "filter": filter_, **extra}

    return {
        "version": 8,
        "sources": {"protomaps": {"type": "vector", "url": "pmtiles://__TILES__"}},
        "glyphs": "https://elsewhere/{fontstack}/{range}.pbf",
        "layers": [
            {"id": "background", "type": "background"},
            place("places_country", ["==", "kind", "country"]),
            place("places_region", ["==", "kind", "region"], minzoom=4, maxzoom=8),
            place("places_locality", ["==", "kind", "locality"]),
            place("places_subplace", ["in", "kind", "neighbourhood", "macrohood"], minzoom=11),
            {"id": "roads_major", "type": "line", "source": "protomaps", "source-layer": "roads"},
        ],
    }


@pytest.fixture()
def mini(monkeypatch):
    monkeypatch.setattr(map_styles, "load", lambda key: __import__("copy").deepcopy(_mini_style()))


def test_locality_layer_is_split_per_label_kind(mini):
    style = svc.load("x")
    ids = [l["id"] for l in style["layers"]]
    assert "places_locality" not in ids
    assert ids.count("roads_major") == 1 and ids[0] == "background"
    assert {k for keys in svc.place_layers(style).values() for k in keys} == set(svc.LABEL_KEYS)
    assert svc.layer_keys(next(l for l in style["layers"] if l["id"] == "roads_major")) is None


def test_defaults_and_patching(mini):
    style = svc.load("x")
    assert svc.style_defaults(style) == {"country": 0, "state": 4, "capital": 0, "city": 0, "town": 0, "village": 0, "other": 0, "suburb": 11}
    built = svc.build_style("x", {"town": 10, "state": 6, "suburb": 9}, tiles_url="pmtiles://https://t/p.pmtiles", glyphs="G", sprite="S")
    by_id = {l["id"]: l for l in built["layers"]}
    assert by_id["places_locality_town"]["minzoom"] == 10
    assert by_id["places_region"]["minzoom"] == 6 and by_id["places_region"]["maxzoom"] == 8
    assert by_id["places_subplace"]["minzoom"] == 9
    assert "minzoom" not in by_id["places_locality_city"]  # untouched
    assert built["sources"]["protomaps"]["url"] == "pmtiles://https://t/p.pmtiles"
    assert built["glyphs"] == "G" and built["sprite"] == "S"
    assert svc.build_style("x", {"town": 10}) == svc.build_style("x", {"town": 10})  # idempotent
    raised = svc.build_style("x", {"state": 9})
    assert "maxzoom" not in next(l for l in raised["layers"] if l["id"] == "places_region")  # raised past its maxzoom


@pytest.mark.parametrize("key", list(map_styles.STYLES))
def test_vendored_styles_are_usable(key):
    style = svc.load(key)
    assert style["sources"]["protomaps"]["type"] == "vector"
    assert {k for keys in svc.place_layers(style).values() for k in keys} == set(svc.LABEL_KEYS)
    layer_sources = {l["source-layer"] for l in style["layers"] if "source-layer" in l}
    assert layer_sources <= {"boundaries", "buildings", "earth", "landcover", "landuse", "natural", "places", "pois", "roads", "transit", "water"}
    built = svc.build_style(key, {"town": 7}, tiles_url="pmtiles://https://t/p.pmtiles", glyphs="g")
    assert built["glyphs"] == "g" and any(l.get("minzoom") == 7 for l in built["layers"])
    assert style["layers"] == svc.load(key)["layers"]  # loading is pure


def test_settings_roundtrip_and_defaults(db_session):
    profile = profiles.create_profile(db_session, "dev")
    assert svc.get_settings(db_session, profile.id) == {"light_style": "light", "dark_style": "dark", "labels": {}}
    svc.save_settings(db_session, profile.id, "white", "dark", {"town": "7", "village": "", "city": "4"})
    assert svc.get_settings(db_session, profile.id) == {"light_style": "white", "dark_style": "dark", "labels": {"town": 7, "city": 4}}
    other = profiles.create_profile(db_session, "other")
    assert svc.get_settings(db_session, other.id)["labels"] == {}
    patched = svc.config_style(db_session, profile.id, "light", tiles_url="u")
    assert any(l.get("minzoom") == 7 for l in patched["layers"] if l["id"] == "places_locality_town")


def test_validation(db_session):
    profile = profiles.create_profile(db_session, "dev")
    with pytest.raises(ValidationError, match="light style"):
        svc.save_settings(db_session, profile.id, "dark", "dark", {})
    with pytest.raises(ValidationError, match="dark style"):
        svc.save_settings(db_session, profile.id, "light", "light", {})
    with pytest.raises(ValidationError, match="between 0 and 22"):
        svc.save_settings(db_session, profile.id, "light", "dark", {"town": "30"})
    with pytest.raises(ValidationError, match="whole number"):
        svc.save_settings(db_session, profile.id, "light", "dark", {"town": "x"})
    with pytest.raises(ValidationError, match="before"):
        svc.save_settings(db_session, profile.id, "light", "dark", {"town": "5", "village": "3"})
    with pytest.raises(ValidationError, match="Unknown"):
        svc.save_settings(db_session, profile.id, "light", "dark", {"bogus": "3"})


def test_resolved_hash_changes_with_labels(db_session):
    profile = profiles.create_profile(db_session, "dev")
    before = resolve.resolve_config(db_session, profile.id).style["hash"]
    svc.save_settings(db_session, profile.id, "light", "dark", {"town": "7"})
    after = resolve.resolve_config(db_session, profile.id).style
    assert after["hash"] != before and after["labels"] == {"town": 7}


def test_saved_key_of_a_removed_style_falls_back_to_the_default(db_session):
    from datamanager.models import StyleSettings

    profile = profiles.create_profile(db_session, "dev")
    db_session.add(StyleSettings(config_profile_id=profile.id, light_style="positron", dark_style="dark-matter", labels_json={}))
    db_session.commit()
    assert svc.get_settings(db_session, profile.id)["light_style"] == "light"
    assert svc.get_settings(db_session, profile.id)["dark_style"] == "dark"


@pytest.mark.parametrize("key", list(map_styles.STYLES))
def test_split_locality_filters_use_the_syntax_of_the_original(key):
    """MapLibre rejects a filter mixing legacy (["==", "kind", ..]) and expression (["get", ..]) syntax: blank map."""
    def uses_get(expr):
        return isinstance(expr, list) and (expr[:1] == ["get"] or any(uses_get(e) for e in expr))

    original = next(l for l in map_styles.load(key)["layers"] if l["id"] == svc.LOCALITY_ID)
    assert not uses_get(original["filter"])
    for layer in svc.load(key)["layers"]:
        if layer["id"].startswith(svc.LOCALITY_ID + "_"):
            assert not uses_get(layer["filter"])
