import copy
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from bulk_payments.schemas import BulkPaymentRequest
from bulk_payments.service import Outcome, create_bulk_payment
from tests.integration.conftest import LOPEZ, NAIR, PINECREST, SAMPLE_REQUEST, SEED_BALANCES

Balances = Callable[[], dict[str, int]]
Payments = Callable[[], list[tuple[int, int, int, str]]]
AFTER_ONE_SAMPLE = {PINECREST: 3_674_875, LOPEZ: 170_075, NAIR: 1_405_050}


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
    barrier = threading.Barrier(10)

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
