"""What must hold when many instances hit the same firms at the same moment.

Workers draw sessions from two separate engines, standing in for two app
instances with their own connection pools; the database is the only thing
they share. A Barrier releases them together so the transactions overlap.
"""

import random
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from bulk_payments import service
from bulk_payments.config import get_settings
from bulk_payments.errors import InsufficientFunds
from bulk_payments.money import format_cents
from bulk_payments.schemas import BulkPaymentRequest
from tests.integration.conftest import LOPEZ, NAIR, PINECREST, SEED_BALANCES

FIRMS = [PINECREST, LOPEZ, NAIR]
Balances = Callable[[], dict[str, int]]

# Received minus paid, per firm, according to the payments table.
NET_FLOW_BY_FIRM = text("""
    SELECT f.uuid,
           COALESCE(SUM(p.amount_cents) FILTER (WHERE p.payee_firm_id = f.id), 0)
         - COALESCE(SUM(p.amount_cents) FILTER (WHERE p.payer_firm_id = f.id), 0)
    FROM firms f
    LEFT JOIN payments p ON f.id IN (p.payer_firm_id, p.payee_firm_id)
    GROUP BY f.uuid
""")


@pytest.fixture
def instances(database_url: str) -> Iterator[list[sessionmaker[Session]]]:
    engines = [create_engine(database_url, pool_size=40, max_overflow=0) for _ in range(2)]
    yield [sessionmaker(engine) for engine in engines]
    for engine in engines:
        engine.dispose()


def request(payer: str, *lines: tuple[str, str]) -> BulkPaymentRequest:
    return BulkPaymentRequest.model_validate(
        {
            "payer_firm_uuid": payer,
            "payments": [
                {"amount": amount, "payee_firm_uuid": payee, "description": "concurrency test"}
                for amount, payee in lines
            ],
        }
    )


def run_concurrently(
    instances: list[sessionmaker[Session]], requests: list[BulkPaymentRequest]
) -> list[str]:
    """Fire all requests at once; return "created" or "denied" per request.

    Anything else (deadlock, constraint violation, timeout) propagates and fails the test.
    """
    barrier = threading.Barrier(len(requests))

    def worker(index: int) -> str:
        with instances[index % len(instances)]() as session:
            barrier.wait()
            try:
                service.create_bulk_payment(session, requests[index])
            except InsufficientFunds:
                return "denied"
            return "created"

    with ThreadPoolExecutor(max_workers=len(requests)) as pool:
        return list(pool.map(worker, range(len(requests))))


def test_concurrent_requests_cannot_overdraw_the_payer(
    instances: list[sessionmaker[Session]], balances: Balances
) -> None:
    # Lopez has $500.00: enough for six $75.00 requests, not twenty.
    outcomes = run_concurrently(instances, [request(LOPEZ, ("75", NAIR))] * 20)

    assert outcomes.count("created") == 6
    assert outcomes.count("denied") == 14
    assert balances()[LOPEZ] == 50_000 - 6 * 7_500
    assert balances()[NAIR] == 200_000 + 6 * 7_500


def test_firms_paying_each_other_do_not_deadlock(
    instances: list[sessionmaker[Session]], balances: Balances, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No retries: a single deadlock would surface as an exception and fail the test.
    monkeypatch.setattr(service, "_MAX_ATTEMPTS", 1)
    # Every request touches all three firms, each listing its payees in a
    # different order: the classic lock-ordering deadlock if locks followed it.
    cycle = [
        request(PINECREST, ("1", NAIR), ("1", LOPEZ)),
        request(LOPEZ, ("1", PINECREST), ("1", NAIR)),
        request(NAIR, ("1", LOPEZ), ("1", PINECREST)),
    ]

    outcomes = run_concurrently(instances, cycle * 20)

    assert outcomes == ["created"] * 60
    # Each firm paid out $2.00 twenty times and received $1.00 forty times.
    assert balances() == SEED_BALANCES


def test_random_storm_conserves_money_and_reconciles_with_payments(
    instances: list[sessionmaker[Session]], balances: Balances, engine: Engine
) -> None:
    rng = random.Random(2026)  # noqa: S311 - reproducible test data, not crypto
    requests = []
    for _ in range(60):
        payer = rng.choice(FIRMS)
        payees = [firm for firm in FIRMS if firm != payer]
        lines = [(format_cents(rng.randint(1, 900_000)), rng.choice(payees)) for _ in range(3)]
        requests.append(request(payer, *lines))

    outcomes = run_concurrently(instances, requests)

    assert set(outcomes) <= {"created", "denied"}
    assert "created" in outcomes
    assert "denied" in outcomes
    final = balances()
    assert sum(final.values()) == sum(SEED_BALANCES.values())
    assert all(cents >= 0 for cents in final.values())
    # Every balance equals its seed plus what the payments table says it received minus paid.
    with engine.connect() as connection:
        net = {uuid: cents for uuid, cents in connection.execute(NET_FLOW_BY_FIRM)}
    assert final == {uuid: SEED_BALANCES[uuid] + net[uuid] for uuid in FIRMS}


def test_a_firm_locked_too_long_fails_fast_with_503(
    client: TestClient, engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "lock_timeout_ms", 200)
    body = {
        "payer_firm_uuid": PINECREST,
        "payments": [{"amount": "1", "payee_firm_uuid": NAIR, "description": "hot row"}],
    }

    with engine.connect() as blocker, blocker.begin():
        blocker.execute(text("SELECT 1 FROM firms WHERE uuid = :uuid FOR UPDATE"), {"uuid": NAIR})
        response = client.post("/bulk_payments", json=body)

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "1"
    assert response.json()["error"]["code"] == "firm_busy"
