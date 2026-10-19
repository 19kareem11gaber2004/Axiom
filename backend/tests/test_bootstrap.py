"""Tests for the bootstrap command (Phase 2, step 2.4).

The command creates the first administrator. It must reject an address
the login endpoint would also reject, otherwise it leaves behind an
account that can never be used.

It must also fail helpfully when it cannot reach the intended database:
the raw driver message blames the password, which is a red herring when
the real problem is that the command is talking to a different server.
"""

import uuid

import pytest

from app.auth.bootstrap import (
    PasswordUnavailableError,
    _can_prompt_securely,
    _connection_help,
    _database_target,
    _read_password,
    _validate_email,
    main,
)


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


# ---------------------------------------------------------------------------
# Failing to reach the intended database
# ---------------------------------------------------------------------------

def test_the_connection_help_names_the_target():
    """The message must say which server was tried.

    "password authentication failed" alone sends the operator hunting
    for a password problem when the real problem is that the command
    reached a different PostgreSQL.
    """
    help_text = _connection_help(RuntimeError("password authentication failed for user"))
    assert _database_target() in help_text
    assert "password authentication failed" in help_text
    assert "docker compose run --rm api" in help_text


def test_the_connection_help_never_prints_the_password():
    help_text = _connection_help(RuntimeError("connection failed"))
    assert "axiom:axiom@" not in help_text
    assert "postgresql+psycopg" not in help_text


def test_the_database_target_has_no_password():
    """The target is host:port/database - the credentials are stripped."""
    target = _database_target()
    assert "@" not in target, "the target must not contain credentials"
    assert ":" in target
    assert "/" in target


def test_a_connection_failure_exits_with_the_actionable_message(monkeypatch, capsys):
    """A database that cannot be reached must not look like a crash."""
    from sqlalchemy.exc import OperationalError

    import app.auth.bootstrap as bootstrap_module

    class BoomSession:
        def execute(self, *args, **kwargs):
            raise OperationalError("SELECT 1", {}, Exception("boom"))

        def rollback(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(bootstrap_module, "SessionLocal", lambda: BoomSession())
    monkeypatch.setenv("BOOTSTRAP_PASSWORD", "some-password-123")

    exit_code = main(["--email", "admin-%s@example.com" % uuid.uuid4().hex])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Could not connect to the database" in captured.out
    assert "docker compose run --rm api" in captured.out
    assert "Bootstrap failed" not in captured.out


# ---------------------------------------------------------------------------
# Reading the password without echoing it
# ---------------------------------------------------------------------------

def test_the_password_is_never_echoed_without_a_terminal(monkeypatch):
    """getpass falls back to reading stdin with the password ECHOED.

    That happens inside a container unless a TTY was allocated, so the
    command must refuse rather than leak the secret.
    """
    monkeypatch.delenv("BOOTSTRAP_PASSWORD", raising=False)
    monkeypatch.setattr("app.auth.bootstrap.sys.stdin.isatty", lambda: False)
    monkeypatch.setattr("app.auth.bootstrap.os.open", _raise_oserror)

    with pytest.raises(PasswordUnavailableError) as exc:
        _read_password()
    assert "BOOTSTRAP_PASSWORD" in str(exc.value)


def test_the_environment_variable_is_preferred(monkeypatch):
    monkeypatch.setenv("BOOTSTRAP_PASSWORD", "from-the-environment")
    assert _read_password() == "from-the-environment"


def test_prompting_is_allowed_when_a_terminal_exists(monkeypatch):
    monkeypatch.delenv("BOOTSTRAP_PASSWORD", raising=False)
    monkeypatch.setattr("app.auth.bootstrap.sys.stdin.isatty", lambda: True)
    assert _can_prompt_securely() is True


def _raise_oserror(*args, **kwargs):
    raise OSError("no /dev/tty")
