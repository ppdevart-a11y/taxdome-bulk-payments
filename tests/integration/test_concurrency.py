"""What must hold when many instances hit the same firms at the same moment.

Workers draw sessions from two separate engines, standing in for two app
instances with their own connection pools; the database is the only thing
they share. A Barrier releases them together so the transactions overlap.
"""

import random
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from bulk_payments import service
from bulk_payments.config import get_settings
from bulk_payments.errors import InsufficientFunds, ServiceBusy
from bulk_payments.money import format_cents
from bulk_payments.schemas import BulkPaymentRequest
from tests.integration.conftest import (
    AFTER_ONE_SAMPLE,
    LOPEZ,
    NAIR,
    PINECREST,
    SAMPLE_REQUEST,
    SEED_BALANCES,
)

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
    barrier = threading.Barrier(len(requests), timeout=30)

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
        started = time.monotonic()
        response = client.post("/bulk_payments", json=body)
        elapsed = time.monotonic() - started

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "1"
    assert response.json()["error"]["code"] == "firm_busy"
    assert elapsed < 2  # the 200 ms lock_timeout fired, not the 10 s statement_timeout


def test_statement_timeout_is_503(
    client: TestClient, engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A lock wait that outlasts statement_timeout surfaces as 57014, not 55P03.
    monkeypatch.setattr(get_settings(), "lock_timeout_ms", 10_000)
    monkeypatch.setattr(get_settings(), "statement_timeout_ms", 200)
    body = {
        "payer_firm_uuid": PINECREST,
        "payments": [{"amount": "1", "payee_firm_uuid": NAIR, "description": "slow"}],
    }

    with engine.connect() as blocker, blocker.begin():
        blocker.execute(text("SELECT 1 FROM firms WHERE uuid = :uuid FOR UPDATE"), {"uuid": NAIR})
        started = time.monotonic()
        response = client.post("/bulk_payments", json=body)
        elapsed = time.monotonic() - started

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "1"
    assert response.json()["error"]["code"] == "firm_busy"
    assert elapsed < 2  # the 200 ms statement_timeout fired, not the 10 s lock_timeout


def test_a_stalled_instance_releases_its_locks(
    instances: list[sessionmaker[Session]],
    balances: Balances,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Instance A stalls while holding the payer's lock; Postgres must end it so B can pay.
    monkeypatch.setattr(get_settings(), "idle_in_transaction_timeout_ms", 300)
    stalled, resume = threading.Event(), threading.Event()
    real_fingerprint = service._fingerprint

    def stall_the_first_request(request: BulkPaymentRequest) -> str:
        if not stalled.is_set():
            stalled.set()
            resume.wait(timeout=10)
        return real_fingerprint(request)

    monkeypatch.setattr(service, "_fingerprint", stall_the_first_request)
    body = request(PINECREST, ("1", LOPEZ))

    def pay(instance: sessionmaker[Session]) -> str:
        with instance() as session:
            try:
                service.create_bulk_payment(session, body)
            except ServiceBusy:
                return "busy"
            return "created"

    with ThreadPoolExecutor(max_workers=1) as pool:
        frozen = pool.submit(pay, instances[0])
        assert stalled.wait(timeout=10)
        started = time.monotonic()
        assert pay(instances[1]) == "created"
        assert time.monotonic() - started < 3
        resume.set()
        # A's session was ended before COMMIT, so it wrote nothing and is safe to retry.
        assert frozen.result(timeout=10) == "busy"

    assert balances()[PINECREST] == SEED_BALANCES[PINECREST] - 100


def wait_until_a_session_waits_for_a_lock(engine: Engine) -> None:
    deadline = time.monotonic() + 5
    with engine.connect() as connection:
        while time.monotonic() < deadline:
            waiting = connection.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND wait_event_type = 'Lock'"
                )
            ).scalar_one()
            connection.rollback()  # pg_stat_activity is snapshotted once per transaction
            if waiting:
                return
            time.sleep(0.01)
    raise AssertionError("no session started waiting for a lock")


@pytest.mark.parametrize(
    "plan",
    [
        pytest.param("-c enable_seqscan=off -c enable_bitmapscan=off", id="uuid index scan"),
        pytest.param("-c enable_indexscan=off -c enable_bitmapscan=off", id="table scan"),
    ],
)
def test_locks_are_taken_in_id_order(
    engine: Engine,
    database_url: str,
    balances: Balances,
    monkeypatch: pytest.MonkeyPatch,
    plan: str,
) -> None:
    monkeypatch.setattr(get_settings(), "lock_timeout_ms", 30_000)
    # Firm 4 gets the lowest uuid and firm 1 moves to the table's end: neither order is id order.
    early_uuid = "00000000-0000-4000-8000-000000000001"
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO firms (name, balance_cents, uuid) VALUES ('Early LLC', 0, :uuid)"),
            {"uuid": early_uuid},
        )
        connection.execute(text("UPDATE firms SET name = name WHERE id = 1"))
        by_uuid = connection.execute(text("SELECT id FROM firms ORDER BY uuid")).scalars().all()
        physical = connection.execute(text("SELECT id FROM firms")).scalars().all()
    assert by_uuid == [4, 1, 2, 3]
    assert physical == [2, 3, 4, 1]
    # Pin the plan, so the order a lock query would follow without ORDER BY id is known.
    planned = create_engine(database_url, connect_args={"options": plan})

    def pay_firms_4_and_3() -> None:
        with Session(planned) as session:
            body = request(PINECREST, ("1", early_uuid), ("1", NAIR))
            service.create_bulk_payment(session, body)

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            with engine.connect() as blocker, blocker.begin():
                blocker.execute(text("SELECT 1 FROM firms WHERE id = 4 FOR UPDATE"))
                payment = pool.submit(pay_firms_4_and_3)
                wait_until_a_session_waits_for_a_lock(engine)
                # Queued on firm 4, the request must already hold firm 1: only id order does that.
                with engine.connect() as probe, pytest.raises(OperationalError) as refused:
                    probe.execute(text("SELECT 1 FROM firms WHERE id = 1 FOR UPDATE NOWAIT"))
                assert isinstance(refused.value.orig, psycopg.errors.LockNotAvailable)
            payment.result(timeout=30)
    finally:
        planned.dispose()

    assert balances()[PINECREST] == SEED_BALANCES[PINECREST] - 200


@pytest.fixture
def deadlock_once(engine: Engine) -> Iterator[Callable[[], int]]:
    """The first idempotency-key insert fails as a deadlock. Yields how often the trigger ran."""
    with engine.begin() as connection:
        connection.execute(text("CREATE SEQUENCE deadlock_once"))
        connection.execute(
            text(
                "CREATE FUNCTION deadlock_once() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
                "IF nextval('deadlock_once') = 1 THEN "
                "RAISE EXCEPTION 'injected by the test' USING ERRCODE = 'deadlock_detected'; "
                "END IF; RETURN NEW; END $$"
            )
        )
        connection.execute(
            text(
                "CREATE TRIGGER deadlock_once BEFORE INSERT ON idempotency_keys "
                "FOR EACH ROW EXECUTE FUNCTION deadlock_once()"
            )
        )

    def runs() -> int:
        with engine.connect() as connection:
            return int(
                connection.execute(text("SELECT last_value FROM deadlock_once")).scalar_one()
            )

    yield runs
    with engine.begin() as connection:
        connection.execute(text("DROP TRIGGER deadlock_once ON idempotency_keys"))
        connection.execute(text("DROP FUNCTION deadlock_once()"))
        connection.execute(text("DROP SEQUENCE deadlock_once"))


def test_deadlock_at_commit_is_retried(
    client: TestClient, deadlock_once: Callable[[], int], balances: Balances
) -> None:
    # The key row is the transaction's last write, so the retry has to redo all of it.
    response = client.post(
        "/bulk_payments", json=SAMPLE_REQUEST, headers={"Idempotency-Key": "retry-me"}
    )

    assert response.status_code == 201
    assert deadlock_once() == 2  # one attempt refused, one committed
    assert balances() == AFTER_ONE_SAMPLE
