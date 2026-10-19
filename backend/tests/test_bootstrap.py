"""Tests for the bootstrap command (Phase 2, step 2.4).

The command creates the first administrator. It must reject an address
the login endpoint would also reject, otherwise it leaves behind an
account that can never be used.
"""

import uuid

import pytest

from app.auth.bootstrap import _validate_email, main


def test_an_ordinary_address_is_accepted():
    assert _validate_email("admin@example.com") == "admin@example.com"


def test_a_reserved_domain_address_is_rejected():
    """email-validator rejects special-use domains, and so must the CLI."""
    for address in ("admin@fresh.test", "admin@localhost", "admin@example.invalid"):
        with pytest.raises(ValueError) as exc:
            _validate_email(address)
        assert "reserved" in str(exc.value)


def test_a_malformed_address_is_rejected():
    with pytest.raises(ValueError):
        _validate_email("not-an-email")


def test_the_command_refuses_a_reserved_domain(monkeypatch):
    monkeypatch.setenv("BOOTSTRAP_PASSWORD", "some-password-123")
    exit_code = main(["--email", "admin@fresh.test", "--workspace", "W"])
    assert exit_code != 0


def test_the_command_refuses_a_short_password(monkeypatch):
    monkeypatch.setenv("BOOTSTRAP_PASSWORD", "short")
    exit_code = main(["--email", "admin-%s@example.com" % uuid.uuid4().hex])
    assert exit_code != 0


def test_the_command_refuses_an_over_long_password(monkeypatch):
    monkeypatch.setenv("BOOTSTRAP_PASSWORD", "a" * 100)
    exit_code = main(["--email", "admin-%s@example.com" % uuid.uuid4().hex])
    assert exit_code != 0
