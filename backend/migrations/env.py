"""Alembic entry point: connects to the database and runs migrations.

Tests pass an open connection in `config.attributes["connection"]`; otherwise
we connect to DATABASE_URL.
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import Connection, create_engine

from fledge import config as settings
from fledge.db import metadata

config = context.config
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name)

target_metadata = metadata


def run(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


connection = config.attributes.get("connection")
if connection is not None:
    run(connection)
else:
    engine = create_engine(settings.database_url())
    with engine.connect() as conn:
        run(conn)
    engine.dispose()
