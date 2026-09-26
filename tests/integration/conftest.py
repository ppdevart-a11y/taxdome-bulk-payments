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
from contextlib import contextmanager
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
# Worked out by hand: the sample moves $13,251.25 from Pinecrest to Nair and Lopez.
AFTER_ONE_SAMPLE = {PINECREST: 3_674_875, LOPEZ: 170_075, NAIR: 1_405_050}

Balances = Callable[[], dict[str, int]]
Payments = Callable[[], list[tuple[int, int, int, str]]]
# (action, stage, times) -> a function that tells how many attempts reached the stage.
InjectFailure = Callable[[str, str, int], Callable[[], int]]

# Where an injected failure strikes. Both watch the idempotency key, the transaction's last write.
FAILURE_STAGES = {
    # Before COMMIT, where a real deadlock or a dropped connection would hit.
    "on the last write": "CREATE TRIGGER injected_failure BEFORE INSERT ON idempotency_keys",
    # Deferred: runs inside COMMIT, after every write went through.
    "during COMMIT": (
        "CREATE CONSTRAINT TRIGGER injected_failure AFTER INSERT ON idempotency_keys "
        "DEFERRABLE INITIALLY DEFERRED"
    ),
}


def pay(payer: str, *lines: tuple[str, str]) -> dict[str, Any]:
    """A request body: `payer` pays each (amount, payee) line."""
    return {
        "payer_firm_uuid": payer,
        "payments": [
            {"amount": amount, "payee_firm_uuid": payee, "description": f"payment {i}"}
            for i, (amount, payee) in enumerate(lines)
        ],
    }


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
def alembic_config() -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    return config


@pytest.fixture(scope="session")
def engine(database_url: str, alembic_config: Config) -> Iterator[Engine]:
    engine = create_engine(database_url, pool_size=30, max_overflow=0)
    with engine.begin() as connection:
        alembic_config.attributes["connection"] = connection
        command.upgrade(alembic_config, "head")
    yield engine
    engine.dispose()


@pytest.fixture(autouse=True)
def seed(engine: Engine) -> None:
    """Reset to the three sample firms before every test."""
    url = engine.url.set(drivername="postgresql").render_as_string(hide_password=False)
    with psycopg.connect(url, autocommit=True) as connection:
        connection.execute(SEED_SQL)


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def instances(database_url: str) -> Iterator[list[sessionmaker[Session]]]:
    """Two engines with separate pools: two app instances sharing only the database."""
    engines = [create_engine(database_url, pool_size=40, max_overflow=0) for _ in range(2)]
    yield [sessionmaker(engine) for engine in engines]
    for engine in engines:
        engine.dispose()


@contextmanager
def app_client(
    session_factory: sessionmaker[Session], *, raise_server_exceptions: bool = True
) -> Iterator[TestClient]:
    """The app, with its database sessions drawn from `session_factory`."""

    def session_override() -> Iterator[Session]:
        with session_factory() as session:
            yield session

    app = create_app()
    app.dependency_overrides[get_session] = session_override
    with TestClient(app, raise_server_exceptions=raise_server_exceptions) as client:
        yield client


@pytest.fixture
def client(session_factory: sessionmaker[Session]) -> Iterator[TestClient]:
    with app_client(session_factory) as client:
        yield client


@pytest.fixture
def lenient_client(session_factory: sessionmaker[Session]) -> Iterator[TestClient]:
    """Like `client`, but an unexpected error comes back as the 500 a real client would see."""
    with app_client(session_factory, raise_server_exceptions=False) as client:
        yield client


@pytest.fixture
def inject_failure(engine: Engine) -> Iterator[InjectFailure]:
    """Run a PL/pgSQL `action` the first `times` times a request reaches `stage`.

    The trigger watches the idempotency key, so the request must send one.
    """

    def install(action: str, stage: str, times: int) -> Callable[[], int]:
        with engine.begin() as connection:
            connection.execute(text("CREATE SEQUENCE injected_attempts"))
            connection.execute(
                text(
                    "CREATE FUNCTION injected_failure() RETURNS trigger LANGUAGE plpgsql AS $$ "
                    f"BEGIN IF nextval('injected_attempts') <= {times} THEN {action}; END IF; "
                    "RETURN NEW; END $$"
                )
            )
            connection.execute(
                text(f"{FAILURE_STAGES[stage]} FOR EACH ROW EXECUTE FUNCTION injected_failure()")
            )

        def attempts() -> int:
            with engine.connect() as connection:
                reached = connection.execute(
                    text(
                        "SELECT CASE WHEN is_called THEN last_value ELSE 0 END "
                        "FROM injected_attempts"
                    )
                )
                return int(reached.scalar_one())

        return attempts

    yield install
    with engine.begin() as connection:
        connection.execute(text("DROP TRIGGER IF EXISTS injected_failure ON idempotency_keys"))
        connection.execute(text("DROP FUNCTION IF EXISTS injected_failure()"))
        connection.execute(text("DROP SEQUENCE IF EXISTS injected_attempts"))


@pytest.fixture
def balances(engine: Engine) -> Balances:
    def read() -> dict[str, int]:
        with engine.connect() as connection:
            rows = connection.execute(text("SELECT uuid, balance_cents FROM firms"))
            return {uuid: cents for uuid, cents in rows}

    return read


@pytest.fixture
def payments(engine: Engine) -> Payments:
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
