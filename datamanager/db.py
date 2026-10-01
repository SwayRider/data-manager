"""Engine/session construction shared by the Flask app, the RQ worker, and
Alembic's env.py, so none of them can silently disagree on which SQLite file
or pragmas are in effect.

Deliberately plain SQLAlchemy, not Flask-SQLAlchemy: the RQ worker runs stage
code with no Flask app/request context and must be able to open a session by
importing this module alone.
"""

from sqlalchemy import create_engine, event
from sqlalchemy.orm import scoped_session, sessionmaker

from datamanager.config import config


def _enable_wal(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def build_engine(database_uri: str | None = None):
    engine = create_engine(
        database_uri or config.database_uri,
        connect_args={"check_same_thread": False},
    )
    event.listen(engine, "connect", _enable_wal)
    return engine


engine = build_engine()
SessionLocal = scoped_session(sessionmaker(bind=engine, autoflush=False, autocommit=False))
