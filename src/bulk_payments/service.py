"""The transfer: one bulk payment request is one database transaction.

Several load-balanced instances share nothing but PostgreSQL, so the database
is what makes concurrent requests safe. The transaction locks every firm it
touches, checks funds while holding those locks, then writes and commits.
"""

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass

import psycopg.errors
from sqlalchemy import func, insert, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.orm import Session

from bulk_payments.config import get_settings
from bulk_payments.errors import (
    FirmBusy,
    IdempotencyKeyReused,
    InsufficientFunds,
    ServiceBusy,
    UnknownFirms,
)
from bulk_payments.models import Firm, IdempotencyKey, Payment
from bulk_payments.money import format_cents
from bulk_payments.schemas import BulkPaymentRequest, BulkPaymentResponse, PaymentOut

# Ordered locking rules out deadlocks between our own transactions; retrying is
# a safety net for ones we cannot foresee. A failed attempt wrote nothing.
_MAX_ATTEMPTS = 3
_RETRYABLE = (psycopg.errors.DeadlockDetected, psycopg.errors.SerializationFailure)
# lock_timeout / statement_timeout fired: a firm is locked by long-running work.
_BUSY = (psycopg.errors.LockNotAvailable, psycopg.errors.QueryCanceled)

# Debit and credits in one round trip. Relative updates, so the statement is
# correct regardless of what the balance was read as.
_APPLY_DELTAS = text(
    "UPDATE firms SET balance_cents = firms.balance_cents + delta.cents "
    "FROM unnest(CAST(:firm_ids AS integer[]), CAST(:cents AS bigint[])) AS delta(firm_id, cents) "
    "WHERE firms.id = delta.firm_id"
)


@dataclass(frozen=True)
class Outcome:
    response: BulkPaymentResponse
    # True when an Idempotency-Key matched an earlier request and nothing was written.
    replayed: bool = False


def create_bulk_payment(
    session: Session, request: BulkPaymentRequest, idempotency_key: str | None = None
) -> Outcome:
    attempt = 1
    while True:
        try:
            with session.begin():
                return _transfer(session, request, idempotency_key)
        except PoolTimeoutError as exc:
            # No connection came free, so nothing reached the database.
            raise ServiceBusy("the service is busy, retry shortly") from exc
        except DBAPIError as exc:
            if isinstance(exc.orig, _BUSY):
                raise FirmBusy(
                    "a firm in this request is busy with another payment, retry shortly"
                ) from exc
            if isinstance(exc.orig, _RETRYABLE) and attempt < _MAX_ATTEMPTS:
                attempt += 1
                continue
            raise


def _transfer(
    session: Session, request: BulkPaymentRequest, idempotency_key: str | None
) -> Outcome:
    settings = get_settings()
    # SET LOCAL cannot take bind parameters; set_config(..., is_local => true) is equivalent.
    session.execute(select(func.set_config("lock_timeout", f"{settings.lock_timeout_ms}ms", True)))
    session.execute(
        select(func.set_config("statement_timeout", f"{settings.statement_timeout_ms}ms", True))
    )

    payer_uuid = str(request.payer_firm_uuid)
    uuids = {payer_uuid} | {str(payment.payee_firm_uuid) for payment in request.payments}

    # Lock the payer and every payee, always in id order, so two requests that
    # share firms queue up instead of deadlocking (A pays B while B pays A).
    # FOR NO KEY UPDATE is the lock an UPDATE takes anyway; unlike FOR UPDATE it
    # does not block inserts of payments whose foreign keys point at these rows.
    firms = session.execute(
        select(Firm.id, Firm.uuid, Firm.balance_cents)
        .where(Firm.uuid.in_(uuids))
        .order_by(Firm.id)
        .with_for_update(key_share=True)
    ).all()
    by_uuid = {firm.uuid: firm for firm in firms}

    if unknown := sorted(uuids - by_uuid.keys()):
        raise UnknownFirms("unknown firm uuid", {"unknown_firm_uuids": unknown})

    payer = by_uuid[payer_uuid]

    fingerprint = _fingerprint(request)
    if idempotency_key is not None:
        # Race-free without extra locking: we hold the payer's row lock, and every
        # request from this payer takes it first. A concurrent duplicate therefore
        # waits for the original to commit, then finds its key here and replays.
        stored = session.execute(
            select(IdempotencyKey.request_fingerprint, IdempotencyKey.response_body).where(
                IdempotencyKey.payer_firm_id == payer.id, IdempotencyKey.key == idempotency_key
            )
        ).one_or_none()
        if stored is not None:
            if stored.request_fingerprint != fingerprint:
                raise IdempotencyKeyReused(
                    "this Idempotency-Key was already used for a different request"
                )
            return Outcome(BulkPaymentResponse.model_validate(stored.response_body), replayed=True)

    total = request.total_cents
    # The balance was read under the lock: nobody can spend it before we commit.
    if total > payer.balance_cents:
        raise InsufficientFunds(
            "payer balance does not cover the total of the payments",
            {"required": format_cents(total), "available": format_cents(payer.balance_cents)},
        )

    deltas: defaultdict[int, int] = defaultdict(int)
    deltas[payer.id] -= total
    for payment in request.payments:
        deltas[by_uuid[str(payment.payee_firm_uuid)].id] += payment.amount_cents
    session.execute(_APPLY_DELTAS, {"firm_ids": list(deltas), "cents": list(deltas.values())})

    payment_ids = session.scalars(
        insert(Payment).returning(Payment.id, sort_by_parameter_order=True),
        [
            {
                "payer_firm_id": payer.id,
                "payee_firm_id": by_uuid[str(payment.payee_firm_uuid)].id,
                "amount_cents": payment.amount_cents,
                "description": payment.description,
            }
            for payment in request.payments
        ],
    ).all()

    response = BulkPaymentResponse(
        payer_firm_uuid=payer_uuid,
        total_amount=format_cents(total),
        payments=[
            PaymentOut(
                id=payment_id,
                payee_firm_uuid=str(payment.payee_firm_uuid),
                amount=format_cents(payment.amount_cents),
                description=payment.description,
            )
            for payment_id, payment in zip(payment_ids, request.payments, strict=True)
        ],
    )
    if idempotency_key is not None:
        # Same transaction as the payments: the key exists if and only if they do.
        # Declined requests roll back and store nothing, so a retry after a
        # top-up is judged afresh.
        session.add(
            IdempotencyKey(
                payer_firm_id=payer.id,
                key=idempotency_key,
                request_fingerprint=fingerprint,
                response_body=response.model_dump(mode="json"),
            )
        )
    return Outcome(response)


def _fingerprint(request: BulkPaymentRequest) -> str:
    """Hash of what the request means, not how it was spelled.

    "300" and "300.00", or upper- and lower-case uuids, are the same payment
    and must not trip the reused-key check on a legitimate retry.
    """
    canonical = {
        "payer": str(request.payer_firm_uuid),
        "payments": [
            [str(payment.payee_firm_uuid), payment.amount_cents, payment.description]
            for payment in request.payments
        ],
    }
    encoded = json.dumps(canonical, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()
