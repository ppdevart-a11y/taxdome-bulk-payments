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
    """A firm stayed locked too long, or a deadlock outlasted the retries.

    Postgres rolled the transaction back, so nothing was written; safe to retry.
    """

    status_code = 503
    code = "firm_busy"


class ServiceBusy(ServiceError):
    """No database connection came free in time, or the database failed before COMMIT.

    Nothing was written; safe to retry.
    """

    status_code = 503
    code = "service_busy"


class UnsupportedMediaType(ServiceError):
    """The body wasn't sent as uncompressed JSON. Kept apart from 422, which means "denied"."""

    status_code = 415
    code = "unsupported_media_type"


class InvalidJson(ServiceError):
    """The body isn't one unambiguous JSON document.

    Raised inside FastAPI's body parse, so it travels out wrapped in api.BodyRejected.
    """

    status_code = 400
    code = "invalid_json"
