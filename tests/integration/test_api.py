import gzip
import json
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from bulk_payments import api
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


def test_sample_is_created_and_moves_money_exactly(
    client: TestClient, balances: Balances, payments: Payments
) -> None:
    response = client.post("/bulk_payments", json=SAMPLE_REQUEST)

    assert response.status_code == 201
    body = response.json()
    assert body["payer_firm_uuid"] == PINECREST
    assert body["total_amount"] == "13251.25"
    assert [p["amount"] for p in body["payments"]] == ["6250.00", "5800.50", "1200.75"]
    assert len({p["id"] for p in body["payments"]}) == 3

    # The sample's expected balances, worked out by hand: $36,748.75, $1,700.75, $14,050.50.
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


def post_raw(client: TestClient, body: bytes | str) -> Any:
    return client.post("/bulk_payments", content=body, headers={"Content-Type": "application/json"})


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(b'{"payer_firm_uuid": ', id="syntax error"),
        pytest.param(b'{"description": "\xff"}', id="invalid UTF-8"),
        pytest.param(b"[" * 100_000, id="nesting too deep"),
        pytest.param(b'{"amount": ' + b"1" * 5_000 + b"}", id="number too long"),
    ],
)
def test_unparseable_body_is_a_400(client: TestClient, payments: Payments, body: bytes) -> None:
    response = post_raw(client, body)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_json"
    assert payments() == []


def test_repeated_key_is_refused_not_guessed(client: TestClient, payments: Payments) -> None:
    # Python's parser keeps the last value, so this would otherwise pay $1,000.00.
    body = (
        f'{{"payer_firm_uuid": "{PINECREST}", "payments": [{{"amount": "1", "amount": "1000", '
        f'"payee_firm_uuid": "{LOPEZ}", "description": "twice"}}]}}'
    )

    response = post_raw(client, body)

    assert response.status_code == 400
    assert response.json()["error"] == {
        "code": "invalid_json",
        "message": "key 'amount' appears more than once in the same object",
    }
    assert payments() == []


def test_empty_body_is_a_validation_error(client: TestClient) -> None:
    response = post_raw(client, b"")

    assert response.status_code == 422
    assert response.json()["error"]["details"] == [
        {"field": "body", "type": "missing", "message": "Field required"}
    ]


def test_lone_surrogate_in_description_is_422(client: TestClient, payments: Payments) -> None:
    # Valid JSON, but a lone "\ud800" is not a character Postgres can store.
    body = json.dumps(pay(PINECREST, ("1", LOPEZ))).replace("payment 0", "a\\ud800b")

    response = post_raw(client, body)

    assert response.status_code == 422
    assert [detail["field"] for detail in response.json()["error"]["details"]] == [
        "payments.0.description"
    ]
    assert payments() == []


def test_exhausted_pool_is_503_and_writes_nothing(
    database_url: str, balances: Balances, payments: Payments
) -> None:
    engine = create_engine(database_url, pool_size=1, max_overflow=0, pool_timeout=0.2)
    factory = sessionmaker(engine, expire_on_commit=False)

    def session_override() -> Iterator[Session]:
        with factory() as session:
            yield session

    app = create_app()
    app.dependency_overrides[get_session] = session_override
    try:
        # Holding the pool's only connection leaves none for the request.
        with engine.connect(), TestClient(app) as client:
            response = client.post("/bulk_payments", json=SAMPLE_REQUEST)
    finally:
        engine.dispose()

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "1"
    assert response.json()["error"]["code"] == "service_busy"
    assert balances() == SEED_BALANCES
    assert payments() == []


def test_health(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.parametrize(
    "content_type",
    [
        None,
        "text/plain",
        "application/x-www-form-urlencoded",  # what curl -d sends
        "text/plain+json",  # FastAPI parses only application/ types as JSON
    ],
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


def test_swagger_example_is_the_sample_request(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    content = schema["paths"]["/bulk_payments"]["post"]["requestBody"]["content"]
    assert content["application/json"]["examples"]["sample"]["value"] == SAMPLE_REQUEST


def test_the_applied_idempotency_key_is_echoed(client: TestClient, payments: Payments) -> None:
    applied = client.post("/bulk_payments", json=SAMPLE_REQUEST, headers={"Idempotency-Key": "k-1"})
    # A misspelled header is ignored, and the missing echo is how a client can tell.
    misspelled = client.post(
        "/bulk_payments", json=SAMPLE_REQUEST, headers={"Idempotency_Key": "k-2"}
    )

    assert applied.status_code == 201
    assert applied.headers["Idempotency-Key"] == "k-1"
    assert misspelled.status_code == 201
    assert "Idempotency-Key" not in misspelled.headers


def test_head_health_answers_like_get(client: TestClient) -> None:
    assert client.head("/health").status_code == 200


def test_a_compressed_body_is_a_415(client: TestClient, payments: Payments) -> None:
    response = client.post(
        "/bulk_payments",
        content=gzip.compress(json.dumps(SAMPLE_REQUEST).encode()),
        headers={"Content-Type": "application/json", "Content-Encoding": "gzip"},
    )

    assert response.status_code == 415
    assert response.json()["error"]["code"] == "unsupported_media_type"
    assert payments() == []


def test_a_parser_failure_is_a_400_never_a_500(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def overflow(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        raise RecursionError

    monkeypatch.setattr(api, "_reject_duplicate_keys", overflow)

    response = post_raw(client, json.dumps(SAMPLE_REQUEST))

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_json"


def test_no_nesting_depth_near_the_parser_limit_is_a_500(client: TestClient) -> None:
    # A second parse used to overflow a few levels earlier than FastAPI's, outside its handler.
    statuses = {
        post_raw(client, '{"a":' * depth + "1" + "}" * depth).status_code
        for depth in range(9_950, 10_050)
    }

    assert 500 not in statuses
