"""Integration tests run against a real PostgreSQL: row locks, CHECK constraints
and transaction semantics are the point of this service, and SQLite has none of
them in the same form.

By default a throwaway container is started with testcontainers. Set
TEST_DATABASE_URL to use an existing database instead; its tables are
truncated before every test, so never point it at data you care about.
"""

import json
import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from bulk_payments.db import get_session
from bulk_payments.main import create_app

ROOT = Path(__file__).resolve().parents[2]
SEED_SQL = (ROOT / "scripts" / "seed.sql").read_text()
SAMPLE_REQUEST: dict[str, Any] = json.loads((ROOT / "scripts" / "sample_request.json").read_text())

PINECREST = "3f1c9a2e-7b4d-4c1e-9a55-2d8e6f0b7c41"
LOPEZ = "8b2e4c71-0d3a-4f6e-b1c9-5a7d2e9f4c10"
NAIR = "e5f18b3c-2a9d-4c07-8e6b-1d4a7f9c3b25"
SEED_BALANCES = {PINECREST: 5_000_000, LOPEZ: 50_000, NAIR: 200_000}


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if "tests/integration" in item.nodeid:
            item.add_marker(pytest.mark.integration)


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    if url := os.environ.get("TEST_DATABASE_URL"):
        yield url
        return
    from testcontainers.community.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine", driver="psycopg") as postgres:
        yield postgres.get_connection_url()


@pytest.fixture(scope="session")
def engine(database_url: str) -> Iterator[Engine]:
    engine = create_engine(database_url, pool_size=30, max_overflow=0)
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
    yield engine
    engine.dispose()


@pytest.fixture(autouse=True)
def seed(engine: Engine) -> None:
    """Reset to the spec's three firms before every test."""
    url = engine.url.set(drivername="postgresql").render_as_string(hide_password=False)
    with psycopg.connect(url, autocommit=True) as connection:
        connection.execute(SEED_SQL)


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def client(session_factory: sessionmaker[Session]) -> Iterator[TestClient]:
    def session_override() -> Iterator[Session]:
        with session_factory() as session:
            yield session

    app = create_app()
    app.dependency_overrides[get_session] = session_override
    with TestClient(app) as client:
        yield client


@pytest.fixture
def balances(engine: Engine) -> Callable[[], dict[str, int]]:
    def read() -> dict[str, int]:
        with engine.connect() as connection:
            rows = connection.execute(text("SELECT uuid, balance_cents FROM firms"))
            return {uuid: cents for uuid, cents in rows}

    return read


@pytest.fixture
def payments(engine: Engine) -> Callable[[], list[tuple[int, int, int, str]]]:
    def read() -> list[tuple[int, int, int, str]]:
        with engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT payer_firm_id, payee_firm_id, amount_cents, description "
                    "FROM payments ORDER BY id"
                )
            )
            return [(row[0], row[1], row[2], row[3]) for row in rows]

    return read
