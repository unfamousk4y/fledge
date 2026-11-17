"""Shared fixtures for tests that need a real Postgres (DECISIONS.md: no SQLite).

The test database is TEST_DATABASE_URL (repo-root .env locally, an env var in CI).
It is rebuilt from the Alembic migrations once per test session, and every
table is emptied before each test, so tests never see each other's rows.
Tests that don't request `engine` never touch a database.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import OperationalError

from fledge import config, db

BACKEND = Path(__file__).resolve().parents[1]


def alembic_config(conn: object) -> Config:
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.attributes["connection"] = conn
    cfg.attributes["configure_logger"] = False
    return cfg


@pytest.fixture(scope="session")
def migrated_engine() -> Iterator[Engine]:
    engine = create_engine(config.test_database_url())
    # This fixture wipes the whole database. Refuse anything not named *_test,
    # so a copy-paste mistake can't point it at real data.
    if not (engine.url.database or "").endswith("_test"):
        pytest.fail(f"TEST_DATABASE_URL must name a *_test database, got {engine.url.database!r}")
    try:
        with engine.begin() as conn:
            conn.execute(text("DROP SCHEMA public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
            command.upgrade(alembic_config(conn), "head")
    except OperationalError as exc:
        pytest.fail(
            "Can't reach the test database. Start it with `docker compose up -d` "
            f"from the repo root.\n{exc}",
            pytrace=False,
        )
    yield engine
    engine.dispose()


@pytest.fixture
def engine(migrated_engine: Engine) -> Engine:
    tables = ", ".join(t.name for t in db.metadata.sorted_tables)
    with migrated_engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    return migrated_engine
