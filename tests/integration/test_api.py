from collections.abc import Callable
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from bulk_payments.money import format_cents
from tests.integration.conftest import LOPEZ, NAIR, PINECREST, SAMPLE_REQUEST, SEED_BALANCES

Balances = Callable[[], dict[str, int]]
Payments = Callable[[], list[tuple[int, int, int, str]]]


def pay(payer: str, *lines: tuple[str, str]) -> dict[str, Any]:
    return {
        "payer_firm_uuid": payer,
        "payments": [
            {"amount": amount, "payee_firm_uuid": payee, "description": f"payment {i}"}
            for i, (amount, payee) in enumerate(lines)
        ],
    }


def test_spec_sample_is_created_and_moves_money_exactly(
    client: TestClient, balances: Balances, payments: Payments
) -> None:
    response = client.post("/bulk_payments", json=SAMPLE_REQUEST)

    assert response.status_code == 201
    body = response.json()
    assert body["payer_firm_uuid"] == PINECREST
    assert body["total_amount"] == "13251.25"
    assert [p["amount"] for p in body["payments"]] == ["6250.00", "5800.50", "1200.75"]
    assert len({p["id"] for p in body["payments"]}) == 3

    # The spec's expected balances: $36,748.75, $1,700.75 and $14,050.50.
    assert balances() == {PINECREST: 3_674_875, LOPEZ: 170_075, NAIR: 1_405_050}
    assert payments() == [
        (1, 3, 625_000, "Overflow returns, August 2026"),
        (1, 3, 580_050, "Amended returns, August 2026"),
        (1, 2, 120_075, "Bookkeeping cleanup, 3 clients"),
    ]


def test_each_returned_id_is_the_row_for_that_line(client: TestClient, engine: Engine) -> None:
    body = client.post("/bulk_payments", json=SAMPLE_REQUEST).json()

    with engine.connect() as connection:
        rows = connection.execute(text("SELECT id, amount_cents, description FROM payments"))
        stored = {id_: (format_cents(cents), description) for id_, cents, description in rows}
    assert {p["id"]: (p["amount"], p["description"]) for p in body["payments"]} == stored
    assert [p["description"] for p in body["payments"]] == [
        p["description"] for p in SAMPLE_REQUEST["payments"]
    ]


def test_insufficient_funds_denies_the_whole_request(
    client: TestClient, balances: Balances, payments: Payments
) -> None:
    # Each line fits Lopez's $500.00 on its own; together they are one cent over.
    response = client.post("/bulk_payments", json=pay(LOPEZ, ("250", NAIR), ("250.01", PINECREST)))

    assert response.status_code == 422
    assert response.json() == {
        "error": {
            "code": "insufficient_funds",
            "message": "payer balance does not cover the total of the payments",
            "details": {"required": "500.01", "available": "500.00"},
        }
    }
    assert balances() == SEED_BALANCES
    assert payments() == []


def test_paying_the_exact_balance_leaves_zero(client: TestClient, balances: Balances) -> None:
    response = client.post("/bulk_payments", json=pay(LOPEZ, ("500", NAIR)))

    assert response.status_code == 201
    assert balances()[LOPEZ] == 0
    assert balances()[NAIR] == 250_000


def test_repeating_the_sample_is_denied_once_funds_run_out(
    client: TestClient, balances: Balances
) -> None:
    # $50,000.00 covers the $13,251.25 sample three times ($39,753.75), not four.
    statuses = [client.post("/bulk_payments", json=SAMPLE_REQUEST).status_code for _ in range(4)]

    assert statuses == [201, 201, 201, 422]
    assert balances()[PINECREST] == 5_000_000 - 3 * 1_325_125


def test_unknown_payee_is_denied_and_nothing_moves(
    client: TestClient, balances: Balances, payments: Payments
) -> None:
    stranger = "00000000-0000-4000-8000-000000000000"
    response = client.post("/bulk_payments", json=pay(PINECREST, ("1", NAIR), ("1", stranger)))

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "unknown_firm"
    assert response.json()["error"]["details"] == {"unknown_firm_uuids": [stranger]}
    assert balances() == SEED_BALANCES
    assert payments() == []


def test_unknown_payer_is_denied(client: TestClient) -> None:
    response = client.post(
        "/bulk_payments", json=pay("00000000-0000-4000-8000-000000000000", ("1", NAIR))
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "unknown_firm"


def test_uppercase_uuids_resolve_to_the_same_firms(client: TestClient, balances: Balances) -> None:
    response = client.post("/bulk_payments", json=pay(LOPEZ.upper(), ("1", NAIR.upper())))

    assert response.status_code == 201
    assert balances()[NAIR] == 200_100


def test_validation_errors_point_at_the_field(client: TestClient, payments: Payments) -> None:
    body = pay(PINECREST, ("1", NAIR))
    body["payments"][0]["amount"] = 1200.75  # a JSON number, not a string

    response = client.post("/bulk_payments", json=body)

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    assert error["details"] == [
        {
            "field": "payments.0.amount",
            "type": "string_type",
            "message": "Input should be a valid string",
        }
    ]
    assert payments() == []


def test_self_payment_is_rejected(client: TestClient) -> None:
    response = client.post("/bulk_payments", json=pay(NAIR, ("1", NAIR)))
    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["type"] == "self_payment"


def test_malformed_json_is_a_400(client: TestClient) -> None:
    response = client.post(
        "/bulk_payments",
        content=b'{"payer_firm_uuid": ',
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_json"


def test_health(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
