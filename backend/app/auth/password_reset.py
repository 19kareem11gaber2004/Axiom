"""Set the password of an existing account.

Axiom has no public registration and no "forgot password" flow, so an
operator who loses the administrator password has no way back in through
the API. This command is that way back.

    python -m app.auth.password_reset --email admin@example.com

It deliberately does much less than a user-management screen would:

* It only ever **updates an existing account**. It never creates a user
  and never grants a role, so it cannot be used to mint an
  administrator behind an operator's back. Use
  ``python -m app.auth.bootstrap`` for the very first administrator.
* It preserves the account. The user row keeps its id, its email, its
  created_at, and every workspace membership it holds. Only the stored
  password hash is replaced.
* It never logs or prints the password. The value is read from a
  terminal prompt that does not echo, or from the AXIOM_ADMIN_PASSWORD
  environment variable for non-interactive use. It is never accepted on
  the command line by default, so it does not end up in shell history or
  in the process list.
* It refuses a password that is shorter than 8 characters or longer than
  the 72 bytes bcrypt accepts.

Because it changes a credential, it is an explicit operator action: run
it deliberately, for one named account.
"""

import argparse
import getpass
import os
import sys

from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError, ProgrammingError

from app.auth.service import hash_password
from app.config import settings
from app.db.models import User
from app.db.session import SessionLocal

MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_BYTES = 72


class PasswordUnavailableError(Exception):
    """The password cannot be read without echoing it."""


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
    password = os.environ.get("AXIOM_ADMIN_PASSWORD")
    if password:
        return password
    if not _can_prompt_securely():
        raise PasswordUnavailableError(
            "No terminal is available to read the password without echoing "
            "it.\n"
            "Set AXIOM_ADMIN_PASSWORD in the environment instead, for "
            "example:\n"
            "  $env:AXIOM_ADMIN_PASSWORD = Read-Host 'New password' "
            "-AsSecureString"
        )
    try:
        return getpass.getpass("New password for the account: ")
    except (EOFError, KeyboardInterrupt):
        # The prompt was closed before anything was typed.
        raise PasswordUnavailableError(
            "No password was entered. Nothing was changed."
        ) from None


def _parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m app.auth.password_reset",
        description="Set a new password for an existing Axiom account.",
    )
    parser.add_argument("--email", required=True, help="account email address")
    parser.add_argument(
        "--password",
        default=None,
        help="password (discouraged: it lands in shell history)",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(argv)

    email = (args.email or "").strip().lower()
    if not email:
        print("An email address is required.")
        return 2

    try:
        password = args.password or _read_password()
    except PasswordUnavailableError as exc:
        print(exc)
        return 2

    if len(password) < MIN_PASSWORD_LENGTH:
        print("Password must be at least %d characters." % MIN_PASSWORD_LENGTH)
        return 2
    if len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
        print("Password is too long (bcrypt accepts at most %d bytes)." % MAX_PASSWORD_BYTES)
        return 2

    session = SessionLocal()
    try:
        try:
            user = session.execute(
                select(User).where(func.lower(User.email) == email)
            ).scalar_one_or_none()
        except ProgrammingError:
            print("The database is not migrated. Apply the migration first:")
            print("  docker compose run --rm api alembic upgrade head")
            return 1
        except OperationalError as exc:
            print("Could not connect to the database: %s" % exc)
            return 1

        if user is None:
            # This command must never create an account. Use the
            # bootstrap command for the first administrator and the
            # members API for everybody else.
            print(
                "No account exists for %s. This command only changes the "
                "password of an existing account; it never creates one."
                % email
            )
            return 1

        # Only the hash changes. The id, the email, the memberships and
        # everything else about the account are left untouched.
        user.password_hash = hash_password(password)
        session.commit()

        print("Updated the password for %s" % email)
        print("The account, its id and its workspace memberships are unchanged.")
        return 0
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        session.rollback()
        print("Password reset failed: %s" % exc)
        return 1
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
