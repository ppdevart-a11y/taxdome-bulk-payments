from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from bulk_payments.db import get_session
from bulk_payments.schemas import BulkPaymentRequest, BulkPaymentResponse, ErrorResponse
from bulk_payments.service import create_bulk_payment

router = APIRouter()

SessionDep = Annotated[Session, Depends(get_session)]
IdempotencyKeyHeader = Annotated[
    str | None,
    Header(
        alias="Idempotency-Key",
        min_length=1,
        max_length=255,
        description=(
            "Optional. Retrying with the same key returns the original 201 instead of "
            "paying twice. Scoped to the payer firm."
        ),
    ),
]

_ERRORS: dict[int | str, dict[str, Any]] = {
    status.HTTP_400_BAD_REQUEST: {"model": ErrorResponse, "description": "Malformed JSON"},
    status.HTTP_422_UNPROCESSABLE_CONTENT: {
        "model": ErrorResponse,
        "description": (
            "Denied: insufficient funds, unknown firm, invalid request, "
            "or an Idempotency-Key reused for a different request"
        ),
    },
    status.HTTP_503_SERVICE_UNAVAILABLE: {
        "model": ErrorResponse,
        "description": "A firm is locked by another payment for too long; safe to retry",
    },
}


@router.post(
    "/bulk_payments",
    status_code=status.HTTP_201_CREATED,
    responses=_ERRORS,
    summary="Pay several firms from the payer's balance, all or nothing",
)
def post_bulk_payment(
    body: BulkPaymentRequest,
    session: SessionDep,
    response: Response,
    idempotency_key: IdempotencyKeyHeader = None,
) -> BulkPaymentResponse:
    outcome = create_bulk_payment(session, body, idempotency_key)
    if outcome.replayed:
        response.headers["Idempotent-Replayed"] = "true"
    return outcome.response


@router.get("/health", summary="Liveness and database connectivity, for the load balancer")
def health(session: SessionDep) -> JSONResponse:
    try:
        session.execute(text("SELECT 1"))
    except SQLAlchemyError:
        return JSONResponse({"status": "database unavailable"}, status.HTTP_503_SERVICE_UNAVAILABLE)
    return JSONResponse({"status": "ok"})
