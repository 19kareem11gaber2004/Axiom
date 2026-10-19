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
from sqlalchemy.exc import ProgrammingError

from app.auth.service import create_workspace, hash_password
from app.db.models import User
from app.db.session import SessionLocal

# EmailStr is an annotation rather than a class, so it is validated
# through a TypeAdapter.
_EMAIL_ADAPTER = TypeAdapter(EmailStr)


def _read_password() -> str:
    password = os.environ.get("BOOTSTRAP_PASSWORD")
    if password:
        return password
    return getpass.getpass("Password for the bootstrap administrator: ")


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

    password = args.password or _read_password()
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
            print(
                "The database is not migrated. Run 'alembic upgrade head' first."
            )
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
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        session.rollback()
        print("Bootstrap failed: %s" % exc)
        return 1
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
