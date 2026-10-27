"""Claim, guarded commit, and failure transitions for document processing.

The contract is defined in ``docs/phase-1/05-job-contract.md``. A task
payload carries only ``document_id`` (D-017), so the claim first resolves
the document's ``workspace_id`` and then repeats it in the UPDATE's WHERE
clause. That keeps the state transition scoped exactly like the contract
and gives the new ``processing_runs`` row its workspace snapshot.

Exactly one worker can win a claim:

* the UPDATE is conditional on ``status IN ('queued', 'retrying')`` and
  on the attempt budget;
* PostgreSQL locks the row for the duration of the transaction, so a
  concurrent UPDATE waits and then re-evaluates the predicate against the
  committed ``processing`` row, matching zero rows.

Losers return ``None`` with no side effects. The function owns its
transaction and commits before returning; file I/O happens later, outside
this transaction, so a slow storage backend never holds a document row
lock.

The same guarded-update idea protects the commit and failure paths: a
worker that lost its claim (sweeper recovery followed by a new claim)
matches zero rows and must roll back rather than overwrite the current
owner's state.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.db.models import Chunk, Document, ProcessingRun
from app.processing.base import ProcessResult
from app.processing.errors import ChunkConflict, ProcessingError

logger = logging.getLogger("axiom.worker.pipeline")

__all__ = [
    "ATTEMPTS_EXHAUSTED",
    "ATTEMPTS_EXHAUSTED_MESSAGE",
    "ClaimedDocument",
    "FailureOutcome",
    "FailureStatus",
    "RandomSource",
    "claim_document",
    "commit_success",
    "compute_backoff",
    "handle_failure",
]

ATTEMPTS_EXHAUSTED = "ATTEMPTS_EXHAUSTED"
ATTEMPTS_EXHAUSTED_MESSAGE = (
    "Document processing failed after the maximum number of attempts."
)


class RandomSource(Protocol):
    """The one random operation the backoff calculation needs.

    Injecting this small interface instead of calling ``random`` directly
    makes the retry table deterministic in tests while still accepting a
    ``random.Random`` instance in production.
    """

    def uniform(self, low: float, high: float) -> float:
        """Return a float in the inclusive range ``[low, high]``."""


class FailureStatus(str, Enum):
    """The three possible results of a failed processing attempt."""

    RETRYING = "retrying"
    FAILED = "failed"
    STALE = "stale"


@dataclass(frozen=True)
class FailureOutcome:
    """What the task layer should do after a failure transition.

    ``countdown`` is set only for ``RETRYING``. It is the delay before the
    fresh task is published; a failed or stale attempt has no next task.
    """

    status: FailureStatus
    countdown: float | None = None


def compute_backoff(
    attempt: int,
    base: float,
    maximum: float,
    jitter: float,
    rng: RandomSource,
) -> float:
    """Return the bounded exponential backoff for one claim attempt.

    ``attempt`` is one-based, matching ``processing_attempts``. The jitter
    is added before the maximum is applied, so the result never exceeds
    ``maximum`` even with a large random draw.
    """
    raw = base * (2 ** (attempt - 1))
    return min(raw + rng.uniform(0, jitter), maximum)


@dataclass(frozen=True)
class ClaimedDocument:
    """The immutable facts a claimed worker needs for the rest of the job.

    ``attempt_no`` is the value returned by the UPDATE's RETURNING clause,
    i.e. the attempt this worker just claimed. It is the number persisted
    on the corresponding ``processing_runs`` row.
    """

    id: uuid.UUID
    workspace_id: uuid.UUID
    attempt_no: int
    storage_key: str
    checksum: str
    size_bytes: int
    mime_type: str
    original_filename: str


def claim_document(
    session: Session,
    document_id: uuid.UUID,
    max_attempts: int,
) -> ClaimedDocument | None:
    """Atomically move one document to ``processing`` and open a run row.

    Returns ``None`` when the document does not exist, is not in a
    claimable state, or has already used its attempt budget. Every
    no-op path rolls back, so it leaves no transaction or lock behind.

    The caller must not have unrelated pending work in ``session``: this
    function commits or rolls back its own unit of work.
    """
    workspace_id = session.execute(
        select(Document.workspace_id).where(Document.id == document_id)
    ).scalar_one_or_none()

    if workspace_id is None:
        session.rollback()
        logger.debug(
            "Document %s was not claimed: no such document.", document_id
        )
        return None

    row = session.execute(
        update(Document)
        .where(
            Document.id == document_id,
            Document.workspace_id == workspace_id,
            Document.status.in_(("queued", "retrying")),
            Document.processing_attempts < max_attempts,
        )
        .values(
            status="processing",
            processing_attempts=Document.processing_attempts + 1,
            updated_at=func.now(),
            error_code=None,
            error_message=None,
            retryable=False,
        )
        .returning(
            Document.id,
            Document.workspace_id,
            Document.processing_attempts,
            Document.storage_key,
            Document.checksum,
            Document.size_bytes,
            Document.mime_type,
            Document.original_filename,
        )
        # The row was not loaded as an ORM entity; RETURNING already gives
        # this function every value it needs, so no identity-map sync is
        # required (or wanted).
        .execution_options(synchronize_session=False)
    ).one_or_none()

    if row is None:
        session.rollback()
        logger.debug(
            "Document %s was not claimed: not queued/retrying, or at the "
            "attempt cap.",
            document_id,
        )
        return None

    claimed = ClaimedDocument(
        id=row.id,
        workspace_id=row.workspace_id,
        attempt_no=row.processing_attempts,
        storage_key=row.storage_key,
        checksum=row.checksum,
        size_bytes=row.size_bytes,
        mime_type=row.mime_type,
        original_filename=row.original_filename,
    )

    try:
        session.add(
            ProcessingRun(
                document_id=claimed.id,
                workspace_id=claimed.workspace_id,
                attempt_no=claimed.attempt_no,
                stage="claimed",
                started_at=func.now(),
            )
        )
        session.commit()
    except Exception:
        # A failed run insert must leave the document claimable again.
        # commit() has already ended the transaction; rollback clears the
        # failed state so the caller can close or reuse the session.
        session.rollback()
        raise

    logger.info(
        "document.claim document_id=%s workspace_id=%s attempt=%d "
        "max_attempts=%d",
        claimed.id,
        claimed.workspace_id,
        claimed.attempt_no,
        max_attempts,
    )
    return claimed


def commit_success(
    session: Session,
    claim: ClaimedDocument,
    result: ProcessResult,
) -> bool:
    """Atomically replace the chunk set and mark the document indexed.

    The guarded UPDATE is the stale-worker barrier. It matches only while
    the document is still in a worker-owned state and
    ``processing_attempts`` still equals the attempt this worker claimed.
    A sweeper timeout followed by a new claim increments that counter, so
    the old worker's commit matches zero rows, rolls back its chunk
    replacement, and returns ``False``.

    The chunk INSERT is flushed inside the same transaction as the
    DELETE, the document UPDATE, and the run-row close. A duplicate
    ``(document_id, chunk_index)`` raises ``ChunkConflict`` and rolls all
    of it back, so the previous chunk set remains visible and no partial
    new set is ever committed.

    Returns ``True`` only when the document UPDATE matched and the
    transaction committed.
    """
    chunk_rows = [
        Chunk(
            document_id=claim.id,
            workspace_id=claim.workspace_id,
            chunk_index=index,
            content=chunk.content,
            page_number=chunk.page_number,
            source_type=chunk.source_type,
            chunk_version=chunk.chunk_version,
            embedding_model=chunk.embedding_model,
            embedding_dim=chunk.embedding_dim,
            embedding=list(chunk.embedding),
        )
        for index, chunk in enumerate(result.chunks)
    ]

    try:
        session.execute(
            delete(Chunk)
            .where(
                Chunk.document_id == claim.id,
                Chunk.workspace_id == claim.workspace_id,
            )
            .execution_options(synchronize_session=False)
        )
    except Exception:
        session.rollback()
        raise

    try:
        session.add_all(chunk_rows)
        # Flush now so a unique/FK/vector failure surfaces here, while the
        # transaction can still be rolled back as a chunk conflict. A
        # deferred failure at commit() would be indistinguishable from a
        # transaction-level error.
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise ChunkConflict() from exc

    try:
        indexed = session.execute(
            update(Document)
            .where(
                Document.id == claim.id,
                Document.workspace_id == claim.workspace_id,
                Document.status.in_(("processing", "retrying")),
                Document.processing_attempts == claim.attempt_no,
            )
            .values(
                status="indexed",
                indexed_at=func.now(),
                updated_at=func.now(),
                error_code=None,
                error_message=None,
                retryable=False,
            )
            .returning(Document.id)
            .execution_options(synchronize_session=False)
        ).one_or_none()

        if indexed is None:
            # A sweeper or a newer claim won. The DELETE and INSERT above
            # are part of this transaction and disappear with the rollback.
            session.rollback()
            logger.warning(
                "document.stale_commit document_id=%s workspace_id=%s "
                "attempt=%d",
                claim.id,
                claim.workspace_id,
                claim.attempt_no,
            )
            return False

        session.execute(
            update(ProcessingRun)
            .where(
                ProcessingRun.document_id == claim.id,
                ProcessingRun.workspace_id == claim.workspace_id,
                ProcessingRun.attempt_no == claim.attempt_no,
                ProcessingRun.finished_at.is_(None),
            )
            .values(stage="indexed", finished_at=func.now())
            .execution_options(synchronize_session=False)
        )
        session.commit()
    except Exception:
        session.rollback()
        raise

    logger.info(
        "Document %s committed as indexed (attempt %d, %d chunk(s)).",
        claim.id,
        claim.attempt_no,
        len(chunk_rows),
    )
    return True


def handle_failure(
    session: Session,
    claim: ClaimedDocument,
    error: ProcessingError,
    max_attempts: int,
    *,
    base_seconds: float,
    maximum_seconds: float,
    jitter_seconds: float,
    rng: RandomSource,
) -> FailureOutcome:
    """Classify a failed attempt and persist the next document state.

    The classification is:

    * transient with a claim remaining -> ``retrying`` + a publish delay;
    * transient at the cap -> ``failed`` / ``ATTEMPTS_EXHAUSTED``;
    * permanent-final -> ``failed`` with ``retryable=false``;
    * permanent-fixable -> ``failed`` with ``retryable=true``.

    The document update and the run-row close are guarded by the same
    claim identity. If either matches zero rows, the whole transaction is
    rolled back and ``STALE`` is returned: another worker or the sweeper
    owns the latest state. Any SQLAlchemy failure is rolled back and
    re-raised immediately; this function never tries a second write after
    a database error.
    """
    if error.transient and claim.attempt_no < max_attempts:
        status = FailureStatus.RETRYING
        document_status = "retrying"
        retryable = True
        run_stage = "retrying"
        error_code = error.code
        error_message = error.safe_message
        countdown: float | None = compute_backoff(
            claim.attempt_no,
            base_seconds,
            maximum_seconds,
            jitter_seconds,
            rng,
        )
    else:
        status = FailureStatus.FAILED
        document_status = "failed"
        run_stage = "failed"
        countdown = None

        if error.transient:
            # The cap is a claim count. The fifth claim has no next
            # automatic attempt, so no backoff is computed for it.
            error_code = ATTEMPTS_EXHAUSTED
            error_message = ATTEMPTS_EXHAUSTED_MESSAGE
            retryable = False
        else:
            error_code = error.code
            error_message = error.safe_message
            retryable = error.retryable

    try:
        updated = session.execute(
            update(Document)
            .where(
                Document.id == claim.id,
                Document.workspace_id == claim.workspace_id,
                Document.status.in_(("processing", "retrying")),
                Document.processing_attempts == claim.attempt_no,
            )
            .values(
                status=document_status,
                error_code=error_code,
                error_message=error_message,
                retryable=retryable,
                updated_at=func.now(),
            )
            .returning(Document.id)
            .execution_options(synchronize_session=False)
        ).one_or_none()
    except SQLAlchemyError:
        session.rollback()
        raise

    if updated is None:
        session.rollback()
        logger.warning(
            "document.stale_failure document_id=%s workspace_id=%s "
            "attempt=%d",
            claim.id,
            claim.workspace_id,
            claim.attempt_no,
        )
        return FailureOutcome(status=FailureStatus.STALE)

    try:
        closed = session.execute(
            update(ProcessingRun)
            .where(
                ProcessingRun.document_id == claim.id,
                ProcessingRun.workspace_id == claim.workspace_id,
                ProcessingRun.attempt_no == claim.attempt_no,
                ProcessingRun.finished_at.is_(None),
            )
            .values(
                stage=run_stage,
                finished_at=func.now(),
                error_code=error_code,
                error_message=error_message,
            )
            .returning(ProcessingRun.id)
            .execution_options(synchronize_session=False)
        ).one_or_none()
    except SQLAlchemyError:
        session.rollback()
        raise

    if closed is None:
        # The document update above is part of this transaction and must
        # not survive when the run row cannot be closed by this worker.
        session.rollback()
        logger.warning(
            "document.stale_failure document_id=%s workspace_id=%s "
            "attempt=%d",
            claim.id,
            claim.workspace_id,
            claim.attempt_no,
        )
        return FailureOutcome(status=FailureStatus.STALE)

    try:
        session.commit()
    except SQLAlchemyError:
        session.rollback()
        raise

    logger.info(
        "Document %s failure classified as %s (attempt %d/%d, code=%s).",
        claim.id,
        status.value,
        claim.attempt_no,
        max_attempts,
        error_code,
    )
    return FailureOutcome(status=status, countdown=countdown)
