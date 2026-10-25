"""Tests for the Phase 4 claim and processing-run lifecycle.

The claim is the only entry to ``processing`` and must be safe under
duplicate Celery delivery. These tests exercise it through a real
PostgreSQL session: the conditional UPDATE, the attempt cap, the run row,
and the rollback that keeps the document claimable when the run insert
fails.

The shared ``db_session`` fixture joins an outer transaction through a
savepoint. ``claim_document`` owns its transaction and may roll it back,
so each test commits its preconditions first; the outer transaction still
rolls everything back at teardown.
"""

from __future__ import annotations

import hashlib
import logging
import uuid

import pytest
from sqlalchemy import event, func, select, update
from sqlalchemy.exc import IntegrityError

from app.db.models import (
    Chunk,
    Document,
    EMBEDDING_DIM,
    ProcessingRun,
    User,
    Workspace,
)
from app.processing import (
    ChunkConflict,
    NewChunk,
    PermanentFinalError,
    ProcessResult,
)
from app.storage.keys import storage_key_for
from app.worker.pipeline import ClaimedDocument, claim_document, commit_success

PDF_BYTES = b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\n%%EOF\n"


def _runs(session, document_id: uuid.UUID) -> list[ProcessingRun]:
    return list(
        session.execute(
            select(ProcessingRun)
            .where(ProcessingRun.document_id == document_id)
            .order_by(ProcessingRun.attempt_no)
        ).scalars()
    )


def _chunks(session, document_id: uuid.UUID) -> list[Chunk]:
    return list(
        session.execute(
            select(Chunk)
            .where(Chunk.document_id == document_id)
            .order_by(Chunk.chunk_index)
        ).scalars()
    )


def _new_chunk(content: str, *, page_number: int | None = None) -> NewChunk:
    """A processor result chunk with a real 768-dimension zero vector."""
    return NewChunk(
        content=content,
        page_number=page_number,
        source_type="text",
        chunk_version="v1",
        embedding_model="test-768",
        embedding_dim=EMBEDDING_DIM,
        embedding=tuple(0.0 for _ in range(EMBEDDING_DIM)),
    )


def _stored_chunk(document: Document, chunk_index: int, content: str) -> Chunk:
    return Chunk(
        document_id=document.id,
        workspace_id=document.workspace_id,
        chunk_index=chunk_index,
        content=content,
        page_number=None,
        source_type="text",
        chunk_version="v1",
        embedding_model="test-768",
        embedding_dim=EMBEDDING_DIM,
        embedding=[0.0] * EMBEDDING_DIM,
    )


def _make_document(
    db_session,
    *,
    status: str = "queued",
    processing_attempts: int = 0,
    retryable: bool = False,
    error_code: str | None = None,
    error_message: str | None = None,
) -> Document:
    """Create the committed precondition for one claim.

    The commit is deliberate. ``claim_document`` rolls back its own unit
    of work on a no-op, and a rollback inside the savepoint fixture would
    otherwise erase the document this test just created.
    """
    workspace = Workspace(name="ws-%s" % uuid.uuid4().hex)
    user = User(
        email="worker-%s@example.com" % uuid.uuid4().hex,
        password_hash="not-used-by-the-worker",
    )
    db_session.add_all([workspace, user])
    db_session.flush()

    document_id = uuid.uuid4()
    document = Document(
        id=document_id,
        workspace_id=workspace.id,
        uploaded_by=user.id,
        original_filename="report.pdf",
        storage_key=storage_key_for(document_id),
        mime_type="application/pdf",
        size_bytes=len(PDF_BYTES),
        checksum=hashlib.sha256(PDF_BYTES).hexdigest(),
        status=status,
        processing_attempts=processing_attempts,
        error_code=error_code,
        error_message=error_message,
        retryable=retryable,
    )
    db_session.add(document)
    db_session.commit()
    return document


# ---------------------------------------------------------------------------
# The winning claim
# ---------------------------------------------------------------------------

def test_claim_moves_a_queued_document_to_processing(db_session):
    document = _make_document(
        db_session,
        retryable=True,
        error_code="OLD_CODE",
        error_message="a previous failure",
    )

    claimed = claim_document(db_session, document.id, max_attempts=5)

    assert isinstance(claimed, ClaimedDocument)
    assert claimed.id == document.id
    assert claimed.workspace_id == document.workspace_id
    assert claimed.attempt_no == 1
    assert claimed.storage_key == document.storage_key
    assert claimed.checksum == document.checksum
    assert claimed.size_bytes == document.size_bytes
    assert claimed.mime_type == document.mime_type
    assert claimed.original_filename == document.original_filename

    db_session.refresh(document)
    assert document.status == "processing"
    assert document.processing_attempts == 1
    assert document.error_code is None
    assert document.error_message is None
    assert document.retryable is False

    runs = _runs(db_session, document.id)
    assert len(runs) == 1
    assert runs[0].workspace_id == document.workspace_id
    assert runs[0].attempt_no == 1
    assert runs[0].stage == "claimed"
    assert runs[0].started_at is not None
    assert runs[0].finished_at is None


def test_a_retrying_document_is_claimable(db_session):
    document = _make_document(
        db_session,
        status="retrying",
        processing_attempts=2,
        retryable=True,
        error_code="STORAGE_UNAVAILABLE",
        error_message="storage was briefly unavailable",
    )

    claimed = claim_document(db_session, document.id, max_attempts=5)

    assert claimed is not None
    assert claimed.attempt_no == 3

    db_session.refresh(document)
    assert document.status == "processing"
    assert document.processing_attempts == 3
    assert document.error_code is None
    assert document.retryable is False

    assert [run.attempt_no for run in _runs(db_session, document.id)] == [3]


def test_a_duplicate_delivery_does_not_claim_again(db_session):
    document = _make_document(db_session)

    first = claim_document(db_session, document.id, max_attempts=5)
    second = claim_document(db_session, document.id, max_attempts=5)

    assert first is not None
    assert second is None

    db_session.refresh(document)
    assert document.status == "processing"
    assert document.processing_attempts == 1

    runs = _runs(db_session, document.id)
    assert len(runs) == 1
    assert runs[0].attempt_no == 1


# ---------------------------------------------------------------------------
# No-op paths
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "status", ["uploaded", "processing", "indexed", "failed"]
)
def test_documents_outside_the_claimable_states_are_not_claimed(
    db_session, status
):
    document = _make_document(
        db_session,
        status=status,
        processing_attempts=2,
        retryable=True,
        error_code="PREVIOUS",
        error_message="previous state",
    )

    assert claim_document(db_session, document.id, max_attempts=5) is None

    db_session.refresh(document)
    assert document.status == status
    assert document.processing_attempts == 2
    assert document.error_code == "PREVIOUS"
    assert document.retryable is True
    assert _runs(db_session, document.id) == []


def test_a_deleted_document_is_not_claimed(db_session):
    document = _make_document(db_session)
    document_id = document.id

    db_session.delete(document)
    db_session.commit()

    assert claim_document(db_session, document_id, max_attempts=5) is None
    assert _runs(db_session, document_id) == []


def test_a_document_at_the_attempt_cap_is_not_claimed(db_session, caplog):
    document = _make_document(db_session, processing_attempts=5)

    with caplog.at_level(logging.DEBUG, logger="axiom.worker.pipeline"):
        claimed = claim_document(db_session, document.id, max_attempts=5)

    assert claimed is None
    db_session.refresh(document)
    assert document.status == "queued"
    assert document.processing_attempts == 5
    assert _runs(db_session, document.id) == []

    debug_messages = [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.DEBUG
    ]
    assert any(str(document.id) in message for message in debug_messages)


def test_the_last_attempt_below_the_cap_is_still_claimable(db_session):
    """The cap is the number of claims, not a zero-based counter."""
    document = _make_document(db_session, processing_attempts=4)

    claimed = claim_document(db_session, document.id, max_attempts=5)

    assert claimed is not None
    assert claimed.attempt_no == 5

    db_session.refresh(document)
    assert document.status == "processing"
    assert document.processing_attempts == 5
    assert [run.attempt_no for run in _runs(db_session, document.id)] == [5]


# ---------------------------------------------------------------------------
# One transaction: the claim and its run row
# ---------------------------------------------------------------------------

def test_a_failed_run_insert_rolls_the_claim_back(db_session):
    """The UPDATE and the run INSERT must be all-or-nothing.

    A pre-existing run with the same attempt number makes the INSERT fail
    its unique constraint. If the claim were committed separately, the
    document would be stuck in ``processing`` with no corresponding run.
    """
    document = _make_document(db_session)
    db_session.add(
        ProcessingRun(
            document_id=document.id,
            workspace_id=document.workspace_id,
            attempt_no=1,
            stage="indexed",
        )
    )
    db_session.commit()

    with pytest.raises(IntegrityError):
        claim_document(db_session, document.id, max_attempts=5)

    db_session.refresh(document)
    assert document.status == "queued"
    assert document.processing_attempts == 0

    runs = _runs(db_session, document.id)
    assert len(runs) == 1
    assert runs[0].stage == "indexed"


# ---------------------------------------------------------------------------
# The guarded commit
# ---------------------------------------------------------------------------

def test_commit_success_indexes_a_document_with_zero_chunks(db_session):
    document = _make_document(db_session)
    claim = claim_document(db_session, document.id, max_attempts=5)
    assert claim is not None

    # Simulate a transient failure flag set after the claim; the guarded
    # commit must clear it when it wins.
    db_session.execute(
        update(Document)
        .where(Document.id == document.id)
        .values(
            error_code="STORAGE_UNAVAILABLE",
            error_message="temporary read failure",
            retryable=True,
        )
    )
    db_session.commit()

    assert commit_success(db_session, claim, ProcessResult(chunks=())) is True

    db_session.refresh(document)
    assert document.status == "indexed"
    assert document.indexed_at is not None
    assert document.error_code is None
    assert document.error_message is None
    assert document.retryable is False
    assert _chunks(db_session, document.id) == []

    runs = _runs(db_session, document.id)
    assert len(runs) == 1
    assert runs[0].stage == "indexed"
    assert runs[0].finished_at is not None


def test_commit_success_persists_chunks_in_sequential_order(db_session):
    document = _make_document(db_session)
    claim = claim_document(db_session, document.id, max_attempts=5)
    assert claim is not None

    result = ProcessResult(
        chunks=(
            _new_chunk("first passage", page_number=2),
            _new_chunk("second passage", page_number=3),
        )
    )

    assert commit_success(db_session, claim, result) is True

    chunks = _chunks(db_session, document.id)
    assert [chunk.chunk_index for chunk in chunks] == [0, 1]
    assert [chunk.content for chunk in chunks] == [
        "first passage",
        "second passage",
    ]
    assert [chunk.page_number for chunk in chunks] == [2, 3]
    assert all(chunk.document_id == document.id for chunk in chunks)
    assert all(chunk.workspace_id == document.workspace_id for chunk in chunks)
    assert all(chunk.embedding_dim == EMBEDDING_DIM for chunk in chunks)
    assert all(len(chunk.embedding) == EMBEDDING_DIM for chunk in chunks)

    db_session.refresh(document)
    assert document.status == "indexed"


def test_commit_success_replaces_existing_chunks(db_session):
    document = _make_document(db_session)
    old_chunks = [
        _stored_chunk(document, 0, "old first"),
        _stored_chunk(document, 1, "old second"),
    ]
    db_session.add_all(old_chunks)
    db_session.commit()
    old_ids = {chunk.id for chunk in old_chunks}
    assert len(old_ids) == 2

    claim = claim_document(db_session, document.id, max_attempts=5)
    assert claim is not None
    assert commit_success(
        db_session,
        claim,
        ProcessResult(chunks=(_new_chunk("replacement"),)),
    ) is True

    chunks = _chunks(db_session, document.id)
    assert [chunk.chunk_index for chunk in chunks] == [0]
    assert [chunk.content for chunk in chunks] == ["replacement"]
    assert {chunk.id for chunk in chunks}.isdisjoint(old_ids)


def test_duplicate_chunk_indexes_raise_chunk_conflict_and_keep_old_chunks(
    db_session,
):
    """A duplicate index can only come from another writer.

    ``commit_success`` assigns indexes sequentially, so this test injects
    the second insert that a concurrent writer would produce and lets the
    real unique constraint raise ``IntegrityError``.
    """
    document = _make_document(db_session)
    db_session.add(_stored_chunk(document, 0, "old chunk"))
    db_session.commit()

    claim = claim_document(db_session, document.id, max_attempts=5)
    assert claim is not None

    injected = {"added": False}

    def add_conflicting_chunk(session, flush_context, instances):
        if injected["added"]:
            return
        injected["added"] = True
        session.add(_stored_chunk(document, 0, "concurrent writer"))

    event.listen(db_session, "before_flush", add_conflicting_chunk)
    try:
        with pytest.raises(ChunkConflict) as caught:
            commit_success(
                db_session,
                claim,
                ProcessResult(chunks=(_new_chunk("new chunk"),)),
            )
    finally:
        event.remove(db_session, "before_flush", add_conflicting_chunk)

    assert isinstance(caught.value, PermanentFinalError)
    assert caught.value.retryable is False

    db_session.refresh(document)
    assert document.status == "processing"
    assert document.processing_attempts == 1

    chunks = _chunks(db_session, document.id)
    assert [chunk.content for chunk in chunks] == ["old chunk"]

    runs = _runs(db_session, document.id)
    assert len(runs) == 1
    assert runs[0].stage == "claimed"
    assert runs[0].finished_at is None


def test_a_stale_commit_returns_false_without_changing_anything(
    db_session, caplog
):
    document = _make_document(db_session)
    db_session.add(_stored_chunk(document, 0, "old chunk"))
    db_session.commit()

    first_claim = claim_document(db_session, document.id, max_attempts=5)
    assert first_claim is not None

    # Simulate the sweeper recovering a dead worker: processing -> retrying,
    # and close the first run row.
    db_session.execute(
        update(Document)
        .where(Document.id == document.id)
        .values(status="retrying", retryable=True, updated_at=func.now())
    )
    db_session.execute(
        update(ProcessingRun)
        .where(
            ProcessingRun.document_id == document.id,
            ProcessingRun.attempt_no == first_claim.attempt_no,
        )
        .values(
            stage="timeout",
            finished_at=func.now(),
            error_code="PROCESSING_TIMEOUT",
        )
    )
    db_session.commit()

    second_claim = claim_document(db_session, document.id, max_attempts=5)
    assert second_claim is not None
    assert second_claim.attempt_no == 2

    with caplog.at_level(logging.WARNING, logger="axiom.worker.pipeline"):
        committed = commit_success(
            db_session,
            first_claim,
            ProcessResult(chunks=(_new_chunk("stale chunk"),)),
        )

    assert committed is False

    db_session.refresh(document)
    assert document.status == "processing"
    assert document.processing_attempts == 2

    chunks = _chunks(db_session, document.id)
    assert [chunk.content for chunk in chunks] == ["old chunk"]

    runs = _runs(db_session, document.id)
    assert len(runs) == 2
    first_run = next(run for run in runs if run.attempt_no == 1)
    second_run = next(run for run in runs if run.attempt_no == 2)
    assert first_run.stage == "timeout"
    assert first_run.finished_at is not None
    assert second_run.stage == "claimed"
    assert second_run.finished_at is None

    assert any(
        "document.stale_commit" in record.getMessage()
        for record in caplog.records
    )


def test_a_deleted_document_is_not_resurrected(db_session):
    document = _make_document(db_session)
    claim = claim_document(db_session, document.id, max_attempts=5)
    assert claim is not None
    document_id = document.id

    db_session.delete(document)
    db_session.commit()

    assert commit_success(db_session, claim, ProcessResult(chunks=())) is False

    assert (
        db_session.execute(
            select(Document).where(Document.id == document_id)
        ).scalar_one_or_none()
        is None
    )
    assert _chunks(db_session, document_id) == []
    assert _runs(db_session, document_id) == []


def test_the_run_row_is_closed_exactly_once(db_session):
    document = _make_document(db_session)
    claim = claim_document(db_session, document.id, max_attempts=5)
    assert claim is not None

    assert commit_success(
        db_session,
        claim,
        ProcessResult(chunks=(_new_chunk("kept chunk"),)),
    ) is True

    runs = _runs(db_session, document.id)
    assert len(runs) == 1
    closed_at = runs[0].finished_at
    assert closed_at is not None
    assert runs[0].stage == "indexed"

    # A duplicate success delivery is stale: the document is already
    # indexed, so the guarded UPDATE rejects it and its chunk replacement
    # must not become visible.
    assert commit_success(
        db_session,
        claim,
        ProcessResult(chunks=(_new_chunk("ignored chunk"),)),
    ) is False

    runs = _runs(db_session, document.id)
    assert len(runs) == 1
    assert runs[0].stage == "indexed"
    assert runs[0].finished_at == closed_at
    assert [chunk.content for chunk in _chunks(db_session, document.id)] == [
        "kept chunk"
    ]
