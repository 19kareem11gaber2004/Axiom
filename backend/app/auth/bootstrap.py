"""Bootstrap the first administrator.

Axiom has no public registration, so the very first user cannot be
created through the API: adding members requires an admin, and there is
no admin yet. This command closes that gap.

Run it once, by hand, on a fresh database:

    python -m app.auth.bootstrap --email admin@example.com --workspace "Axiom"

If the password is not supplied on the command line it is prompted for
(interactively) or read from the BOOTSTRAP_PASSWORD environment
variable. It is never taken from a command-line argument by default, so
it does not end up in shell history or in the process list.

The command refuses to run when any user already exists, so it can never
be used to mint a second administrator behind an operator's back.
"""

import argparse
import getpass
import os
import sys

from pydantic import EmailStr, TypeAdapter, ValidationError
from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError, ProgrammingError

from app.auth.service import create_workspace, hash_password
from app.config import settings
from app.db.models import User
from app.db.session import SessionLocal

# EmailStr is an annotation rather than a class, so it is validated
# through a TypeAdapter.
_EMAIL_ADAPTER = TypeAdapter(EmailStr)


class PasswordUnavailableError(Exception):
    """The password cannot be read without echoing it."""


class BootstrapConnectionError(Exception):
    """The database could not be reached."""


def _can_prompt_securely() -> bool:
    """True when a password can be read without being echoed.

    getpass falls back to reading stdin with the password ECHOED when
    there is no controlling terminal. That happens inside a container
    unless a TTY was allocated, so the prompt is refused there rather
    than leaking the secret into the terminal or the log.
    """
    if sys.stdin.isatty():
        return True
    try:
        handle = os.open("/dev/tty", os.O_RDWR)
    except OSError:
        return False
    os.close(handle)
    return True


def _read_password() -> str:
    password = os.environ.get("BOOTSTRAP_PASSWORD")
    if password:
        return password
    if not _can_prompt_securely():
        raise PasswordUnavailableError(
            "No terminal is available to read the password without echoing "
            "it.\n"
            "Set BOOTSTRAP_PASSWORD in the environment instead, for example:\n"
            "  $env:BOOTSTRAP_PASSWORD = Read-Host 'Admin password' -AsSecureString"
        )
    return getpass.getpass("Password for the bootstrap administrator: ")


def _database_target() -> str:
    """Human readable database target, without the password."""
    try:
        url = make_url(settings.database_url)
    except Exception:  # noqa: BLE001 - display only
        return "<unparseable DATABASE_URL>"
    return "%s:%s/%s" % (url.host, url.port or 5432, url.database)


def _connection_help(exc: Exception) -> str:
    """Explain a failed connection, including which server was tried.

    The raw driver message usually blames the password, which is a
    red herring when the real problem is that the command is talking to
    a different PostgreSQL than the one the API uses.
    """
    reason = str(exc).strip().splitlines()[0] if str(exc).strip() else exc.__class__.__name__
    return (
        "Could not connect to the database.\n"
        "  target : %s\n"
        "  reason : %s\n"
        "\n"
        "The API and the worker use the Compose service name, not "
        "'localhost'.\n"
        "If the database runs in Docker Compose, run this command inside "
        "the Compose network:\n"
        "  docker compose run --rm api python -m app.auth.bootstrap "
        "--email <email> --workspace <name>"
        % (_database_target(), reason)
    )


def _validate_email(email: str) -> str:
    """Check the address with the same rule the login endpoint uses.

    Without this the command could create an account whose email the API
    then refuses at login time (special-use domains such as .test or
    .localhost are rejected), leaving an unusable administrator behind.
    """
    try:
        return str(_EMAIL_ADAPTER.validate_python(email))
    except ValidationError as exc:
        raise ValueError(
            "Not a usable email address: %r\n"
            "Use an ordinary address such as admin@example.com. Addresses on "
            "reserved domains (.test, .localhost, .invalid) are rejected by "
            "the API at login time." % email
        ) from exc


def _parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m app.auth.bootstrap",
        description="Create the first administrator and its workspace.",
    )
    parser.add_argument("--email", required=True, help="administrator email address")
    parser.add_argument(
        "--workspace",
        default="Axiom",
        help="name of the workspace the administrator will own",
    )
    parser.add_argument(
        "--password",
        default=None,
        help="password (discouraged: it lands in shell history)",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(argv)

    try:
        password = args.password or _read_password()
    except PasswordUnavailableError as exc:
        print(exc)
        return 2

    if len(password.encode("utf-8")) > 72:
        print("Password is too long (bcrypt accepts at most 72 bytes).")
        return 2
    if len(password) < 8:
        print("Password must be at least 8 characters.")
        return 2

    # Validated before the database is touched, so a bad address is
    # reported plainly instead of as a bootstrap failure.
    try:
        email = _validate_email(args.email.strip().lower())
    except ValueError as exc:
        print(exc)
        return 2

    session = SessionLocal()
    try:
        try:
            existing = session.execute(
                select(func.count()).select_from(User)
            ).scalar_one()
        except ProgrammingError:
            # PostgreSQL reports a missing table as a ProgrammingError
            # (SQLSTATE 42P01).
            print("The database is not migrated. Apply the migration first:")
            print("  docker compose run --rm api alembic upgrade head")
            return 1
        except OperationalError as exc:
            print(_connection_help(exc))
            return 1

        if existing:
            print(
                "Refusing to bootstrap: the database already contains %d user(s). "
                "Add further administrators through the members API." % existing
            )
            return 1

        user = User(email=email, password_hash=hash_password(password))
        session.add(user)
        session.commit()
        session.refresh(user)
        workspace = create_workspace(session, args.workspace, user.id)
        print("Created administrator %s" % email)
        print("Created workspace %r (id=%s)" % (workspace.name, workspace.id))
        print("Sign in with POST /api/v1/auth/login")
        return 0
    except OperationalError as exc:
        session.rollback()
        print(_connection_help(exc))
        return 1
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        session.rollback()
        print("Bootstrap failed: %s" % exc)
        return 1
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
