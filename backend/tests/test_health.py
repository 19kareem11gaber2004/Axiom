"""Health endpoint tests.

No infrastructure required: uses FastAPI TestClient against the in-process
app. Verifies /health returns 200 and every response carries a request ID.
"""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health_returns_ok():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_health_includes_request_id():
    r = client.get("/health")
    rid = r.headers.get("x-request-id")
    assert rid is not None
    assert rid.startswith("req_")


def test_health_generates_request_id_when_absent():
    r = client.get("/health")
    rid = r.headers.get("x-request-id")
    assert rid is not None
    assert len(rid) <= 64


def test_health_rejects_invalid_request_id():
    r = client.get("/health", headers={"x-request-id": "bad id with spaces!!"})
    rid = r.headers.get("x-request-id")
    assert rid is not None
    assert rid != "bad id with spaces!!"
    assert rid.startswith("req_")


def test_health_echoes_valid_request_id():
    r = client.get("/health", headers={"x-request-id": "req_abc123"})
    assert r.headers.get("x-request-id") == "req_abc123"
