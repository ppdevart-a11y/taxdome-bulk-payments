"""One request is one transaction: every write happens, or none does."""

from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from bulk_payments.db import get_session
from bulk_payments.main import create_app
from tests.integration.conftest import LOPEZ, NAIR, PINECREST, SAMPLE_REQUEST, SEED_BALANCES

Balances = Callable[[], dict[str, int]]
Payments = Callable[[], list[tuple[int, int, int, str]]]
BIGINT_MAX = 2**63 - 1


def pay(payer: str, *lines: tuple[str, str]) -> dict[str, Any]:
    return {
        "payer_firm_uuid": payer,
        "payments": [
            {"amount": amount, "payee_firm_uuid": payee, "description": "transaction test"}
            for amount, payee in lines
        ],
    }


@pytest.fixture
def lenient_client(session_factory: sessionmaker[Session]) -> Iterator[TestClient]:
    """Like `client`, but an unexpected error comes back as the 500 a real client would see."""

    def session_override() -> Iterator[Session]:
        with session_factory() as session:
            yield session

    app = create_app()
    app.dependency_overrides[get_session] = session_override
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


@pytest.fixture
def refuse_payment_rows(engine: Engine) -> Iterator[None]:
    """The database refuses payment rows: a failure that comes after the balances moved."""
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE FUNCTION refuse_payment() RETURNS trigger LANGUAGE plpgsql AS "
                "$$ BEGIN RAISE EXCEPTION 'payment rows refused by the test'; END $$"
            )
        )
        connection.execute(
            text(
                "CREATE TRIGGER refuse_payment BEFORE INSERT ON payments "
                "FOR EACH ROW EXECUTE FUNCTION refuse_payment()"
            )
        )
    yield
    with engine.begin() as connection:
        connection.execute(text("DROP TRIGGER refuse_payment ON payments"))
        connection.execute(text("DROP FUNCTION refuse_payment()"))


@pytest.mark.usefixtures("refuse_payment_rows")
def test_failure_after_the_balances_moved_rolls_everything_back(
    lenient_client: TestClient, balances: Balances, payments: Payments
) -> None:
    response = lenient_client.post("/bulk_payments", json=SAMPLE_REQUEST)

    assert response.status_code == 500
    assert response.json() == {"error": {"code": "internal_error", "message": "unexpected error"}}
    assert balances() == SEED_BALANCES
    assert payments() == []


def test_payee_credit_overflow_writes_nothing(
    lenient_client: TestClient, engine: Engine, balances: Balances, payments: Payments
) -> None:
    # $92 quadrillion can't happen with real money; if a credit ever overflowed, nothing may stick.
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE firms SET balance_cents = :cents WHERE uuid = :uuid"),
            {"cents": BIGINT_MAX - 500, "uuid": NAIR},
        )
    before = balances()

    response = lenient_client.post(
        "/bulk_payments", json=pay(PINECREST, ("1", LOPEZ), ("10", NAIR))
    )

    assert response.status_code == 500
    assert balances() == before
    assert payments() == []


def test_database_down_is_503_and_writes_nothing(balances: Balances, payments: Payments) -> None:
    # Nothing listens on port 1, so the connection fails before anything reaches a database.
    dead = create_engine("postgresql+psycopg://postgres:postgres@127.0.0.1:1/bulk_payments")
    factory = sessionmaker(dead, expire_on_commit=False)

    def session_override() -> Iterator[Session]:
        with factory() as session:
            yield session

    app = create_app()
    app.dependency_overrides[get_session] = session_override
    try:
        with TestClient(app) as client:
            response = client.post("/bulk_payments", json=SAMPLE_REQUEST)
    finally:
        dead.dispose()

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "1"
    assert response.json()["error"]["code"] == "service_busy"
    assert balances() == SEED_BALANCES
    assert payments() == []


@pytest.fixture
def drop_connection_on_the_last_write(engine: Engine) -> Iterator[None]:
    """The backend kills itself while storing the idempotency key, the transaction's last write."""
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE FUNCTION drop_connection() RETURNS trigger LANGUAGE plpgsql AS "
                "$$ BEGIN PERFORM pg_terminate_backend(pg_backend_pid()); RETURN NEW; END $$"
            )
        )
        connection.execute(
            text(
                "CREATE TRIGGER drop_connection BEFORE INSERT ON idempotency_keys "
                "FOR EACH ROW EXECUTE FUNCTION drop_connection()"
            )
        )
    yield
    with engine.begin() as connection:
        connection.execute(text("DROP TRIGGER drop_connection ON idempotency_keys"))
        connection.execute(text("DROP FUNCTION drop_connection()"))


@pytest.mark.usefixtures("drop_connection_on_the_last_write")
def test_a_connection_lost_before_commit_is_503_and_writes_nothing(
    lenient_client: TestClient, balances: Balances, payments: Payments
) -> None:
    # Every write is flushed before COMMIT, so losing the connection there provably wrote nothing.
    response = lenient_client.post(
        "/bulk_payments", json=SAMPLE_REQUEST, headers={"Idempotency-Key": "lost"}
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "service_busy"
    assert balances() == SEED_BALANCES
    assert payments() == []


def test_total_beyond_bigint_is_denied(
    client: TestClient, balances: Balances, payments: Payments
) -> None:
    # 1000 of the largest amount add up to more than any BIGINT balance can hold.
    response = client.post(
        "/bulk_payments", json=pay(PINECREST, *[("999999999999999.99", LOPEZ)] * 1000)
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "insufficient_funds"
    assert response.json()["error"]["details"] == {
        "required": "999999999999999990.00",
        "available": "50000.00",
    }
    assert balances() == SEED_BALANCES
    assert payments() == []


def test_briefs_example_moves_money_exactly(client: TestClient, balances: Balances) -> None:
    # The request printed in the brief; the expected balances are worked out by hand.
    body = {
        "payer_firm_uuid": PINECREST,
        "payments": [
            {
                "amount": "1200.75",
                "payee_firm_uuid": LOPEZ,
                "description": "Bookkeeping cleanup, 3 clients",
            },
            {"amount": "300", "payee_firm_uuid": NAIR, "description": "Referral fee, 2 clients"},
        ],
    }

    response = client.post("/bulk_payments", json=body)

    assert response.status_code == 201
    assert response.json()["total_amount"] == "1500.75"
    assert [line["amount"] for line in response.json()["payments"]] == ["1200.75", "300.00"]
    assert balances() == {PINECREST: 4_849_925, LOPEZ: 170_075, NAIR: 230_000}
