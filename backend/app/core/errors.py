"""Domain exceptions and the handlers that turn them into JSON responses."""

from __future__ import annotations

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.core.logging import get_logger

logger = get_logger(__name__)


class VoiceDeskError(Exception):
    """Base class for all handled application errors."""

    status_code: int = status.HTTP_400_BAD_REQUEST
    code: str = "error"

    def __init__(self, message: str, *, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class NotFoundError(VoiceDeskError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "not_found"


class ConflictError(VoiceDeskError):
    status_code = status.HTTP_409_CONFLICT
    code = "conflict"


class ValidationError(VoiceDeskError):
    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    code = "validation_error"


class AuthenticationError(VoiceDeskError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "authentication_error"


class PermissionError_(VoiceDeskError):
    status_code = status.HTTP_403_FORBIDDEN
    code = "permission_denied"


class RateLimitError(VoiceDeskError):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    code = "rate_limited"


class ComplianceError(VoiceDeskError):
    """A TRAI / regulatory rule blocked the action."""

    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    code = "compliance_blocked"


class ExternalServiceError(VoiceDeskError):
    status_code = status.HTTP_502_BAD_GATEWAY
    code = "external_service_error"


class QuotaExceededError(VoiceDeskError):
    status_code = status.HTTP_402_PAYMENT_REQUIRED
    code = "quota_exceeded"


def _payload(code: str, message: str, details: dict | None = None) -> dict:
    body: dict = {"error": {"code": code, "message": message}}
    if details:
        body["error"]["details"] = details
    return body


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(VoiceDeskError)
    async def _handle_domain_error(_: Request, exc: VoiceDeskError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=_payload(exc.code, exc.message, exc.details),
        )

    @app.exception_handler(RequestValidationError)
    async def _handle_request_validation(
        _: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content=_payload(
                "validation_error",
                "Request validation failed.",
                {"errors": _jsonable_errors(exc.errors())},
            ),
        )

    @app.exception_handler(Exception)
    async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=_payload("internal_error", "An unexpected error occurred."),
        )


def _jsonable_errors(errors: list[dict]) -> list[dict]:
    """Strip non-serialisable ``ctx`` values out of pydantic error dicts."""
    cleaned = []
    for err in errors:
        item = {k: v for k, v in err.items() if k != "ctx"}
        item["loc"] = [str(part) for part in err.get("loc", ())]
        cleaned.append(item)
    return cleaned
