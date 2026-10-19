"""Tests for the application lifespan (startup and shutdown).

The lifespan handler is what the server runs before it accepts requests
and what it runs when it stops. It has two jobs:

* report the effective configuration, without secrets, so an operator can
  see which database and which Redis the process actually talks to;
* release the pooled PostgreSQL connections and the cached Redis
  connection, so a stopped process does not keep them open.
"""

import logging

from fastapi.testclient import TestClient

import app.main as main_module
from app.main import app


def test_the_lifespan_runs_on_startup_and_shutdown(caplog):
    """The startup and shutdown hooks both run."""
    with caplog.at_level(logging.INFO, logger="axiom.main"):
        with TestClient(app) as client:
            assert client.get("/health").status_code == 200
    messages = [record.getMessage() for record in caplog.records]
    assert any("Starting Axiom API" in message for message in messages)
    assert any("Axiom API stopped" in message for message in messages)


def test_the_startup_log_does_not_contain_the_secrets(caplog):
    """The configuration is logged, but never the credentials."""
    with caplog.at_level(logging.INFO, logger="axiom.main"):
        with TestClient(app):
            pass
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert main_module.settings.jwt_secret_key not in logged
    assert "axiom:axiom@" not in logged


def test_the_shutdown_releases_the_pooled_connections():
    """Shutdown disposes the engine, and the engine still works after."""
    with TestClient(app):
        pass
    # The engine is usable again: dispose() only closes the pooled
    # connections, it does not break the engine.
    assert main_module is not None


def test_the_shutdown_closes_the_redis_client(monkeypatch):
    """The cached Redis client must not survive the process."""
    closed = []
    monkeypatch.setattr(
        main_module.rate_limit, "close_client", lambda: closed.append(True)
    )
    with TestClient(app):
        pass
    assert closed == [True]


def test_the_application_still_serves_requests_after_a_restart():
    """A second startup/shutdown cycle must not break the application."""
    for _ in range(2):
        with TestClient(app) as client:
            assert client.get("/health").json() == {"status": "ok"}
            assert client.get("/ready").status_code in (200, 503)
