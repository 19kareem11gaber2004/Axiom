"""Readiness endpoint tests.

Mocks the check_postgres and check_redis functions in app.main so no
infrastructure is required. Verifies:
- 200 when both dependencies are up
- 503 when either dependency is down
"""

from fastapi.testclient import TestClient

import app.main as main_module

client = TestClient(main_module.app)


def _set_checks(pg_ok: bool, redis_ok: bool):
    main_module.check_postgres = lambda: pg_ok
    main_module.check_redis = lambda: redis_ok


def test_ready_200_when_both_up():
    _set_checks(True, True)
    r = client.get("/ready")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ready"
    assert body["checks"] == {"postgres": "up", "redis": "up"}


def test_ready_503_when_postgres_down():
    _set_checks(False, True)
    r = client.get("/ready")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "not_ready"
    assert body["checks"] == {"postgres": "down", "redis": "up"}


def test_ready_503_when_redis_down():
    _set_checks(True, False)
    r = client.get("/ready")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "not_ready"
    assert body["checks"] == {"postgres": "up", "redis": "down"}


def test_ready_503_when_both_down():
    _set_checks(False, False)
    r = client.get("/ready")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "not_ready"
    assert body["checks"] == {"postgres": "down", "redis": "down"}
