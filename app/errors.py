"""Domain errors. Each maps to an HTTP status and a stable machine-readable code."""

from __future__ import annotations


class LedgerError(Exception):
    http_status = 400
    code = "BAD_REQUEST"

    def __init__(self, message: str, *, code: str | None = None, http_status: int | None = None,
                 extra: dict | None = None, headers: dict | None = None):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        if http_status:
            self.http_status = http_status
        self.extra = extra or {}
        self.headers = headers or {}

    def to_body(self) -> dict:
        body = {"error": {"code": self.code, "message": self.message}}
        if self.extra:
            body["error"].update(self.extra)
        return body


class Unauthorized(LedgerError):
    http_status = 401
    code = "UNAUTHORIZED"


class Forbidden(LedgerError):
    http_status = 403
    code = "FORBIDDEN"


class NotFound(LedgerError):
    http_status = 404
    code = "NOT_FOUND"


class Conflict(LedgerError):
    http_status = 409
    code = "CONFLICT"


class ValidationFailed(LedgerError):
    http_status = 422
    code = "VALIDATION_FAILED"


class IdempotencyKeyReused(Conflict):
    code = "IDEMPOTENCY_KEY_REUSED"


class RequestInProgress(Conflict):
    code = "REQUEST_IN_PROGRESS"


class TooManyRequests(LedgerError):
    http_status = 429
    code = "TOO_MANY_REQUESTS"
