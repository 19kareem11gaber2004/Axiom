"""Recover stuck documents and keep the queue moving.

The sweeper is the state-based recovery path for the job contract in
``docs/phase-1/05-job-contract.md``. PostgreSQL is authoritative; Redis
only transports tasks. Every rule therefore reads and writes the database
first and publishes to Celery afterwards. A crash between the two is safe:
the next sweep finds the same stale row and publishes again (the claim
guard makes duplicate delivery harmless).

Four stale states are recovered, oldest first and capped per category:

``uploaded``
    The upload committed but the process died before publishing.
``queued``
    The broker lost the task, or the original publish failed.
``retrying``
    A retry task was lost before it was consumed.
``processing``
    The worker died or exceeded its hard limit.

The ``updated_at`` timestamp is the retry marker for non-terminal states:
it is touched only after a successful publish. If publishing fails, the
row keeps its old timestamp and the next sweep tries again. Terminal
transitions (``failed``) do update the timestamp because no publish is
expected.

Only one sweep may run at a time. A session-level PostgreSQL advisory lock
is held on a dedicated connection; the sweep itself uses a separate
database session so the lock cannot be released by a transaction commit.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Iterable

from sqlalchemy import case, cast, func, select, text, update
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import Document, ProcessingRun
from app.db.session import SessionLocal, close_session, engine
from app.processing import ProcessingTimeout
from app.worker.pipeline import (
    ATTEMPTS_EXHAUSTED,
    ATTEMPTS_EXHAUSTED_MESSAGE,
)
from app.worker.publisher import TaskPublisher, get_task_publisher

logger = logging.getLogger("axiom.worker.sweeper")

# A stable application-wide key for the sweeper's session advisory lock.
# Any 64-bit value works; keeping it explicit makes it easy to find when
# diagnosing a lock from psql (pg_locks.objid).
SWEEPER_ADVISORY_LOCK_KEY = 0x41584F4D

__all__ = [
    "SWEEPER_ADVISORY_LOCK_KEY",
    "SweepSummary",
    "run_sweeper",
    "sweep",
]


@dataclass(frozen=True)
class SweepSummary:
    """The outcome of one sweep, used for logs and tests."""

    scanned: int = 0
    recovered: int = 0
    failed: int = 0
    publish_failures: int = 0
    more_remaining: bool = False
    duration_ms: int = 0


def _stale_cutoff(seconds: int):
    """Return a PostgreSQL-side timestamp ``now() - seconds``.

    Keeping the subtraction in SQL avoids clock skew between the worker
    host and PostgreSQL; the database decides what is stale.
    """
    return func.now() - func.make_interval(0, 0, 0, 0, 0, 0, seconds)


def _select_candidates(
    session: Session,
    status: str,
    stale_seconds: int,
    batch_size: int,
) -> tuple[list, bool]:
    """Return the oldest stale rows and whether more remain.

    One extra row is fetched purely to answer ``more_remaining`` without
    a second count query. Only ``batch_size`` rows are returned for work.
    """
    rows = session.execute(
        select(Document.id, Document.processing_attempts)
        .where(
            Document.status == status,
            Document.updated_at < _stale_cutoff(stale_seconds),
        )
        .order_by(Document.updated_at.asc(), Document.id.asc())
        .limit(batch_size + 1)
    ).all()
    return rows[:batch_size], len(rows) > batch_size


def _mark_uploaded_queued(
    session: Session,
    document_ids: Iterable,
) -> list:
    """Conditionally move uploaded rows to queued and return winners."""
    ids = list(document_ids)
    if not ids:
        return []
    transitioned = set(
        session.execute(
            update(Document)
            .where(
                Document.id.in_(ids),
                Document.status == "uploaded",
            )
            .values(
                status="queued",
                error_code=None,
                error_message=None,
                retryable=False,
            )
            .returning(Document.id)
            .execution_options(synchronize_session=False)
        ).scalars()
    )
    # RETURNING does not guarantee the order of an IN list. Rebuild the
    # result from the selection order so publications remain oldest first.
    return [document_id for document_id in ids if document_id in transitioned]


def _fail_exhausted(
    session: Session,
    document_ids: Iterable,
    status: str,
    max_attempts: int,
) -> list:
    """Conditionally fail stale rows that have no claim left.

    The status and attempt guards are repeated here even though they were
    checked while selecting candidates: another actor may have claimed or
    changed the row between selection and this update.
    """
    ids = list(document_ids)
    if not ids:
        return []
    return list(
        session.execute(
            update(Document)
            .where(
                Document.id.in_(ids),
                Document.status == status,
                Document.processing_attempts >= max_attempts,
            )
            .values(
                status="failed",
                error_code=ATTEMPTS_EXHAUSTED,
                error_message=ATTEMPTS_EXHAUSTED_MESSAGE,
                retryable=False,
                updated_at=func.now(),
            )
            .returning(Document.id)
            .execution_options(synchronize_session=False)
        ).scalars()
    )


def _timeout_processing(
    session: Session,
    document_ids: Iterable,
    max_attempts: int,
) -> list[tuple]:
    """Atomically move stale processing rows and return their new status."""
    ids = list(document_ids)
    if not ids:
        return []

    remaining_attempt = Document.processing_attempts < max_attempts
    rows = session.execute(
        update(Document)
        .where(
            Document.id.in_(ids),
            Document.status == "processing",
        )
        .values(
            # The CASE expression has its own VARCHAR type. Cast it to the
            # native enum type, otherwise PostgreSQL rejects the assignment:
            # "column status is of type document_status but expression is
            # of type character varying".
            status=cast(
                case(
                    (remaining_attempt, "retrying"),
                    else_="failed",
                ),
                Document.status.type,
            ),
            error_code=case(
                (remaining_attempt, "PROCESSING_TIMEOUT"),
                else_=ATTEMPTS_EXHAUSTED,
            ),
            error_message=case(
                (remaining_attempt, ProcessingTimeout.safe_message),
                else_=ATTEMPTS_EXHAUSTED_MESSAGE,
            ),
            retryable=case(
                (remaining_attempt, True),
                else_=False,
            ),
            # A retrying row is only touched again after a successful
            # publish. A failed row is terminal, so it is stamped now.
            updated_at=case(
                (remaining_attempt, Document.updated_at),
                else_=func.now(),
            ),
        )
        .returning(Document.id, Document.status)
        .execution_options(synchronize_session=False)
    ).all()

    transitioned_by_id = {row.id: row.status for row in rows}
    # Rebuild from the selection order: UPDATE ... RETURNING is not
    # required to preserve the order of the IN list.
    transitioned = [
        (document_id, transitioned_by_id[document_id])
        for document_id in ids
        if document_id in transitioned_by_id
    ]
    if transitioned:
        session.execute(
            update(ProcessingRun)
            .where(
                ProcessingRun.document_id.in_(
                    [document_id for document_id, _ in transitioned]
                ),
                ProcessingRun.finished_at.is_(None),
            )
            .values(
                stage="timeout",
                finished_at=func.now(),
                error_code="PROCESSING_TIMEOUT",
                error_message=ProcessingTimeout.safe_message,
            )
            .execution_options(synchronize_session=False)
        )
    return transitioned


def _publish(
    publisher: TaskPublisher,
    document_id,
    status: str,
) -> bool:
    """Publish one task; log and return False instead of aborting a batch."""
    try:
        published = publisher.enqueue_document(document_id, countdown=0)
    except Exception as exc:
        logger.warning(
            "sweeper.publish_failed document_id=%s status=%s "
            "error_type=%s",
            document_id,
            status,
            type(exc).__name__,
        )
        return False
    if not published:
        logger.warning(
            "sweeper.publish_failed document_id=%s status=%s "
            "published=false",
            document_id,
            status,
        )
        return False
    return True


def _touch_published(session: Session, document_ids: Iterable, status: str):
    """Guarded timestamp update for rows whose task was published.

    A row that was claimed or otherwise changed since selection no longer
    matches the status guard, so this update leaves it alone. The claim
    already stamped ``updated_at`` in that case.
    """
    ids = list(document_ids)
    if not ids:
        return
    session.execute(
        update(Document)
        .where(
            Document.id.in_(ids),
            Document.status == status,
        )
        .values(updated_at=func.now())
        .execution_options(synchronize_session=False)
    )


def _log_summary(summary: SweepSummary) -> None:
    logger.info(
        "sweeper.summary scanned=%d recovered=%d failed=%d "
        "publish_failures=%d more_remaining=%s duration_ms=%d",
        summary.scanned,
        summary.recovered,
        summary.failed,
        summary.publish_failures,
        summary.more_remaining,
        summary.duration_ms,
    )


def sweep(
    session: Session,
    publisher: TaskPublisher,
    *,
    batch_size: int | None = None,
    max_attempts: int | None = None,
) -> SweepSummary:
    """Recover every stale category once; the caller owns ``session``.

    State changes are committed before the first publish. Publishes are
    then attempted independently; only successful ones lead to a guarded
    ``updated_at`` touch. A publish failure is logged and counted, never
    raised, so one broker error cannot abort the rest of the batch.
    """
    started = time.monotonic()
    batch_size = settings.sweeper_batch_size if batch_size is None else batch_size
    max_attempts = (
        settings.max_processing_attempts
        if max_attempts is None
        else max_attempts
    )

    try:
        uploaded_rows, uploaded_more = _select_candidates(
            session,
            "uploaded",
            settings.sweeper_uploaded_stale_seconds,
            batch_size,
        )
        queued_rows, queued_more = _select_candidates(
            session,
            "queued",
            settings.sweeper_queued_stale_seconds,
            batch_size,
        )
        retrying_rows, retrying_more = _select_candidates(
            session,
            "retrying",
            settings.sweeper_retrying_stale_seconds,
            batch_size,
        )
        processing_rows, processing_more = _select_candidates(
            session,
            "processing",
            settings.sweeper_processing_stale_seconds,
            batch_size,
        )

        more_remaining = any(
            (
                uploaded_more,
                queued_more,
                retrying_more,
                processing_more,
            )
        )
        scanned = (
            len(uploaded_rows)
            + len(queued_rows)
            + len(retrying_rows)
            + len(processing_rows)
        )

        queued_uploaded = _mark_uploaded_queued(
            session,
            [row.id for row in uploaded_rows],
        )

        queued_at_cap = [
            row.id
            for row in queued_rows
            if row.processing_attempts >= max_attempts
        ]
        queued_publish = [
            row.id
            for row in queued_rows
            if row.processing_attempts < max_attempts
        ]
        retrying_at_cap = [
            row.id
            for row in retrying_rows
            if row.processing_attempts >= max_attempts
        ]
        retrying_publish = [
            row.id
            for row in retrying_rows
            if row.processing_attempts < max_attempts
        ]

        failed_ids = _fail_exhausted(
            session,
            queued_at_cap,
            "queued",
            max_attempts,
        )
        failed_ids += _fail_exhausted(
            session,
            retrying_at_cap,
            "retrying",
            max_attempts,
        )

        timeout_rows = _timeout_processing(
            session,
            [row.id for row in processing_rows],
            max_attempts,
        )
        processing_publish = [
            document_id
            for document_id, status in timeout_rows
            if status == "retrying"
        ]
        failed_ids += [
            document_id
            for document_id, status in timeout_rows
            if status == "failed"
        ]

        # The state transitions are durable before any task can observe
        # them. A crash here leaves rows for the next sweep, not a
        # published task that races an uncommitted transition.
        session.commit()
    except Exception:
        session.rollback()
        raise

    recovered = 0
    publish_failures = 0
    published_queued: list = []
    published_retrying: list = []

    publish_groups = (
        (queued_uploaded, "queued", published_queued),
        (queued_publish, "queued", published_queued),
        (retrying_publish, "retrying", published_retrying),
        (processing_publish, "retrying", published_retrying),
    )
    for document_ids, status, successes in publish_groups:
        for document_id in document_ids:
            if _publish(publisher, document_id, status):
                recovered += 1
                successes.append(document_id)
            else:
                publish_failures += 1

    try:
        _touch_published(session, published_queued, "queued")
        _touch_published(session, published_retrying, "retrying")
        session.commit()
    except Exception:
        session.rollback()
        raise

    summary = SweepSummary(
        scanned=scanned,
        recovered=recovered,
        failed=len(failed_ids),
        publish_failures=publish_failures,
        more_remaining=more_remaining,
        duration_ms=int((time.monotonic() - started) * 1000),
    )
    _log_summary(summary)
    return summary


def _try_acquire_lock(connection: Connection) -> bool:
    """Try the session-level advisory lock on a dedicated connection."""
    return bool(
        connection.execute(
            text("SELECT pg_try_advisory_lock(:key)"),
            {"key": SWEEPER_ADVISORY_LOCK_KEY},
        ).scalar_one()
    )


def _release_lock(connection: Connection) -> None:
    """Release the lock; discard the connection if that cannot be done.

    Invalidating is essential: a pooled connection that still holds a
    session-level advisory lock would block every future sweep even after
    this task returns.
    """
    try:
        released = bool(
            connection.execute(
                text("SELECT pg_advisory_unlock(:key)"),
                {"key": SWEEPER_ADVISORY_LOCK_KEY},
            ).scalar_one()
        )
        if not released:
            raise RuntimeError("sweeper advisory lock was not held")
        connection.commit()
    except Exception:
        logger.warning(
            "sweeper.unlock_failed key=%d",
            SWEEPER_ADVISORY_LOCK_KEY,
            exc_info=True,
        )
        connection.invalidate()


def run_sweeper(publisher: TaskPublisher | None = None) -> SweepSummary:
    """Acquire the sweeper lock and run one sweep with its own session."""
    started = time.monotonic()
    connection = engine.connect()
    acquired = False
    try:
        acquired = _try_acquire_lock(connection)
        if not acquired:
            logger.info(
                "sweeper.lock_busy key=%d",
                SWEEPER_ADVISORY_LOCK_KEY,
            )
            return SweepSummary(
                duration_ms=int((time.monotonic() - started) * 1000)
            )

        # The lock is session-level, so ending this short transaction does
        # not release it. The sweep session below is a different
        # connection and must not be able to release the lock by commit.
        connection.commit()

        session = SessionLocal()
        try:
            return sweep(
                session,
                publisher if publisher is not None else get_task_publisher(),
            )
        finally:
            close_session(session)
    finally:
        if acquired:
            _release_lock(connection)
        connection.close()
