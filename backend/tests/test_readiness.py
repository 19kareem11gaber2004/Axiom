"""Tests for the readiness endpoint's dependency checks (Phase 2, step 9.2).

These guard a bug that was present for several milestones: the readiness
check passed the SQLAlchemy URL to raw psycopg, which does not understand
the "+psycopg" dialect selector. The application connected fine (SQLAlchemy
strips it) while /ready reported the database as down.
"""

import app.main as main_module
from app.config import settings
from app.db.session import psycopg_dsn


def test_psycopg_dsn_strips_the_dialect_selector():
    """The DSN handed to psycopg must not contain "+psycopg"."""
    dsn = psycopg_dsn()
    assert "+psycopg" not in dsn
    assert dsn.startswith("postgresql://")


def test_psycopg_dsn_keeps_the_host_port_and_database():
    dsn = psycopg_dsn()
    assert "@" in dsn
    assert "/" in dsn.split("@", 1)[1]


def test_psycopg_dsn_accepts_an_explicit_url():
    dsn = psycopg_dsn("postgresql+psycopg://u:p@host:1234/dbname")
    assert dsn == "postgresql://u:p@host:1234/dbname"


def test_the_readiness_check_uses_a_psycopg_compatible_dsn(monkeypatch):
    """check_postgres must not pass the raw SQLAlchemy URL to psycopg."""
    seen = {}

    def fake_connect(dsn, **kwargs):
        seen["dsn"] = dsn
        raise RuntimeError("stop here - the DSN is what matters")

    import psycopg

    monkeypatch.setattr(psycopg, "connect", fake_connect)
    assert main_module.check_postgres() is False
    assert "+psycopg" not in seen.get("dsn", "")


def test_readiness_reports_ready_when_both_dependencies_are_up(monkeypatch):
    monkeypatch.setattr(main_module, "check_postgres", lambda: True)
    monkeypatch.setattr(main_module, "check_redis", lambda: True)
    from fastapi.testclient import TestClient

    r = TestClient(main_module.app).get("/ready")
    assert r.status_code == 200
    assert r.json()["status"] == "ready"


def test_readiness_reports_not_ready_when_postgres_is_down(monkeypatch):
    monkeypatch.setattr(main_module, "check_postgres", lambda: False)
    monkeypatch.setattr(main_module, "check_redis", lambda: True)
    from fastapi.testclient import TestClient

    r = TestClient(main_module.app).get("/ready")
    assert r.status_code == 503
    assert r.json()["checks"]["postgres"] == "down"


def test_the_configured_url_is_a_sqlalchemy_url():
    """Guards the assumption the DSN helper depends on."""
    assert "+psycopg" in settings.database_url
