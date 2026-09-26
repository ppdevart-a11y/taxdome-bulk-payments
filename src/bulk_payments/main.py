from collections.abc import Mapping
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from bulk_payments.api import DuplicateKey, router
from bulk_payments.errors import InvalidJson, ServiceError
from bulk_payments.schemas import ErrorDetail, ErrorResponse


def _error(
    status_code: int,
    code: str,
    message: str,
    details: dict[str, Any] | list[dict[str, Any]] | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    body = ErrorResponse(error=ErrorDetail(code=code, message=message, details=details))
    return JSONResponse(body.model_dump(exclude_none=True), status_code, headers=headers)


def create_app() -> FastAPI:
    app = FastAPI(
        title="Bulk payments",
        summary="One firm pays many firms from its platform balance, all or nothing.",
        version="1.0.0",
    )
    app.include_router(router)

    @app.exception_handler(ServiceError)
    async def service_error(_: Request, exc: ServiceError) -> JSONResponse:
        retryable = exc.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
        headers = {"Retry-After": "1"} if retryable else None
        return _error(exc.status_code, exc.code, exc.message, exc.details, headers)

    @app.exception_handler(Exception)
    async def unexpected_error(_: Request, exc: Exception) -> JSONResponse:
        # Starlette still logs the traceback; the client gets the usual envelope, no internals.
        return _error(status.HTTP_500_INTERNAL_SERVER_ERROR, "internal_error", "unexpected error")

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        if exc.status_code == status.HTTP_400_BAD_REQUEST:
            # FastAPI's catch-all for a body it couldn't decode: invalid UTF-8,
            # nesting too deep, a number too long to convert. Same code as a syntax error.
            message = exc.detail if isinstance(exc, DuplicateKey) else "body is not valid JSON"
            return _error(exc.status_code, InvalidJson.code, message)
        # 404, 405 and friends use the same envelope as every other error.
        code = HTTPStatus(exc.status_code).phrase.lower().replace(" ", "_")
        return _error(exc.status_code, code, str(exc.detail), headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        errors = exc.errors()
        if any(error["type"] == "json_invalid" for error in errors):
            return _error(status.HTTP_400_BAD_REQUEST, InvalidJson.code, "body is not valid JSON")
        details = [
            {
                # ("body", "payments", 0, "amount") -> "payments.0.amount"
                "field": ".".join(str(part) for part in error["loc"][1:]) or error["loc"][0],
                "type": error["type"],
                "message": error["msg"],
            }
            for error in errors
        ]
        return _error(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "validation_error",
            "request is invalid",
            details,
        )

    return app


app = create_app()
