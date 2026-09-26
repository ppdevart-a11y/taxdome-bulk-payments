import json
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from bulk_payments.db import get_session
from bulk_payments.main import create_app
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


@pytest.mark.parametrize(
    "content_type",
    [None, "text/plain", "application/x-www-form-urlencoded"],  # curl -d sends the last one
)
def test_body_not_sent_as_json_is_a_415_not_a_denial(
    client: TestClient, payments: Payments, content_type: str | None
) -> None:
    headers = {"Content-Type": content_type} if content_type else {}

    response = client.post("/bulk_payments", content=json.dumps(SAMPLE_REQUEST), headers=headers)

    assert response.status_code == 415
    assert response.json()["error"]["code"] == "unsupported_media_type"
    assert payments() == []


def test_json_with_charset_is_accepted(client: TestClient) -> None:
    response = client.post(
        "/bulk_payments",
        content=json.dumps(SAMPLE_REQUEST),
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    assert response.status_code == 201


def test_unknown_route_and_wrong_method_use_the_error_envelope(client: TestClient) -> None:
    not_found = client.get("/nope")
    wrong_method = client.get("/bulk_payments")

    assert not_found.status_code == 404
    assert not_found.json() == {"error": {"code": "not_found", "message": "Not Found"}}
    assert wrong_method.status_code == 405
    assert wrong_method.json()["error"]["code"] == "method_not_allowed"
    assert wrong_method.headers["Allow"] == "POST"


def test_unexpected_errors_use_the_envelope_without_internals() -> None:
    def broken_session() -> Iterator[Session]:
        raise RuntimeError("secret connection string")
        yield  # pragma: no cover

    app = create_app()
    app.dependency_overrides[get_session] = broken_session
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/bulk_payments", json=SAMPLE_REQUEST)

    assert response.status_code == 500
    assert response.json() == {"error": {"code": "internal_error", "message": "unexpected error"}}


def test_swagger_example_is_the_brief_sample(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    content = schema["paths"]["/bulk_payments"]["post"]["requestBody"]["content"]
    assert content["application/json"]["examples"]["brief"]["value"] == SAMPLE_REQUEST
