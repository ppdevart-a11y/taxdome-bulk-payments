import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from bulk_payments.schemas import (
    MAX_DESCRIPTION_LENGTH,
    MAX_FIELDS_PER_OBJECT,
    MAX_PAYMENTS_PER_REQUEST,
    BulkPaymentRequest,
)

SAMPLE = json.loads((Path(__file__).parents[2] / "scripts" / "sample_request.json").read_text())
PAYER = SAMPLE["payer_firm_uuid"]
PAYEE = SAMPLE["payments"][0]["payee_firm_uuid"]


def request(**payment_overrides: Any) -> dict[str, Any]:
    payment = {"amount": "10", "payee_firm_uuid": PAYEE, "description": "x"}
    payment.update(payment_overrides)
    return {"payer_firm_uuid": PAYER, "payments": [payment]}


def parse(body: dict[str, Any]) -> BulkPaymentRequest:
    # What production validates: the output of json.loads, in Python mode.
    return BulkPaymentRequest.model_validate(json.loads(json.dumps(body)))


def error_types(body: dict[str, Any]) -> set[str]:
    with pytest.raises(ValidationError) as exc_info:
        parse(body)
    return {error["type"] for error in exc_info.value.errors()}


def test_sample_totals_13251_25() -> None:
    parsed = parse(SAMPLE)
    assert sum(p.amount_cents for p in parsed.payments) == 1_325_125
    assert [p.amount_cents for p in parsed.payments] == [625_000, 580_050, 120_075]


def test_uuids_are_normalised_to_canonical_lowercase() -> None:
    body = request(payee_firm_uuid=PAYEE.upper())
    body["payer_firm_uuid"] = PAYER.upper()
    parsed = parse(body)
    assert str(parsed.payer_firm_uuid) == PAYER
    assert str(parsed.payments[0].payee_firm_uuid) == PAYEE


def test_amount_as_json_number_is_rejected() -> None:
    # Floats are lossy; the contract says amounts are strings.
    assert error_types(request(amount=1200.75)) == {"string_type"}


@pytest.mark.parametrize("amount", ["0", "-1", "1.234", "abc"])
def test_invalid_amount_strings_are_rejected(amount: str) -> None:
    assert error_types(request(amount=amount)) == {"invalid_amount"}


def test_unknown_fields_are_rejected() -> None:
    assert error_types(request(ammount="10")) == {"extra_forbidden"}
    body = request()
    body["currency"] = "USD"
    assert error_types(body) == {"extra_forbidden"}


def test_payments_must_not_be_empty() -> None:
    assert error_types({"payer_firm_uuid": PAYER, "payments": []}) == {"too_short"}


def test_payments_are_capped() -> None:
    body = request()
    body["payments"] = body["payments"] * (MAX_PAYMENTS_PER_REQUEST + 1)
    assert error_types(body) == {"too_long"}


def test_limits_are_inclusive() -> None:
    body = request(description="x" * MAX_DESCRIPTION_LENGTH)
    body["payments"] = body["payments"] * MAX_PAYMENTS_PER_REQUEST
    parsed = parse(body)
    assert len(parsed.payments) == MAX_PAYMENTS_PER_REQUEST
    assert len(parsed.payments[0].description) == MAX_DESCRIPTION_LENGTH


def test_firm_cannot_pay_itself() -> None:
    assert error_types(request(payee_firm_uuid=PAYER)) == {"self_payment"}


def test_each_self_payment_is_reported_at_its_line() -> None:
    body = request()
    body["payments"] = [
        {"amount": "1", "payee_firm_uuid": payee, "description": "x"}
        for payee in (PAYER, PAYEE, PAYER)
    ]
    with pytest.raises(ValidationError) as exc_info:
        parse(body)
    assert [(error["type"], error["loc"]) for error in exc_info.value.errors()] == [
        ("self_payment", ("payments", 0, "payee_firm_uuid")),
        ("self_payment", ("payments", 2, "payee_firm_uuid")),
    ]


def test_unknown_fields_are_named_up_to_the_field_limit() -> None:
    # A payment has 3 fields; a few typos on top are each reported by name.
    extra = {f"typo{i}": "x" for i in range(MAX_FIELDS_PER_OBJECT - 3)}
    with pytest.raises(ValidationError) as exc_info:
        parse(request(**extra))
    assert [error["loc"][-1] for error in exc_info.value.errors()] == list(extra)


@pytest.mark.parametrize("where", ["request", "payment"])
def test_an_object_over_the_field_limit_is_one_error(where: str) -> None:
    extra = {f"k{i}": 0 for i in range(100_000)}
    body = request(**extra) if where == "payment" else {**request(), **extra}
    with pytest.raises(ValidationError) as exc_info:
        parse(body)
    assert [error["type"] for error in exc_info.value.errors()] == ["too_many_fields"]


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        ("", "string_too_short"),
        ("   ", "blank_description"),
        ("a\x00b", "invalid_description"),
        ("x" * 1001, "string_too_long"),
        (42, "string_type"),
    ],
)
def test_description_validation(description: object, expected: str) -> None:
    assert error_types(request(description=description)) == {expected}


@pytest.mark.parametrize("value", ["not-a-uuid", "", 123])
def test_malformed_uuid_is_rejected(value: object) -> None:
    assert error_types(request(payee_firm_uuid=value)) <= {"uuid_parsing", "uuid_type"}


def test_missing_fields_are_reported() -> None:
    assert error_types({"payments": [{}]}) == {"missing"}
