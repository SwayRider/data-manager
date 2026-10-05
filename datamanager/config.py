import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


class AppConfig:
    """Bootstrap config: where the DB file, broker, and downloads live.

    Deliberately separate from data-manager's own domain configuration model
    (regions, profiles, ...), which is DB-driven starting Phase 1. This is
    just "where's my stuff" for the process itself.
    """

    REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:36389/0")
    DATABASE_PATH = os.environ.get("DATABASE_PATH", "./data/state/db.sqlite3")
    DATA_ROOT = os.environ.get("DATA_ROOT", "./data")
    # The package repository: its own folder (ideally another filesystem, e.g. a ZFS dataset); default under DATA_ROOT
    PACKAGE_ROOT = os.environ.get("PACKAGE_ROOT", "")

    NATURAL_EARTH_URL = os.environ.get(
        "NATURAL_EARTH_URL",
        "https://naturalearth.s3.amazonaws.com/10m_cultural/ne_10m_admin_0_countries.zip",
    )
    NATURAL_EARTH_DIR = os.environ.get(
        "NATURAL_EARTH_DIR", "./data/downloads/natural-earth"
    )

    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-key-change-me")
    LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
    LOG_JSON = os.environ.get("LOG_JSON", "true").lower() in ("1", "true", "yes")

    @property
    def database_uri(self) -> str:
        return f"sqlite:///{Path(self.DATABASE_PATH).resolve()}"

    @property
    def package_root(self) -> Path:
        return Path(self.PACKAGE_ROOT) if self.PACKAGE_ROOT else Path(self.DATA_ROOT) / "releases"

    @property
    def library_dir(self) -> Path:
        return Path(self.DATA_ROOT) / "library"

    @property
    def work_dir(self) -> Path:
        return Path(self.DATA_ROOT) / "work"

    def ensure_data_dirs(self) -> None:
        Path(self.DATABASE_PATH).resolve().parent.mkdir(parents=True, exist_ok=True)
        for sub in ("downloads", "library", "work", "releases", "deploy-state"):
            (Path(self.DATA_ROOT) / sub).mkdir(parents=True, exist_ok=True)


config = AppConfig()
