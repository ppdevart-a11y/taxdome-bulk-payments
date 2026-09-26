import copy
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from bulk_payments.errors import IdempotencyKeyReused
from bulk_payments.schemas import BulkPaymentRequest
from bulk_payments.service import Outcome, create_bulk_payment
from tests.integration.conftest import (
    AFTER_ONE_SAMPLE,
    LOPEZ,
    NAIR,
    PINECREST,
    SAMPLE_REQUEST,
    SEED_BALANCES,
    Balances,
    Payments,
    pay,
)


def post(client: TestClient, body: dict[str, Any], key: str | None = None) -> Any:
    headers = {"Idempotency-Key": key} if key is not None else {}
    return client.post("/bulk_payments", json=body, headers=headers)


def test_retry_with_same_key_replays_without_paying_twice(
    client: TestClient, balances: Balances, payments: Payments
) -> None:
    first = post(client, SAMPLE_REQUEST, key="order-42")
    retry = post(client, SAMPLE_REQUEST, key="order-42")

    assert first.status_code == retry.status_code == 201
    assert retry.json() == first.json()
    assert "Idempotent-Replayed" not in first.headers
    assert retry.headers["Idempotent-Replayed"] == "true"
    assert len(payments()) == 3
    assert balances() == AFTER_ONE_SAMPLE


def test_same_payment_spelled_differently_is_still_a_replay(
    client: TestClient, payments: Payments
) -> None:
    respelled = copy.deepcopy(SAMPLE_REQUEST)
    respelled["payer_firm_uuid"] = PINECREST.upper()
    respelled["payments"][0]["amount"] = "6250.00"

    post(client, SAMPLE_REQUEST, key="order-42")
    retry = post(client, respelled, key="order-42")

    assert retry.status_code == 201
    assert retry.headers["Idempotent-Replayed"] == "true"
    assert len(payments()) == 3


def test_reusing_a_key_for_a_different_request_is_rejected(
    client: TestClient, balances: Balances
) -> None:
    changed = copy.deepcopy(SAMPLE_REQUEST)
    changed["payments"][0]["amount"] = "6250.01"

    post(client, SAMPLE_REQUEST, key="order-42")
    response = post(client, changed, key="order-42")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "idempotency_key_reused"
    assert balances() == AFTER_ONE_SAMPLE


def test_declined_request_is_not_remembered(
    client: TestClient, engine: Engine, balances: Balances
) -> None:
    body = {
        "payer_firm_uuid": LOPEZ,
        "payments": [{"amount": "600", "payee_firm_uuid": NAIR, "description": "retainer"}],
    }
    assert post(client, body, key="retainer-1").status_code == 422

    # The client tops up and retries with the same key: judged afresh, not replayed.
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE firms SET balance_cents = 100000 WHERE uuid = :u"), {"u": LOPEZ}
        )
    retry = post(client, body, key="retainer-1")

    assert retry.status_code == 201
    assert "Idempotent-Replayed" not in retry.headers
    assert balances()[LOPEZ] == 40_000


def test_keys_are_scoped_to_the_payer(client: TestClient, payments: Payments) -> None:
    lopez_pays = {
        "payer_firm_uuid": LOPEZ,
        "payments": [{"amount": "1", "payee_firm_uuid": NAIR, "description": "fee"}],
    }

    assert post(client, SAMPLE_REQUEST, key="2026-09").status_code == 201
    response = post(client, lopez_pays, key="2026-09")

    assert response.status_code == 201
    assert "Idempotent-Replayed" not in response.headers
    assert len(payments()) == 4


def test_requests_without_a_key_are_not_deduplicated(
    client: TestClient, balances: Balances
) -> None:
    post(client, SAMPLE_REQUEST)
    post(client, SAMPLE_REQUEST)
    assert balances()[PINECREST] == SEED_BALANCES[PINECREST] - 2 * 1_325_125


def test_oversized_key_is_a_validation_error(client: TestClient) -> None:
    response = post(client, SAMPLE_REQUEST, key="k" * 256)
    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["field"] == "Idempotency-Key"


def test_concurrent_duplicates_pay_exactly_once(
    instances: list[sessionmaker[Session]], balances: Balances, payments: Payments
) -> None:
    # A client that times out and retries aggressively, across both instances.
    request = BulkPaymentRequest.model_validate(SAMPLE_REQUEST)
    barrier = threading.Barrier(10, timeout=30)

    def worker(index: int) -> Outcome:
        with instances[index % len(instances)]() as session:
            barrier.wait()
            return create_bulk_payment(session, request, idempotency_key="order-42")

    with ThreadPoolExecutor(max_workers=10) as pool:
        outcomes = list(pool.map(worker, range(10)))

    assert [outcome.replayed for outcome in outcomes].count(False) == 1
    assert len({outcome.response.model_dump_json() for outcome in outcomes}) == 1
    assert len(payments()) == 3
    assert balances() == AFTER_ONE_SAMPLE


def test_replay_after_the_payer_spent_everything(client: TestClient, balances: Balances) -> None:
    # The money already moved: the retry must replay before a funds check sees an empty balance.
    body = pay(LOPEZ, ("500", NAIR))

    first = post(client, body, key="all-in")
    retry = post(client, body, key="all-in")

    assert first.status_code == 201
    assert retry.status_code == 201
    assert retry.headers["Idempotent-Replayed"] == "true"
    assert retry.json() == first.json()
    assert balances() == {**SEED_BALANCES, LOPEZ: 0, NAIR: 250_000}


def test_same_key_with_different_bodies_at_once(
    instances: list[sessionmaker[Session]], balances: Balances, payments: Payments
) -> None:
    # One request wins the key. Its twins replay it; the other body is refused, never paid.
    to_lopez = BulkPaymentRequest.model_validate(pay(PINECREST, ("1", LOPEZ)))
    to_nair = BulkPaymentRequest.model_validate(pay(PINECREST, ("2", NAIR)))
    bodies = [to_lopez] * 5 + [to_nair] * 5
    barrier = threading.Barrier(len(bodies), timeout=30)

    def worker(index: int) -> str:
        with instances[index % len(instances)]() as session:
            barrier.wait()
            try:
                outcome = create_bulk_payment(session, bodies[index], idempotency_key="shared")
            except IdempotencyKeyReused:
                return "reused"
            return "replayed" if outcome.replayed else "created"

    with ThreadPoolExecutor(max_workers=len(bodies)) as pool:
        results = list(pool.map(worker, range(len(bodies))))

    assert results.count("created") == 1
    winner = bodies[results.index("created")]
    for body, result in zip(bodies, results, strict=True):
        assert result in (("created", "replayed") if body is winner else ("reused",))
    assert len(payments()) == 1
    paid, payee = (100, LOPEZ) if winner is to_lopez else (200, NAIR)
    expected = dict(SEED_BALANCES)
    expected[PINECREST] -= paid
    expected[payee] += paid
    assert balances() == expected


def two_lines() -> dict[str, Any]:
    return {
        "payer_firm_uuid": PINECREST,
        "payments": [
            {"amount": "1", "payee_firm_uuid": LOPEZ, "description": "first"},
            {"amount": "2", "payee_firm_uuid": NAIR, "description": "second"},
        ],
    }


def change_payee(body: dict[str, Any]) -> None:
    body["payments"][0]["payee_firm_uuid"] = NAIR


def change_amount(body: dict[str, Any]) -> None:
    body["payments"][0]["amount"] = "1.01"


def change_description(body: dict[str, Any]) -> None:
    body["payments"][0]["description"] = "other"


def reorder_lines(body: dict[str, Any]) -> None:
    body["payments"].reverse()


def add_a_line(body: dict[str, Any]) -> None:
    body["payments"].append(dict(body["payments"][0]))


@pytest.mark.parametrize(
    "change", [change_payee, change_amount, change_description, reorder_lines, add_a_line]
)
def test_a_key_reused_for_any_other_request_is_rejected(
    client: TestClient, payments: Payments, change: Callable[[dict[str, Any]], None]
) -> None:
    # Every field that decides who gets paid what must be in the fingerprint.
    assert post(client, two_lines(), key="order-7").status_code == 201
    different = two_lines()
    change(different)

    response = post(client, different, key="order-7")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "idempotency_key_reused"
    assert len(payments()) == 2


def test_the_same_description_in_another_unicode_form_is_a_replay(
    client: TestClient, payments: Payments
) -> None:
    composed = pay(PINECREST, ("1", LOPEZ))
    composed["payments"][0]["description"] = "Caf\u00e9"
    decomposed = copy.deepcopy(composed)
    decomposed["payments"][0]["description"] = "Cafe\u0301"

    first = post(client, composed, key="cafe")
    retry = post(client, decomposed, key="cafe")

    assert first.status_code == 201
    assert retry.status_code == 201
    assert retry.headers["Idempotent-Replayed"] == "true"
    assert len(payments()) == 1


def test_replay_after_a_payee_changed_its_uuid(client: TestClient, engine: Engine) -> None:
    # The key is looked up before any check on current state: the original request went through.
    body = pay(PINECREST, ("1", LOPEZ))
    first = post(client, body, key="moved")
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE firms SET uuid = :new WHERE uuid = :old"),
            {"new": "11111111-1111-4111-8111-111111111111", "old": LOPEZ},
        )

    retry = post(client, body, key="moved")

    assert first.status_code == 201
    assert retry.status_code == 201
    assert retry.headers["Idempotent-Replayed"] == "true"
    assert retry.json() == first.json()
