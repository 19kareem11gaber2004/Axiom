"""Tests for the CORS configuration (Phase 2, step 2.6).

CORS is enforced by the browser, so these tests check the response
headers the browser would look at.
"""

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import app

client = TestClient(app, raise_server_exceptions=False)

ALLOWED_ORIGIN = "http://localhost:5173"
OTHER_ORIGIN = "https://evil.example.com"


def test_allowed_origin_is_returned_on_a_simple_request():
    r = client.get("/health", headers={"Origin": ALLOWED_ORIGIN})
    assert r.headers.get("access-control-allow-origin") == ALLOWED_ORIGIN


def test_disallowed_origin_gets_no_cors_header():
    r = client.get("/health", headers={"Origin": OTHER_ORIGIN})
    assert "access-control-allow-origin" not in r.headers


def test_preflight_allows_the_authorization_header():
    r = client.options(
        "/api/v1/documents",
        headers={
            "Origin": ALLOWED_ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization",
        },
    )
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") == ALLOWED_ORIGIN
    allowed = r.headers.get("access-control-allow-headers", "").lower()
    assert "authorization" in allowed


def test_preflight_allows_the_custom_workspace_header():
    """Without this the browser never sends the real request."""
    r = client.options(
        "/api/v1/documents",
        headers={
            "Origin": ALLOWED_ORIGIN,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "x-workspace-id",
        },
    )
    assert r.status_code == 200
    allowed = r.headers.get("access-control-allow-headers", "").lower()
    assert "x-workspace-id" in allowed


def test_preflight_allows_the_request_id_header():
    r = client.options(
        "/api/v1/documents",
        headers={
            "Origin": ALLOWED_ORIGIN,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "x-request-id",
        },
    )
    allowed = r.headers.get("access-control-allow-headers", "").lower()
    assert "x-request-id" in allowed


def test_request_id_header_is_exposed_to_the_browser():
    r = client.get("/health", headers={"Origin": ALLOWED_ORIGIN})
    exposed = r.headers.get("access-control-expose-headers", "").lower()
    assert "x-request-id" in exposed


def test_preflight_from_a_disallowed_origin_is_not_approved():
    r = client.options(
        "/api/v1/documents",
        headers={
            "Origin": OTHER_ORIGIN,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        },
    )
    assert "access-control-allow-origin" not in r.headers


def test_credentials_are_not_enabled_by_default():
    """Axiom uses a Bearer token, not cookies, so credentials are off."""
    r = client.get("/health", headers={"Origin": ALLOWED_ORIGIN})
    assert "access-control-allow-credentials" not in r.headers


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def test_origins_are_parsed_from_a_comma_separated_list():
    settings = Settings(cors_origins="http://a.test, http://b.test ,")
    assert settings.cors_origin_list == ["http://a.test", "http://b.test"]


def test_a_single_origin_is_accepted():
    settings = Settings(cors_origins="http://only.test")
    assert settings.cors_origin_list == ["http://only.test"]


def test_wildcard_with_credentials_is_rejected():
    """Starlette would echo any Origin back and enable credentials."""
    settings = Settings(cors_origins="*", cors_allow_credentials=True)
    with pytest.raises(ValueError) as exc:
        _ = settings.cors_origin_list
    assert "must not contain" in str(exc.value)


def test_wildcard_without_credentials_is_allowed():
    settings = Settings(cors_origins="*", cors_allow_credentials=False)
    assert settings.cors_origin_list == ["*"]


def test_error_responses_carry_cors_headers():
    """A 401 must be readable by the browser, not reported as a CORS error."""
    r = client.get("/api/v1/auth/me", headers={"Origin": ALLOWED_ORIGIN})
    assert r.status_code == 401
    assert r.headers.get("access-control-allow-origin") == ALLOWED_ORIGIN


def test_unhandled_errors_also_carry_cors_headers():
    """A 500 must not be reported to the browser as a CORS error.

    Starlette attaches a plain Exception handler to ServerErrorMiddleware,
    which sits outside the user middleware stack. The error boundary
    middleware is registered inside CORSMiddleware precisely so that a 500
    response passes through the CORS layer.
    """
    from fastapi import APIRouter

    router = APIRouter()

    @router.get("/_test/unhandled")
    def _unhandled():
        raise RuntimeError("internal detail that must not leak")

    app.include_router(router)
    try:
        r = client.get("/_test/unhandled", headers={"Origin": ALLOWED_ORIGIN})
    finally:
        for route in list(app.routes):
            if getattr(route, "path", "") == "/_test/unhandled":
                app.router.routes.remove(route)

    assert r.status_code == 500
    assert r.headers.get("access-control-allow-origin") == ALLOWED_ORIGIN
    assert r.json()["error"]["code"] == "INTERNAL_ERROR"
    assert "internal detail" not in r.text
    # The request ID must still correlate the header and the body.
    assert r.headers["x-request-id"] == r.json()["error"]["request_id"]


def test_the_error_boundary_is_inside_cors():
    """Middleware order is what makes the test above pass."""
    names = [mw.cls.__name__ for mw in app.user_middleware]
    assert names.index("CORSMiddleware") < names.index("ErrorBoundaryMiddleware")
