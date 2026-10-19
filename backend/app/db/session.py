"""Database engine and session management.

PostgreSQL is the system of record. This module owns the connection pool
and the session factory; request handlers and Celery tasks obtain a
session through get_db() (or SessionLocal directly in the worker).
"""

from typing import Generator

from sqlalchemy import create_engine
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


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency that yields a session and always closes it."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
