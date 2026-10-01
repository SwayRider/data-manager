from datamanager.countries.autofill import geofabrik_paths, lookup_code, openaddresses_picks


def _feature(id_, parent_path, codes):
    pbf = f"https://download.geofabrik.de/{parent_path}{id_}-latest.osm.pbf"
    props = {"id": id_, "urls": {"pbf": pbf}}
    if codes is not None:
        props["iso3166-1:alpha2"] = codes
    return {"properties": props}


def test_geofabrik_prefers_fewest_countries_then_shallowest():
    index = {"features": [
        _feature("france", "europe/", ["FR"]),
        _feature("senegal-and-gambia", "africa/", ["SN", "GM"]),
        _feature("gambia", "africa/", ["GM"]),
        _feature("no-iso", "europe/", None),
    ]}
    paths = geofabrik_paths(index)
    assert paths["fr"] == ("europe/france", [])
    assert paths["gm"] == ("africa/gambia", [])
    assert paths["sn"] == ("africa/senegal-and-gambia", ["gm"])
    assert "no-iso" not in paths


def _oa(source, name):
    return {"source": source, "name": name, "job": 1}


def test_openaddresses_pick_rules():
    index = [
        _oa("nl/countrywide", "country"), _oa("nl/amsterdam", "city"),
        _oa("de/bw/statewide", "state"), _oa("de/by/statewide", "state"), _oa("de/by/munich", "state"),
        _oa("xx/a/region-a", "state"), _oa("xx/b/region-b", "region"),
        _oa("yy/x/city", "city"), _oa("yy/y/county", "county"),
        {"source": "zz/countrywide", "name": "country"},  # no job: ignored
    ]
    picks = openaddresses_picks(index)
    assert (picks["nl"].sources, picks["nl"].level) == (["nl/countrywide"], "country")
    assert (picks["de"].sources, picks["de"].level) == (["de/bw/statewide", "de/by/statewide"], "state")
    assert picks["xx"].sources == ["xx/a/region-a", "xx/b/region-b"]
    assert picks["yy"].sources == [] and picks["yy"].available == {"city": 1, "county": 1}
    assert "zz" not in picks


def test_lookup_code_uses_last_part():
    assert lookup_code("cn-tw") == "tw" and lookup_code("be") == "be"
