import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from bulk_payments.schemas import MAX_PAYMENTS_PER_REQUEST, BulkPaymentRequest

SAMPLE = json.loads((Path(__file__).parents[2] / "scripts" / "sample_request.json").read_text())
PAYER = SAMPLE["payer_firm_uuid"]
PAYEE = SAMPLE["payments"][0]["payee_firm_uuid"]


def request(**payment_overrides: Any) -> dict[str, Any]:
    payment = {"amount": "10", "payee_firm_uuid": PAYEE, "description": "x"}
    payment.update(payment_overrides)
    return {"payer_firm_uuid": PAYER, "payments": [payment]}


def error_types(body: dict[str, Any]) -> set[str]:
    with pytest.raises(ValidationError) as exc_info:
        BulkPaymentRequest.model_validate_json(json.dumps(body))
    return {error["type"] for error in exc_info.value.errors()}


def test_spec_sample_totals_13251_25() -> None:
    parsed = BulkPaymentRequest.model_validate_json(json.dumps(SAMPLE))
    assert parsed.total_cents == 1_325_125
    assert [p.amount_cents for p in parsed.payments] == [625_000, 580_050, 120_075]


def test_uuids_are_normalised_to_canonical_lowercase() -> None:
    body = request(payee_firm_uuid=PAYEE.upper())
    body["payer_firm_uuid"] = PAYER.upper()
    parsed = BulkPaymentRequest.model_validate_json(json.dumps(body))
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


def test_firm_cannot_pay_itself() -> None:
    assert error_types(request(payee_firm_uuid=PAYER)) == {"self_payment"}


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
