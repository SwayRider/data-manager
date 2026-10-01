import pytest

from datamanager.errors import ValidationError
from datamanager.services import config_profiles as svc


def test_create_trims_and_persists(db_session):
    p = svc.create_profile(db_session, "  dev-mini ", " small setup ")
    assert (p.name, p.description) == ("dev-mini", "small setup")
    assert [x.name for x in svc.list_profiles(db_session)] == ["dev-mini"]


@pytest.mark.parametrize("name", ["", "   ", "x" * 101])
def test_invalid_names(db_session, name):
    with pytest.raises(ValidationError):
        svc.create_profile(db_session, name)


def test_name_unique_case_insensitive(db_session):
    svc.create_profile(db_session, "Dev")
    with pytest.raises(ValidationError):
        svc.create_profile(db_session, "dev")


def test_update_allows_own_name_but_not_others(db_session):
    a = svc.create_profile(db_session, "a")
    svc.create_profile(db_session, "b")
    svc.update_profile(db_session, a, "A", "new")
    assert (a.name, a.description) == ("A", "new")
    with pytest.raises(ValidationError):
        svc.update_profile(db_session, a, "b", "")


def test_delete(db_session):
    p = svc.create_profile(db_session, "a")
    svc.delete_profile(db_session, p)
    assert svc.list_profiles(db_session) == []


def test_duplicate_sequences_names(db_session):
    p = svc.create_profile(db_session, "dev", "desc")
    c1 = svc.duplicate_profile(db_session, p)
    c2 = svc.duplicate_profile(db_session, p)
    assert (c1.name, c2.name) == ("dev (copy)", "dev (copy 2)")
    assert c1.description == "desc"
