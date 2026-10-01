from logging.config import fileConfig

from alembic import context

from datamanager.config import config as app_config
from datamanager.db import build_engine
from datamanager.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=app_config.database_uri,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # Always resolves the same DB file the running app/worker use — env.py
    # imports the same config/db modules rather than reading sqlalchemy.url
    # from alembic.ini.
    app_config.ensure_data_dirs()
    connectable = build_engine(app_config.database_uri)

    with connectable.connect() as connection:
        # SQLite batch migrations drop and recreate tables; with foreign keys ON
        # that would cascade-delete child rows (ON DELETE CASCADE). Must be set
        # before any DML starts a transaction, so do it first.
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.commit()  # end SQLAlchemy's autobegun transaction so Alembic owns the migration transaction
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()

        violations = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError(f"foreign key violations after migration: {violations}")


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
