"""Structured error envelope tests.

Proves every API error path returns the same shape:
    {"error": {"code", "message", "retryable", "request_id"}}
"""

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient

import app.main as main_module
from app.api.errors import (
    ConflictError,
    DocumentProcessingError,
    ForbiddenError,
    NotFoundError,
    RateLimitError,
    ValidationError,
)
from app.api.schemas import TokenRequest

client = TestClient(main_module.app, raise_server_exceptions=False)

ERROR_KEYS = {"code", "message", "retryable", "request_id"}


@pytest.fixture
def error_routes():
    """Register temporary routes that raise each error type."""
    router = APIRouter()

    @router.get("/_test/notfound")
    def _notfound():
        raise NotFoundError("Document not found.")

    @router.get("/_test/forbidden")
    def _forbidden():
        raise ForbiddenError()

    @router.get("/_test/conflict")
    def _conflict():
        raise ConflictError()

    @router.get("/_test/ratelimit")
    def _ratelimit():
        raise RateLimitError()

    @router.get("/_test/processing")
    def _processing():
        raise DocumentProcessingError("The PDF did not contain readable text.")

    @router.post("/_test/login")
    def _login(payload: TokenRequest):
        return {"email": payload.email}

    @router.get("/_test/boom")
    def _boom():
        raise RuntimeError("internal detail that must never reach the client")

    main_module.app.include_router(router)
    yield router
    for route in list(main_module.app.routes):
        if getattr(route, "path", "").startswith("/_test"):
            main_module.app.router.routes.remove(route)


def assert_error_envelope(response, expected_code, expected_status):
    """Assert the response uses the consistent Axiom error envelope."""
    assert response.status_code == expected_status
    body = response.json()
    assert set(body.keys()) == {"error"}
    assert set(body["error"].keys()) == ERROR_KEYS
    assert body["error"]["code"] == expected_code
    assert isinstance(body["error"]["message"], str)
    assert isinstance(body["error"]["retryable"], bool)


def test_not_found_uses_envelope(error_routes):
    assert_error_envelope(client.get("/_test/notfound"), "NOT_FOUND", 404)


def test_forbidden_uses_envelope(error_routes):
    assert_error_envelope(client.get("/_test/forbidden"), "FORBIDDEN", 403)


def test_conflict_uses_envelope(error_routes):
    assert_error_envelope(client.get("/_test/conflict"), "INVALID_STATE", 409)


def test_rate_limit_is_retryable(error_routes):
    r = client.get("/_test/ratelimit")
    assert_error_envelope(r, "RATE_LIMITED", 429)
    assert r.json()["error"]["retryable"] is True


def test_document_processing_error_uses_envelope(error_routes):
    r = client.get("/_test/processing")
    assert_error_envelope(r, "DOCUMENT_PROCESSING_FAILED", 422)
    assert r.json()["error"]["retryable"] is False


def test_unhandled_exception_does_not_leak_internals(error_routes):
    r = client.get("/_test/boom")
    assert_error_envelope(r, "INTERNAL_ERROR", 500)
    assert "internal detail" not in r.json()["error"]["message"]


def test_request_id_in_error_matches_header(error_routes):
    r = client.get("/_test/notfound")
    assert r.json()["error"]["request_id"] == r.headers.get("x-request-id")


def test_request_validation_uses_envelope(error_routes):
    r = client.post("/_test/login", json={"email": "not-an-email", "password": "x"})
    assert_error_envelope(r, "VALIDATION_ERROR", 422)


def test_validation_error_class_uses_400():
    exc = ValidationError("bad input")
    assert exc.code == "VALIDATION_ERROR"
    assert exc.status_code == 400
    assert exc.retryable is False
