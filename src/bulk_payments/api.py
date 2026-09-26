import json
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Header, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from bulk_payments.db import get_session
from bulk_payments.errors import InvalidJson, UnsupportedMediaType
from bulk_payments.schemas import BulkPaymentRequest, BulkPaymentResponse, ErrorResponse
from bulk_payments.service import create_bulk_payment

router = APIRouter()

SessionDep = Annotated[Session, Depends(get_session)]

# Pre-fills Swagger UI (/docs) with scripts/sample_request.json: "Try it out" works on seeded data.
_SAMPLE_REQUEST: dict[str, Any] = {
    "payer_firm_uuid": "3f1c9a2e-7b4d-4c1e-9a55-2d8e6f0b7c41",
    "payments": [
        {
            "amount": "6250",
            "payee_firm_uuid": "e5f18b3c-2a9d-4c07-8e6b-1d4a7f9c3b25",
            "description": "Overflow returns, August 2026",
        },
        {
            "amount": "5800.5",
            "payee_firm_uuid": "e5f18b3c-2a9d-4c07-8e6b-1d4a7f9c3b25",
            "description": "Amended returns, August 2026",
        },
        {
            "amount": "1200.75",
            "payee_firm_uuid": "8b2e4c71-0d3a-4f6e-b1c9-5a7d2e9f4c10",
            "description": "Bookkeeping cleanup, 3 clients",
        },
    ],
}
BulkPaymentBody = Annotated[
    BulkPaymentRequest,
    Body(
        openapi_examples={
            "sample": {"summary": "Sample request for the seeded firms", "value": _SAMPLE_REQUEST}
        }
    ),
]
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


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: set[str] = set()
    for key, _ in pairs:
        if key in seen:
            raise InvalidJson(f"key {key!r} appears more than once in the same object")
        seen.add(key)
    return dict(pairs)


async def require_json(request: Request) -> None:
    """Answer 415 for a body FastAPI won't parse as JSON, and 400 for one that repeats a key."""
    media_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    # FastAPI parses only these; any other body fails validation as a 422, which here means denied.
    parsed_as_json = media_type == "application/json" or (
        media_type.startswith("application/") and media_type.endswith("+json")
    )
    if not parsed_as_json:
        raise UnsupportedMediaType("send the body as JSON with 'Content-Type: application/json'")
    # FastAPI keeps the last of a repeated key; a body that states two amounts is refused.
    if body := await request.body():
        json.loads(body, object_pairs_hook=_reject_duplicate_keys)


_ERRORS: dict[int | str, dict[str, Any]] = {
    status.HTTP_400_BAD_REQUEST: {
        "model": ErrorResponse,
        "description": "Body is not valid JSON, or repeats a key in an object",
    },
    status.HTTP_415_UNSUPPORTED_MEDIA_TYPE: {
        "model": ErrorResponse,
        "description": "Body not sent as application/json",
    },
    status.HTTP_422_UNPROCESSABLE_CONTENT: {
        "model": ErrorResponse,
        "description": (
            "Denied: insufficient funds, unknown firm, invalid request, "
            "or an Idempotency-Key reused for a different request"
        ),
    },
    status.HTTP_503_SERVICE_UNAVAILABLE: {
        "model": ErrorResponse,
        "description": (
            "A firm is locked by another payment for too long, or no database connection "
            "came free. Nothing was written; safe to retry"
        ),
    },
}


@router.post(
    "/bulk_payments",
    status_code=status.HTTP_201_CREATED,
    responses=_ERRORS,
    dependencies=[Depends(require_json)],
    summary="Pay several firms from the payer's balance, all or nothing",
)
def post_bulk_payment(
    body: BulkPaymentBody,
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
