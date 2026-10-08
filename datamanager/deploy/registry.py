from datamanager.deploy.base import DeployDriver
from datamanager.errors import DeployError

DRIVERS: dict[str, type[DeployDriver]] = {}


def register(driver: type[DeployDriver]) -> type[DeployDriver]:
    DRIVERS[driver.key] = driver
    return driver


def get_driver(key: str) -> type[DeployDriver]:
    from datamanager.deploy import compose_single_machine  # noqa: F401  (registers itself)

    try:
        return DRIVERS[key]
    except KeyError:
        raise DeployError(f"Unknown deploy driver: {key}") from None
