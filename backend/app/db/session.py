"""Database engine and session management.

PostgreSQL is the system of record. This module owns the connection pool
and the session factory; request handlers and Celery tasks obtain a
session through get_db() (or SessionLocal directly in the worker).
"""

from typing import Generator

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings

engine = create_engine(
    settings.database_url,
    # pool_pre_ping issues a cheap "SELECT 1" before handing out a pooled
    # connection. Without it, a connection that the database closed while
    # idle (restart, network blip) surfaces as a confusing error on the
    # next request instead of being transparently replaced.
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=10,
)

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    # Do not expire ORM attributes after commit; the API layer serialises
    # objects right after committing and lazy-loading would need a new
    # session otherwise.
    expire_on_commit=False,
)


def psycopg_dsn(database_url: str | None = None) -> str:
    """Convert a SQLAlchemy URL into a libpq/psycopg connection string.

    settings.database_url is a SQLAlchemy URL such as
    "postgresql+psycopg://user:pass@host:5432/db". The "+psycopg" part is
    a SQLAlchemy dialect selector and is meaningless to psycopg, which
    only understands the plain "postgresql://" scheme. Passing the URL
    straight through raises ProgrammingError, which is how a readiness
    check can report "database down" while the application is talking to
    that very database successfully.
    """
    url = make_url(database_url if database_url is not None else settings.database_url)
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency that yields a session and always closes it."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
