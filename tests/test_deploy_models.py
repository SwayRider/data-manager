import pytest
from sqlalchemy.exc import IntegrityError

from datamanager.models import DeployConfig, Deployment, Package
from datamanager.services import settings as settings_service


def _package(session, tag="r-20261008-1"):
    package = Package(tag=tag, status="complete", path=f"/tmp/{tag}")
    session.add(package)
    session.flush()
    return package


def test_deploy_config_key_is_unique(db_session):
    db_session.add(DeployConfig(key="dev-mini", driver="compose-single-machine", config_json={"host": None}))
    db_session.commit()
    db_session.add(DeployConfig(key="dev-mini", driver="compose-single-machine"))
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_deployment_survives_deleting_its_package(db_session):
    config = DeployConfig(key="dev-mini", driver="compose-single-machine")
    package, previous = _package(db_session), _package(db_session, "r-20261007-1")
    deployment = Deployment(package_id=package.id, package_tag=package.tag, deploy_config=config,
                            classes_json=["geodata", "tiles"], previous_package_id=previous.id)
    db_session.add_all([config, deployment])
    db_session.commit()
    assert deployment.status == "running" and deployment.detail_json == {}
    assert deployment.package.tag == "r-20261008-1" and deployment.previous_package.tag == "r-20261007-1"

    db_session.delete(package)
    db_session.commit()
    db_session.refresh(deployment)
    assert deployment.package_id is None and deployment.package_tag == "r-20261008-1"


def test_rollback_links_to_the_deployment_it_reverts(db_session):
    config = DeployConfig(key="dev-mini", driver="compose-single-machine")
    package = _package(db_session)
    first = Deployment(package_id=package.id, package_tag=package.tag, deploy_config=config)
    db_session.add_all([config, first])
    db_session.flush()
    second = Deployment(package_id=package.id, package_tag=package.tag, deploy_config=config, rolled_back_from_id=first.id)
    db_session.add(second)
    db_session.commit()
    assert second.rolled_back_from_id == first.id


def test_deploy_settings_defaults_and_limits(db_session):
    assert settings_service.get(db_session, "deploy.verify") == "full"
    assert settings_service.get(db_session, "deploy.health_timeout") == 300
