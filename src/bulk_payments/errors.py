"""Domain errors. Each maps to one HTTP status and a stable machine-readable code."""

from typing import Any


class ServiceError(Exception):
    status_code: int = 422
    code: str = "unprocessable"

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details


class InsufficientFunds(ServiceError):
    code = "insufficient_funds"


class UnknownFirms(ServiceError):
    code = "unknown_firm"


class IdempotencyKeyReused(ServiceError):
    code = "idempotency_key_reused"


class FirmBusy(ServiceError):
    """A firm's row stayed locked past lock_timeout; safe for the client to retry."""

    status_code = 503
    code = "firm_busy"
