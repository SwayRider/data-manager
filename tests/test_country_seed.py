from datamanager.countries.seed import seed_countries
from datamanager.models.country import Country
from datamanager.services import address_sources as address_sources_service
from datamanager.services import boundary_sources as boundary_sources_service

EXPECTED_ISO2_CODES = {"be", "lu", "nl", "fr", "mc", "de"}


def test_seed_populates_all_countries_and_flags_curated(db_session):
    changed = seed_countries(db_session)
    rows = {c.iso2: c for c in db_session.query(Country).all()}
    assert set(changed) == set(rows)
    assert len(rows) > 200
    assert EXPECTED_ISO2_CODES <= set(rows)

    assert {iso for iso, c in rows.items() if c.is_curated} == EXPECTED_ISO2_CODES
    uncurated = rows["ch"]
    assert uncurated.geofabrik_path is None
    assert uncurated.wof_code == "ch"
    assert address_sources_service.openaddresses_files(uncurated) == []
    # Overture defaults on for covered countries only
    assert address_sources_service.get_state(uncurated, "overture").enabled is True
    assert address_sources_service.get_state(rows["af"], "overture").enabled is False
    assert address_sources_service.openaddresses_files(rows["nl"]) == ["nl/countrywide"]


def test_seed_is_idempotent(db_session):
    seed_countries(db_session)
    changed_on_rerun = seed_countries(db_session)
    assert changed_on_rerun == []


def test_belgium_srtm_bbox_matches_hand_typed_config_mini(db_session):
    # data-pipeline/config/config-mini.yml hand-types belgium's srtm box as
    # [49, 52, 2, 7] — this is Phase 0's spot-check that auto-derivation
    # from Natural Earth geometry reproduces it.
    seed_countries(db_session)
    belgium = db_session.get(Country, "be")
    assert belgium.srtm_bbox_json == [49, 52, 2, 7]


def test_germany_curated_fields_match_config_mini():
    from datamanager.countries.seed import load_curated

    curated = load_curated()
    assert curated["de"]["geofabrik_path"] == "europe/germany"
    assert curated["de"]["wof_code"] == "de"
    assert len(curated["de"]["openaddresses_files"]) == 15


def test_reseed_does_not_overwrite_ui_edits(db_session):
    seed_countries(db_session)
    be = db_session.get(Country, "be")
    be.geofabrik_path = "europe/belgium-edited"
    address_sources_service.set_openaddresses_files(be, ["custom"])
    address_sources_service.set_state(be, "overture", address_sources_service.SourceState(enabled=False))
    ch = db_session.get(Country, "ch")
    ch.geofabrik_path = "europe/switzerland"
    db_session.commit()

    assert seed_countries(db_session) == []
    assert db_session.get(Country, "be").geofabrik_path == "europe/belgium-edited"
    assert address_sources_service.openaddresses_files(db_session.get(Country, "be")) == ["custom"]
    assert address_sources_service.get_state(db_session.get(Country, "be"), "overture").enabled is False
    assert db_session.get(Country, "ch").geofabrik_path == "europe/switzerland"


def test_seed_boundary_defaults_only_for_measured_countries(db_session):
    seed_countries(db_session)
    levels = {
        iso: boundary_sources_service.get_state(db_session.get(Country, iso), "osm_admin")
        for iso in ("be", "nl", "lu", "de", "fr", "ie")
    }
    assert levels["be"].config["levels"] == [9] and levels["nl"].config["levels"] == [10]
    assert levels["lu"].config["levels"] == [9] and levels["de"].config["levels"] == [9, 10]
    assert not levels["fr"].enabled and not levels["ie"].enabled
    postal = boundary_sources_service.get_state(db_session.get(Country, "be"), "geonames_postal")
    assert postal.enabled is False


def test_reseed_keeps_edited_boundary_rows(db_session):
    seed_countries(db_session)
    be = db_session.get(Country, "be")
    boundary_sources_service.set_state(be, "osm_admin", boundary_sources_service.SourceState(True, {"levels": [10]}))
    db_session.commit()
    assert seed_countries(db_session) == []
    assert boundary_sources_service.get_state(db_session.get(Country, "be"), "osm_admin").config["levels"] == [10]
