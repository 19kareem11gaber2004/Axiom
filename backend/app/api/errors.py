"""Structured error handling for Axiom API.

All API failures are returned in a single consistent envelope:

    {
        "error": {
            "code": "NOT_FOUND",
            "message": "Document not found.",
            "retryable": false,
            "request_id": "req_abc123"
        }
    }

Clients can branch on `code` and `retryable` instead of parsing message
strings, and internal exception details never reach the client.
"""

import logging
from typing import Optional

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger("axiom.api")


class AxiomError(Exception):
    """Base class for application errors that map to a client response.

    Attributes:
        code: Stable machine-readable identifier (e.g. "NOT_FOUND").
        message: Human-readable, safe to display to end users.
        status_code: HTTP status code to return.
        retryable: Whether the client may retry the same request later.
    """

    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.retryable = retryable


class ValidationError(AxiomError):
    """Request payload failed semantic validation."""

    def __init__(self, message: str) -> None:
        super().__init__(
            code="VALIDATION_ERROR",
            message=message,
            status_code=status.HTTP_400_BAD_REQUEST,
            retryable=False,
        )


class AuthenticationError(AxiomError):
    """The caller could not be authenticated.

    Covers a missing, malformed, expired or unrecognised access token, and
    a token whose account no longer exists. The message never reveals
    which of these applied, so it cannot be used to probe for valid
    accounts.
    """

    def __init__(self, message: str = "Authentication credentials are invalid.") -> None:
        super().__init__(
            code="UNAUTHENTICATED",
            message=message,
            status_code=status.HTTP_401_UNAUTHORIZED,
            retryable=False,
        )
        # RFC 6750: a 401 response should advertise the accepted scheme.
        self.www_authenticate = "Bearer"


class NotFoundError(AxiomError):
    """Requested resource does not exist or is not visible to the caller."""

    def __init__(self, message: str = "Resource not found.") -> None:
        super().__init__(
            code="NOT_FOUND",
            message=message,
            status_code=status.HTTP_404_NOT_FOUND,
            retryable=False,
        )


class ForbiddenError(AxiomError):
    """Caller is authenticated but lacks the required permission."""

    def __init__(self, message: str = "Insufficient permissions.") -> None:
        super().__init__(
            code="FORBIDDEN",
            message=message,
            status_code=status.HTTP_403_FORBIDDEN,
            retryable=False,
        )


class ConflictError(AxiomError):
    """Request conflicts with the current state of the resource."""

    def __init__(self, message: str = "Conflicting request.") -> None:
        super().__init__(
            code="INVALID_STATE",
            message=message,
            status_code=status.HTTP_409_CONFLICT,
            retryable=False,
        )


class RateLimitError(AxiomError):
    """Caller exceeded an allowed request rate."""

    def __init__(self, message: str = "Too many requests. Please try again later.") -> None:
        super().__init__(
            code="RATE_LIMITED",
            message=message,
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            retryable=True,
        )


class DocumentProcessingError(AxiomError):
    """Document could not be processed.

    `retryable` is False for permanent failures (corrupt or unsupported
    file) and True for transient ones (provider or network timeouts).
    """

    def __init__(self, message: str, retryable: bool = False) -> None:
        super().__init__(
            code="DOCUMENT_PROCESSING_FAILED",
            message=message,
            status_code=422,
            retryable=retryable,
        )


def _error_body(code: str, message: str, retryable: bool, request_id: Optional[str]) -> dict:
    return {
        "error": {
            "code": code,
            "message": message,
            "retryable": retryable,
            "request_id": request_id,
        }
    }


def _request_id(request: Request) -> Optional[str]:
    """Read the request ID assigned by the request-ID middleware."""
    return getattr(request.state, "request_id", None)


class ErrorBoundaryMiddleware(BaseHTTPMiddleware):
    """Turns unhandled exceptions into the Axiom error envelope.

    This middleware must be registered *inside* CORSMiddleware. Starlette
    attaches a plain ``@app.exception_handler(Exception)`` handler to
    ServerErrorMiddleware, which sits outside the whole user middleware
    stack. A 500 produced there would carry no CORS headers, so a browser
    would report a CORS error instead of a server error - a genuinely
    confusing debugging trap.

    Catching the exception here keeps the 500 response inside the CORS
    layer, where the CORS headers are added normally.
    """

    async def dispatch(self, request, call_next):
        try:
            return await call_next(request)
        except Exception:  # noqa: BLE001 - this is the boundary
            logger.exception(
                "Unhandled error",
                extra={"request_id": _request_id(request), "path": request.url.path},
            )
            return JSONResponse(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                content=_error_body(
                    code="INTERNAL_ERROR",
                    message="An unexpected error occurred.",
                    retryable=False,
                    request_id=_request_id(request),
                ),
            )


def register_error_handlers(app: FastAPI) -> None:
    """Attach Axiom's error handlers to the FastAPI application.

    Handlers registered here take precedence over FastAPI's defaults, so
    every error path (application, validation, and unhandled) produces the
    same response envelope.
    """

    @app.exception_handler(AxiomError)
    async def handle_axiom_error(request: Request, exc: AxiomError) -> JSONResponse:
        headers = {}
        # RFC 6750 requires a 401 to advertise the accepted scheme.
        www_authenticate = getattr(exc, "www_authenticate", None)
        if www_authenticate:
            headers["WWW-Authenticate"] = www_authenticate
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_body(
                code=exc.code,
                message=exc.message,
                retryable=exc.retryable,
                request_id=_request_id(request),
            ),
            headers=headers or None,
        )

    @app.exception_handler(RequestValidationError)
    async def handle_request_validation(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """Convert FastAPI/Pydantic body validation failures into the envelope."""
        first = exc.errors()[0] if exc.errors() else {}
        location = ".".join(str(part) for part in first.get("loc", []) if part != "body")
        message = first.get("msg", "Invalid request payload.")
        if location:
            message = f"{location}: {message}"
        return JSONResponse(
            status_code=422,
            content=_error_body(
                code="VALIDATION_ERROR",
                message=message,
                retryable=False,
                request_id=_request_id(request),
            ),
        )

    # Unhandled exceptions are handled by ErrorBoundaryMiddleware, which
    # is registered inside CORSMiddleware so that a 500 response still
    # carries CORS headers. Registering a handler for Exception here
    # instead would attach it to ServerErrorMiddleware, outside the CORS
    # layer, and the browser would see a CORS error rather than a 500.
