"""Celery tasks for the document-processing lifecycle.

The task wrapper is deliberately thin. It validates the payload, owns one
database session, and composes the pieces from :mod:`app.worker.pipeline`:

    claim -> process outside a transaction -> commit or classify failure

The processor and storage are resolved through module-level factories so
tests can substitute a fake processor without a broker or filesystem.

A soft time limit is converted into the normal ``ProcessingTimeout``
taxonomy. A database failure is never converted into a processing failure:
it is rolled back by the pipeline and re-raised here so Celery records a
task failure and the document remains for the sweeper.
"""

from __future__ import annotations

import logging
import random
import time
import uuid

from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy.exc import SQLAlchemyError

from app.config import settings
from app.db.session import SessionLocal, close_session
from app.processing import (
    DocumentProcessor,
    DocumentSnapshot,
    IntegrityProcessor,
    InternalError,
    ProcessingError,
    ProcessingTimeout,
    ProcessResult,
)
from app.storage import get_storage
from app.worker.celery_app import celery_app
from app.worker.pipeline import (
    ClaimedDocument,
    FailureStatus,
    claim_document,
    commit_success,
    handle_failure,
)
from app.worker.publisher import get_task_publisher

logger = logging.getLogger("axiom.worker.tasks")

PROCESS_DOCUMENT_TASK_NAME = "documents.process"
SWEEP_DOCUMENTS_TASK_NAME = "documents.sweep"

# SystemRandom has no state, so prefork children cannot inherit an
# identical jitter sequence. Tests replace it with a fixed implementation.
_RNG = random.SystemRandom()

__all__ = [
    "PROCESS_DOCUMENT_TASK_NAME",
    "SWEEP_DOCUMENTS_TASK_NAME",
    "process_document",
]


def get_processor() -> DocumentProcessor:
    """Return the processor implementation for this deployment.

    Phase 5 replaces the integrity-only processor here without changing
    the claim, commit, retry, or task code.
    """
    return IntegrityProcessor()


def _parse_document_id(payload: object) -> uuid.UUID | None:
    """Return the payload as a UUID, or ``None`` when it is malformed.

    ``str`` first is intentional: the publisher sends a string, but an
    in-process caller may already hold a UUID. Both are valid; arbitrary
    objects and malformed strings are rejected before any DB access.
    """
    try:
        return uuid.UUID(str(payload))
    except (AttributeError, TypeError, ValueError):
        return None


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _snapshot(claim: ClaimedDocument) -> DocumentSnapshot:
    """Copy the immutable claim facts into the processor contract."""
    return DocumentSnapshot(
        id=claim.id,
        workspace_id=claim.workspace_id,
        storage_key=claim.storage_key,
        checksum=claim.checksum,
        size_bytes=claim.size_bytes,
        mime_type=claim.mime_type,
    )


def _run_processor(claim: ClaimedDocument) -> ProcessResult:
    """Read and process the document; the claim transaction is closed.

    ``claim_document`` commits before returning, and this function does no
    database work. A slow storage read therefore holds no document row
    lock.
    """
    return get_processor().process(_snapshot(claim), get_storage())


@celery_app.task(
    bind=True,
    ignore_result=True,
    name=PROCESS_DOCUMENT_TASK_NAME,
)
def process_document(self, document_id: str) -> str:
    """Claim, process, and commit one document end to end.

    Outcomes are ``invalid``, ``noop``, ``indexed``, ``stale``,
    ``retrying``, or ``failed``. A retry is published as a fresh task with
    a countdown after the failure state has committed; ``self.retry`` is
    never used, so Celery's result backend is never the source of truth.
    """
    started = time.monotonic()
    task_id = getattr(self.request, "id", None)

    parsed_id = _parse_document_id(document_id)
    if parsed_id is None:
        logger.warning(
            "document.invalid_payload attempt=0 outcome=invalid "
            "duration_ms=%d task_id=%s",
            _elapsed_ms(started),
            task_id,
        )
        return "invalid"

    session = SessionLocal()
    claim: ClaimedDocument | None = None
    try:
        claim = claim_document(
            session,
            parsed_id,
            max_attempts=settings.max_processing_attempts,
        )
        if claim is None:
            logger.debug(
                "document.noop document_id=%s attempt=0 outcome=noop "
                "duration_ms=%d task_id=%s",
                parsed_id,
                _elapsed_ms(started),
                task_id,
            )
            return "noop"

        # Processor and guarded commit live in one try so a commit-time
        # ProcessingError (for example ChunkConflict) follows the same
        # classification path as a processor failure. SQLAlchemy errors
        # are re-raised before any failure write is attempted.
        try:
            result = _run_processor(claim)
            committed = commit_success(session, claim, result)
        except SoftTimeLimitExceeded:
            # A soft limit can interrupt any line, including the commit.
            # Discard whatever transaction state it left before the
            # failure transition opens its own.
            session.rollback()
            error = ProcessingTimeout()
        except ProcessingError as exc:
            error = exc
        except SQLAlchemyError:
            raise
        except Exception as exc:
            logger.warning(
                "document.unexpected_error document_id=%s attempt=%d "
                "error_type=%s task_id=%s",
                claim.id,
                claim.attempt_no,
                type(exc).__name__,
                task_id,
            )
            error = InternalError()
        else:
            if committed:
                logger.info(
                    "document.indexed document_id=%s attempt=%d "
                    "outcome=indexed duration_ms=%d task_id=%s",
                    claim.id,
                    claim.attempt_no,
                    _elapsed_ms(started),
                    task_id,
                )
                return "indexed"

            logger.warning(
                "document.stale document_id=%s attempt=%d outcome=stale "
                "duration_ms=%d task_id=%s",
                claim.id,
                claim.attempt_no,
                _elapsed_ms(started),
                task_id,
            )
            return "stale"

        failure = handle_failure(
            session,
            claim,
            error,
            settings.max_processing_attempts,
            base_seconds=settings.processing_retry_base_seconds,
            maximum_seconds=settings.processing_retry_max_seconds,
            jitter_seconds=settings.processing_retry_jitter_seconds,
            rng=_RNG,
        )

        if failure.status is FailureStatus.RETRYING:
            countdown = (
                failure.countdown if failure.countdown is not None else 0
            )
            published = get_task_publisher().enqueue_document(
                claim.id,
                countdown=countdown,
            )
            logger.info(
                "document.retrying document_id=%s attempt=%d "
                "outcome=retrying countdown=%s published=%s "
                "duration_ms=%d task_id=%s",
                claim.id,
                claim.attempt_no,
                countdown,
                published,
                _elapsed_ms(started),
                task_id,
            )
            return "retrying"

        if failure.status is FailureStatus.STALE:
            logger.warning(
                "document.stale document_id=%s attempt=%d outcome=stale "
                "duration_ms=%d task_id=%s",
                claim.id,
                claim.attempt_no,
                _elapsed_ms(started),
                task_id,
            )
            return "stale"

        logger.warning(
            "document.failed document_id=%s attempt=%d outcome=failed "
            "duration_ms=%d task_id=%s",
            claim.id,
            claim.attempt_no,
            _elapsed_ms(started),
            task_id,
        )
        return "failed"

    except SQLAlchemyError as exc:
        # The pipeline rolled its transaction back. Do not attempt any
        # failure write here: a database error must reach Celery, not be
        # hidden as a document failure.
        logger.error(
            "document.db_error document_id=%s attempt=%s outcome=db_error "
            "error_type=%s duration_ms=%d task_id=%s",
            parsed_id,
            claim.attempt_no if claim is not None else 0,
            type(exc).__name__,
            _elapsed_ms(started),
            task_id,
        )
        raise
    finally:
        close_session(session)
