from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Integer,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Firm(Base):
    __tablename__ = "firms"
    __table_args__ = (
        # Safety net under the application-level funds check: even a buggy code
        # path cannot leave a firm with a negative balance.
        CheckConstraint("balance_cents >= 0", name="firms_balance_non_negative"),
    )

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    # BIGINT rather than the spec's INTEGER: a 4-byte Postgres INTEGER caps a
    # balance at $21,474,836.47, which a large practice can exceed.
    balance_cents: Mapped[int] = mapped_column(BigInteger)
    uuid: Mapped[str] = mapped_column(Text, unique=True)


class Payment(Base):
    __tablename__ = "payments"
    __table_args__ = (
        CheckConstraint("amount_cents > 0", name="payments_amount_positive"),
        CheckConstraint("payer_firm_id <> payee_firm_id", name="payments_not_to_self"),
    )

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    payer_firm_id: Mapped[int] = mapped_column(ForeignKey("firms.id"), index=True)
    payee_firm_id: Mapped[int] = mapped_column(ForeignKey("firms.id"), index=True)
    amount_cents: Mapped[int] = mapped_column(BigInteger)
    description: Mapped[str] = mapped_column(Text)


class IdempotencyKey(Base):
    """Result of a bulk payment made with an ``Idempotency-Key`` header.

    Written in the same transaction as the payments, so a key exists if and only
    if its payments were committed.
    """

    __tablename__ = "idempotency_keys"

    payer_firm_id: Mapped[int] = mapped_column(ForeignKey("firms.id"), primary_key=True)
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    request_fingerprint: Mapped[str] = mapped_column(Text)
    response_body: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
