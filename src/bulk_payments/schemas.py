from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator, model_validator
from pydantic_core import PydanticCustomError

from bulk_payments.money import parse_amount

# Bounds the size of one transaction and therefore how long it holds row locks.
MAX_PAYMENTS_PER_REQUEST = 1_000
MAX_DESCRIPTION_LENGTH = 1_000


class PaymentIn(BaseModel):
    # Unknown fields are rejected: on a money endpoint a typo like "ammount"
    # must fail loudly rather than be silently ignored.
    model_config = ConfigDict(extra="forbid")

    amount: StrictStr = Field(examples=["1200.75"])
    payee_firm_uuid: UUID
    description: StrictStr = Field(min_length=1, max_length=MAX_DESCRIPTION_LENGTH)

    @field_validator("amount")
    @classmethod
    def _amount_is_valid(cls, value: str) -> str:
        try:
            parse_amount(value)
        except ValueError as exc:
            raise PydanticCustomError("invalid_amount", str(exc)) from exc
        return value

    @field_validator("description")
    @classmethod
    def _description_is_storable(cls, value: str) -> str:
        if not value.strip():
            raise PydanticCustomError("blank_description", "must not be blank")
        # PostgreSQL TEXT cannot store NUL; without this check it is a 500.
        if "\x00" in value:
            raise PydanticCustomError("invalid_description", "must not contain NUL characters")
        return value

    @property
    def amount_cents(self) -> int:
        return parse_amount(self.amount)


class BulkPaymentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    payer_firm_uuid: UUID
    payments: list[PaymentIn] = Field(min_length=1, max_length=MAX_PAYMENTS_PER_REQUEST)

    @model_validator(mode="after")
    def _no_payment_to_self(self) -> Self:
        for payment in self.payments:
            if payment.payee_firm_uuid == self.payer_firm_uuid:
                raise PydanticCustomError("self_payment", "a firm cannot pay itself")
        return self

    @property
    def total_cents(self) -> int:
        return sum(payment.amount_cents for payment in self.payments)


class PaymentOut(BaseModel):
    id: int
    payee_firm_uuid: str
    amount: str
    description: str


class BulkPaymentResponse(BaseModel):
    payer_firm_uuid: str
    total_amount: str
    payments: list[PaymentOut]


class ErrorDetail(BaseModel):
    code: str
    message: str
    details: dict[str, object] | list[dict[str, object]] | None = None


class ErrorResponse(BaseModel):
    error: ErrorDetail
