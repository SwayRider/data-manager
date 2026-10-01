import pytest
import requests

from datamanager.address_sources import SOURCES, SourceState
from datamanager.address_sources.overture import is_covered
from datamanager.errors import ValidationError
from datamanager.services import openaddresses

OA, OV = SOURCES["openaddresses"], SOURCES["overture"]


def test_overture_default_only_for_covered_countries():
    assert OV.default("nl", {}).enabled and not OV.default("af", {}).enabled
    assert is_covered("de") and not is_covered("gb")


def test_openaddresses_default_from_curated_normalises_extensions():
    state = OA.default("be", {"openaddresses_files": ["be/a.csv", "be/b"]})
    assert state == SourceState(enabled=True, config={"files": ["be/a", "be/b"]})
    assert OA.default("af", {}) == SourceState(enabled=False, config={"files": []})


def test_openaddresses_validate_cleans_and_verifies(monkeypatch):
    monkeypatch.setattr(openaddresses, "find_missing", lambda files: [f for f in files if f == "x/bad"])
    ok = OA.validate(OA.from_form({"openaddresses_files": "x/a.csv\n\nx/a\nx/b"}), verify=True)
    assert ok == SourceState(enabled=True, config={"files": ["x/a", "x/b"]})
    with pytest.raises(ValidationError, match="x/bad"):
        OA.validate(OA.from_form({"openaddresses_files": "x/bad.csv"}), verify=True)
    assert OA.validate(OA.from_form({"openaddresses_files": "x/bad"}), verify=False).enabled


def test_openaddresses_blank_is_disabled_and_whitespace_rejected():
    assert OA.validate(OA.from_form({"openaddresses_files": "  \n"}), verify=True).enabled is False
    with pytest.raises(ValidationError):
        OA.validate(OA.from_form({"openaddresses_files": "has space"}), verify=False)


def test_openaddresses_unreachable_blocks_unless_unverified(monkeypatch):
    def boom(files):
        raise requests.ConnectionError()
    monkeypatch.setattr(openaddresses, "find_missing", boom)
    with pytest.raises(ValidationError, match="Could not reach"):
        OA.validate(OA.from_form({"openaddresses_files": "x/a"}), verify=True)


def test_describe_and_form_values():
    assert OV.describe(SourceState(enabled=True)) == "✓" and OV.describe(SourceState()) == "—"
    assert OA.describe(SourceState(True, {"files": ["a", "b"]})) == "2 sources"
    assert OA.form_values(SourceState(True, {"files": ["a", "b"]})) == {"openaddresses_files": "a\nb"}
