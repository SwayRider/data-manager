import tempfile
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from datamanager.config import config as app_config
from datamanager.db import build_engine
from datamanager.models import Base


@pytest.fixture()
def tmp_data_root(monkeypatch, tmp_path):
    monkeypatch.setattr(app_config, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(app_config, "DATABASE_PATH", str(tmp_path / "state" / "db.sqlite3"))
    app_config.ensure_data_dirs()
    return tmp_path


@pytest.fixture()
def db_session(tmp_data_root):
    engine = build_engine(app_config.database_uri)
    Base.metadata.create_all(engine)
    session: Session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def app(tmp_data_root):
    from datamanager import create_app
    from datamanager.db import SessionLocal, engine as default_engine

    engine = build_engine(app_config.database_uri)
    Base.metadata.create_all(engine)
    SessionLocal.remove()
    SessionLocal.configure(bind=engine)

    flask_app = create_app()
    flask_app.config.update(TESTING=True)
    yield flask_app

    SessionLocal.remove()
    SessionLocal.configure(bind=default_engine)
    engine.dispose()


@pytest.fixture()
def client(app):
    return app.test_client()
