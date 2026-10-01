import pytest
import requests

from datamanager.services import openaddresses


class FakeResponse:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


def test_normalize_strips_extension():
    assert openaddresses.normalize("be/bru/bosa-region-brussels-fr.csv") == "be/bru/bosa-region-brussels-fr"
    assert openaddresses.normalize("be/bru/x") == "be/bru/x"


def test_source_exists_and_find_missing(monkeypatch):
    seen = {}

    def fake_get(url, params, timeout):
        seen[params["source"]] = params
        return FakeResponse([{"job": 1}] if "good" in params["source"] else [])

    monkeypatch.setattr(requests, "get", fake_get)
    assert openaddresses.find_missing(["a/good.csv", "b/bad.csv", "c/good2"]) == ["b/bad.csv"]
    assert seen["a/good"]["layer"] == "addresses"
    assert openaddresses.find_missing([]) == []


def test_unreachable_raises(monkeypatch):
    def boom(*a, **k):
        raise requests.ConnectionError()
    monkeypatch.setattr(requests, "get", boom)
    with pytest.raises(requests.RequestException):
        openaddresses.find_missing(["a.csv"])


def test_strip_extension_only_known_extensions():
    assert openaddresses.strip_extension(" de/bw/statewide.csv ") == "de/bw/statewide"
    assert openaddresses.strip_extension("x/y.GEOJSON") == "x/y"
    assert openaddresses.strip_extension("us/nc/city.of.x") == "us/nc/city.of.x"
    assert openaddresses.strip_extension("nl/countrywide") == "nl/countrywide"
