"""Tests for the stuck-document sweeper and its Beat entry point.

The sweeper is the recovery path for the four states in which a document
can be left behind by a crash, a lost publish, or a hard worker timeout.
The rule tests must never sweep the development database's real documents.
They therefore run against temporary copies of ``documents`` and
``processing_runs`` created on the savepoint fixture's connection; PostgreSQL
resolves ``pg_temp`` before ``public``. The advisory-lock tests exercise the
lock lifecycle against real connections without running a global sweep.

The expected lifecycle is always the same: transition and commit first,
publish second, touch ``updated_at`` last (and only after a successful
publish). A publish failure must never undo the committed transition and
must never advance the retry marker.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import psycopg
import pytest
from sqlalchemy import select, text

from app.config import settings
from app.db.models import Document, ProcessingRun, User, Workspace
from app.db.session import engine, psycopg_dsn
from app.storage.keys import storage_key_for
from app.worker import sweeper
from app.worker.pipeline import (
    ATTEMPTS_EXHAUSTED,
    ATTEMPTS_EXHAUSTED_MESSAGE,
)
from tests.fakes.publisher import RecordingTaskPublisher

PDF_BYTES = b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\n%%EOF\n"
STALE_AGE = timedelta(days=1)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stale_time() -> datetime:
    """A timestamp beyond every configured stale cutoff."""
    return _now() - STALE_AGE


@pytest.fixture
def isolated_session(db_session):
    """Run rule tests against temporary shadow tables.

    The development database contains real documents, including stale
    queued uploads. A global sweep in a test would publish them. Creating
    temporary copies of the two tables the sweeper touches keeps every test
    on its own rows: PostgreSQL searches ``pg_temp`` before ``public`` and
    the copies disappear when the fixture's outer transaction rolls back.
    """
    connection = db_session.get_bind()
    connection.exec_driver_sql(
        "CREATE TEMP TABLE documents "
        "(LIKE public.documents INCLUDING ALL) "
        "ON COMMIT PRESERVE ROWS"
    )
    connection.exec_driver_sql(
        "CREATE TEMP TABLE processing_runs "
        "(LIKE public.processing_runs INCLUDING ALL) "
        "ON COMMIT PRESERVE ROWS"
    )
    return db_session


def _owner(session):
    workspace = Workspace(name="sweep-%s" % uuid.uuid4().hex)
    user = User(
        email="sweep-%s@example.com" % uuid.uuid4().hex,
        password_hash="not-used-by-the-worker",
    )
    session.add_all([workspace, user])
    session.flush()
    return user, workspace


def _make_document(
    session,
    *,
    status: str,
    updated_at: datetime,
    processing_attempts: int = 0,
    user=None,
    workspace=None,
) -> Document:
    if user is None or workspace is None:
        user, workspace = _owner(session)

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
        updated_at=updated_at,
    )
    session.add(document)
    session.flush()
    return document


def _make_run(
    session,
    document: Document,
    *,
    attempt_no: int,
    stage: str = "claimed",
    finished_at: datetime | None = None,
) -> ProcessingRun:
    run = ProcessingRun(
        document_id=document.id,
        workspace_id=document.workspace_id,
        attempt_no=attempt_no,
        stage=stage,
        finished_at=finished_at,
    )
    session.add(run)
    session.flush()
    return run


def _runs(session, document_id: uuid.UUID) -> list[ProcessingRun]:
    return list(
        session.execute(
            select(ProcessingRun)
            .where(ProcessingRun.document_id == document_id)
            .order_by(ProcessingRun.attempt_no)
        ).scalars()
    )


class _SelectivePublisher:
    """Fail one document and succeed for every other document."""

    def __init__(self, failing_id: uuid.UUID) -> None:
        self.failing_id = failing_id
        self.calls: list[tuple[uuid.UUID, float]] = []

    def enqueue_document(
        self,
        document_id: uuid.UUID,
        *,
        countdown: float = 0,
    ) -> bool:
        self.calls.append((document_id, countdown))
        return document_id != self.failing_id


def _assert_summary(
    summary: sweeper.SweepSummary,
    *,
    scanned: int,
    recovered: int,
    failed: int,
    publish_failures: int,
    more_remaining: bool,
) -> None:
    assert summary.scanned == scanned
    assert summary.recovered == recovered
    assert summary.failed == failed
    assert summary.publish_failures == publish_failures
    assert summary.more_remaining is more_remaining
    assert summary.duration_ms >= 0


def _summary_logs(caplog) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == "axiom.worker.sweeper"
    ]


# ---------------------------------------------------------------------------
# The four recovery rules
# ---------------------------------------------------------------------------


def test_uploaded_rows_are_queued_published_and_timestamped(
    isolated_session,
    caplog,
):
    old = _stale_time()
    document = _make_document(
        isolated_session,
        status="uploaded",
        updated_at=old,
    )
    publisher = RecordingTaskPublisher()

    with caplog.at_level(logging.INFO, logger="axiom.worker.sweeper"):
        summary = sweeper.sweep(isolated_session, publisher)

    _assert_summary(
        summary,
        scanned=1,
        recovered=1,
        failed=0,
        publish_failures=0,
        more_remaining=False,
    )
    assert publisher.calls == [(document.id, 0)]

    isolated_session.refresh(document)
    assert document.status == "queued"
    assert document.processing_attempts == 0
    assert document.error_code is None
    assert document.error_message is None
    assert document.retryable is False
    assert document.updated_at > old

    logs = _summary_logs(caplog)
    assert any(
        "sweeper.summary" in message
        and "scanned=1" in message
        and "recovered=1" in message
        and "failed=0" in message
        and "publish_failures=0" in message
        and "more_remaining=False" in message
        and "duration_ms=" in message
        for message in logs
    )


def test_stale_queued_rows_are_published_without_changing_status(
    isolated_session,
):
    old = _stale_time()
    document = _make_document(
        isolated_session,
        status="queued",
        updated_at=old,
        processing_attempts=2,
    )
    publisher = RecordingTaskPublisher()

    summary = sweeper.sweep(isolated_session, publisher)

    _assert_summary(
        summary,
        scanned=1,
        recovered=1,
        failed=0,
        publish_failures=0,
        more_remaining=False,
    )
    assert publisher.calls == [(document.id, 0)]

    isolated_session.refresh(document)
    assert document.status == "queued"
    assert document.processing_attempts == 2
    assert document.updated_at > old


def test_stale_retrying_rows_are_published_without_changing_status(
    isolated_session,
):
    old = _stale_time()
    document = _make_document(
        isolated_session,
        status="retrying",
        updated_at=old,
        processing_attempts=3,
    )
    publisher = RecordingTaskPublisher()

    summary = sweeper.sweep(isolated_session, publisher)

    _assert_summary(
        summary,
        scanned=1,
        recovered=1,
        failed=0,
        publish_failures=0,
        more_remaining=False,
    )
    assert publisher.calls == [(document.id, 0)]

    isolated_session.refresh(document)
    assert document.status == "retrying"
    assert document.processing_attempts == 3
    assert document.updated_at > old


def test_stale_processing_rows_move_to_retrying_and_close_the_run(
    isolated_session,
):
    old = _stale_time()
    document = _make_document(
        isolated_session,
        status="processing",
        updated_at=old,
        processing_attempts=2,
    )
    closed_run = _make_run(
        isolated_session,
        document,
        attempt_no=1,
        stage="retrying",
        finished_at=old,
    )
    open_run = _make_run(
        isolated_session,
        document,
        attempt_no=2,
        stage="claimed",
    )
    publisher = RecordingTaskPublisher()

    summary = sweeper.sweep(isolated_session, publisher)

    _assert_summary(
        summary,
        scanned=1,
        recovered=1,
        failed=0,
        publish_failures=0,
        more_remaining=False,
    )
    assert publisher.calls == [(document.id, 0)]

    isolated_session.expire_all()
    stored = isolated_session.get(Document, document.id)
    assert stored is not None
    assert stored.status == "retrying"
    assert stored.processing_attempts == 2
    assert stored.error_code == "PROCESSING_TIMEOUT"
    assert stored.error_message == "Document processing exceeded its time limit."
    assert stored.retryable is True
    assert stored.updated_at > old

    runs = _runs(isolated_session, document.id)
    assert len(runs) == 2
    previous, timed_out = runs
    assert previous.id == closed_run.id
    assert previous.stage == "retrying"
    assert previous.finished_at is not None
    assert previous.error_code is None

    assert timed_out.id == open_run.id
    assert timed_out.stage == "timeout"
    assert timed_out.finished_at is not None
    assert timed_out.error_code == "PROCESSING_TIMEOUT"
    assert (
        timed_out.error_message
        == "Document processing exceeded its time limit."
    )


# ---------------------------------------------------------------------------
# Attempt caps and untouched rows
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["queued", "retrying"])
def test_stale_queued_and_retrying_at_the_cap_fail(
    isolated_session,
    status,
):
    old = _stale_time()
    document = _make_document(
        isolated_session,
        status=status,
        updated_at=old,
        processing_attempts=settings.max_processing_attempts,
    )
    publisher = RecordingTaskPublisher()

    summary = sweeper.sweep(isolated_session, publisher)

    _assert_summary(
        summary,
        scanned=1,
        recovered=0,
        failed=1,
        publish_failures=0,
        more_remaining=False,
    )
    assert publisher.calls == []

    isolated_session.refresh(document)
    assert document.status == "failed"
    assert document.processing_attempts == settings.max_processing_attempts
    assert document.error_code == ATTEMPTS_EXHAUSTED
    assert document.error_message == ATTEMPTS_EXHAUSTED_MESSAGE
    assert document.retryable is False
    assert document.updated_at > old


def test_stale_processing_at_the_cap_fails_and_closes_the_run(
    isolated_session,
):
    old = _stale_time()
    document = _make_document(
        isolated_session,
        status="processing",
        updated_at=old,
        processing_attempts=settings.max_processing_attempts,
    )
    open_run = _make_run(
        isolated_session,
        document,
        attempt_no=settings.max_processing_attempts,
        stage="claimed",
    )
    publisher = RecordingTaskPublisher()

    summary = sweeper.sweep(isolated_session, publisher)

    _assert_summary(
        summary,
        scanned=1,
        recovered=0,
        failed=1,
        publish_failures=0,
        more_remaining=False,
    )
    assert publisher.calls == []

    isolated_session.expire_all()
    stored = isolated_session.get(Document, document.id)
    assert stored is not None
    assert stored.status == "failed"
    assert stored.processing_attempts == settings.max_processing_attempts
    assert stored.error_code == ATTEMPTS_EXHAUSTED
    assert stored.error_message == ATTEMPTS_EXHAUSTED_MESSAGE
    assert stored.retryable is False
    assert stored.updated_at > old

    run = isolated_session.get(ProcessingRun, open_run.id)
    assert run is not None
    assert run.stage == "timeout"
    assert run.finished_at is not None
    assert run.error_code == "PROCESSING_TIMEOUT"


def test_fresh_rows_in_every_state_are_untouched(isolated_session):
    fresh = _now()
    user, workspace = _owner(isolated_session)
    documents = {
        status: _make_document(
            isolated_session,
            status=status,
            updated_at=fresh,
            processing_attempts=1 if status == "processing" else 0,
            user=user,
            workspace=workspace,
        )
        for status in ("uploaded", "queued", "retrying", "processing")
    }
    publisher = RecordingTaskPublisher()

    summary = sweeper.sweep(isolated_session, publisher)

    _assert_summary(
        summary,
        scanned=0,
        recovered=0,
        failed=0,
        publish_failures=0,
        more_remaining=False,
    )
    assert publisher.calls == []

    for status, document in documents.items():
        isolated_session.refresh(document)
        assert document.status == status
        assert document.updated_at == fresh
        assert document.processing_attempts == (
            1 if status == "processing" else 0
        )


# ---------------------------------------------------------------------------
# Batch limits, idempotency, and publication failures
# ---------------------------------------------------------------------------


def test_batch_limit_is_per_category_and_oldest_first(isolated_session):
    user, workspace = _owner(isolated_session)
    base = _stale_time() - timedelta(days=2)
    oldest_uploaded = _make_document(
        isolated_session,
        status="uploaded",
        updated_at=base,
        user=user,
        workspace=workspace,
    )
    newer_uploaded = _make_document(
        isolated_session,
        status="uploaded",
        updated_at=base + timedelta(minutes=10),
        user=user,
        workspace=workspace,
    )
    oldest_queued = _make_document(
        isolated_session,
        status="queued",
        updated_at=base + timedelta(minutes=20),
        user=user,
        workspace=workspace,
    )
    newer_queued = _make_document(
        isolated_session,
        status="queued",
        updated_at=base + timedelta(minutes=30),
        user=user,
        workspace=workspace,
    )
    newer_timestamps = {
        newer_uploaded.id: newer_uploaded.updated_at,
        newer_queued.id: newer_queued.updated_at,
    }

    publisher = RecordingTaskPublisher()
    first = sweeper.sweep(isolated_session, publisher, batch_size=1)

    _assert_summary(
        first,
        scanned=2,
        recovered=2,
        failed=0,
        publish_failures=0,
        more_remaining=True,
    )
    assert publisher.calls == [
        (oldest_uploaded.id, 0),
        (oldest_queued.id, 0),
    ]

    isolated_session.expire_all()
    assert isolated_session.get(Document, oldest_uploaded.id).status == "queued"
    assert isolated_session.get(Document, oldest_queued.id).status == "queued"
    still_uploaded = isolated_session.get(Document, newer_uploaded.id)
    still_queued = isolated_session.get(Document, newer_queued.id)
    assert still_uploaded is not None
    assert still_uploaded.status == "uploaded"
    assert still_uploaded.updated_at == newer_timestamps[newer_uploaded.id]
    assert still_queued is not None
    assert still_queued.status == "queued"
    assert still_queued.updated_at == newer_timestamps[newer_queued.id]

    publisher.calls.clear()
    second = sweeper.sweep(isolated_session, publisher, batch_size=1)

    _assert_summary(
        second,
        scanned=2,
        recovered=2,
        failed=0,
        publish_failures=0,
        more_remaining=False,
    )
    assert publisher.calls == [
        (newer_uploaded.id, 0),
        (newer_queued.id, 0),
    ]


def test_multi_row_recovery_publishes_oldest_first(isolated_session):
    user, workspace = _owner(isolated_session)
    base = _stale_time() - timedelta(days=2)

    # Insert newest first so physical table order cannot accidentally
    # produce the expected ordering.
    newest_uploaded = _make_document(
        isolated_session,
        status="uploaded",
        updated_at=base + timedelta(minutes=20),
        user=user,
        workspace=workspace,
    )
    middle_uploaded = _make_document(
        isolated_session,
        status="uploaded",
        updated_at=base + timedelta(minutes=10),
        user=user,
        workspace=workspace,
    )
    oldest_uploaded = _make_document(
        isolated_session,
        status="uploaded",
        updated_at=base,
        user=user,
        workspace=workspace,
    )
    newest_processing = _make_document(
        isolated_session,
        status="processing",
        updated_at=base + timedelta(minutes=50),
        processing_attempts=1,
        user=user,
        workspace=workspace,
    )
    middle_processing = _make_document(
        isolated_session,
        status="processing",
        updated_at=base + timedelta(minutes=40),
        processing_attempts=1,
        user=user,
        workspace=workspace,
    )
    oldest_processing = _make_document(
        isolated_session,
        status="processing",
        updated_at=base + timedelta(minutes=30),
        processing_attempts=1,
        user=user,
        workspace=workspace,
    )
    publisher = RecordingTaskPublisher()

    summary = sweeper.sweep(isolated_session, publisher, batch_size=3)

    _assert_summary(
        summary,
        scanned=6,
        recovered=6,
        failed=0,
        publish_failures=0,
        more_remaining=False,
    )
    assert publisher.calls == [
        (oldest_uploaded.id, 0),
        (middle_uploaded.id, 0),
        (newest_uploaded.id, 0),
        (oldest_processing.id, 0),
        (middle_processing.id, 0),
        (newest_processing.id, 0),
    ]


def test_a_second_sweep_is_idempotent(isolated_session):
    old = _stale_time()
    document = _make_document(
        isolated_session,
        status="queued",
        updated_at=old,
        processing_attempts=2,
    )
    publisher = RecordingTaskPublisher()

    first = sweeper.sweep(isolated_session, publisher)
    _assert_summary(
        first,
        scanned=1,
        recovered=1,
        failed=0,
        publish_failures=0,
        more_remaining=False,
    )

    isolated_session.refresh(document)
    timestamp_after_first = document.updated_at

    second = sweeper.sweep(isolated_session, publisher)

    _assert_summary(
        second,
        scanned=0,
        recovered=0,
        failed=0,
        publish_failures=0,
        more_remaining=False,
    )
    assert publisher.calls == [(document.id, 0)]

    isolated_session.refresh(document)
    assert document.status == "queued"
    assert document.processing_attempts == 2
    assert document.updated_at == timestamp_after_first


def test_publish_failure_keeps_the_timestamp_and_counts_the_failure(
    isolated_session,
    caplog,
):
    old = _stale_time()
    document = _make_document(
        isolated_session,
        status="queued",
        updated_at=old,
        processing_attempts=2,
    )
    publisher = RecordingTaskPublisher(result=False)

    with caplog.at_level(logging.WARNING, logger="axiom.worker.sweeper"):
        summary = sweeper.sweep(isolated_session, publisher)

    _assert_summary(
        summary,
        scanned=1,
        recovered=0,
        failed=0,
        publish_failures=1,
        more_remaining=False,
    )
    assert publisher.calls == [(document.id, 0)]

    isolated_session.refresh(document)
    assert document.status == "queued"
    assert document.processing_attempts == 2
    assert document.updated_at == old

    logs = _summary_logs(caplog)
    assert any(
        "sweeper.publish_failed" in message
        and str(document.id) in message
        and "status=queued" in message
        for message in logs
    )

    publisher.result = True
    publisher.calls.clear()
    retried = sweeper.sweep(isolated_session, publisher)

    _assert_summary(
        retried,
        scanned=1,
        recovered=1,
        failed=0,
        publish_failures=0,
        more_remaining=False,
    )
    assert publisher.calls == [(document.id, 0)]

    isolated_session.refresh(document)
    assert document.updated_at > old


def test_one_publish_failure_does_not_abort_the_batch(
    isolated_session,
):
    user, workspace = _owner(isolated_session)
    base = _stale_time() - timedelta(days=1)
    failing = _make_document(
        isolated_session,
        status="queued",
        updated_at=base,
        user=user,
        workspace=workspace,
    )
    succeeding = _make_document(
        isolated_session,
        status="queued",
        updated_at=base + timedelta(minutes=10),
        user=user,
        workspace=workspace,
    )
    failing_timestamp = failing.updated_at
    succeeding_timestamp = succeeding.updated_at
    publisher = _SelectivePublisher(failing.id)

    summary = sweeper.sweep(isolated_session, publisher)

    _assert_summary(
        summary,
        scanned=2,
        recovered=1,
        failed=0,
        publish_failures=1,
        more_remaining=False,
    )
    assert publisher.calls == [
        (failing.id, 0),
        (succeeding.id, 0),
    ]

    isolated_session.refresh(failing)
    isolated_session.refresh(succeeding)
    assert failing.status == "queued"
    assert failing.updated_at == failing_timestamp
    assert succeeding.status == "queued"
    assert succeeding.updated_at > succeeding_timestamp


def test_processing_transition_is_committed_when_publish_fails(
    isolated_session,
):
    old = _stale_time()
    document = _make_document(
        isolated_session,
        status="processing",
        updated_at=old,
        processing_attempts=1,
    )
    open_run = _make_run(
        isolated_session,
        document,
        attempt_no=1,
        stage="claimed",
    )
    publisher = RecordingTaskPublisher(result=False)

    summary = sweeper.sweep(isolated_session, publisher)

    _assert_summary(
        summary,
        scanned=1,
        recovered=0,
        failed=0,
        publish_failures=1,
        more_remaining=False,
    )
    assert publisher.calls == [(document.id, 0)]

    isolated_session.expire_all()
    stored = isolated_session.get(Document, document.id)
    assert stored is not None
    assert stored.status == "retrying"
    assert stored.error_code == "PROCESSING_TIMEOUT"
    assert stored.retryable is True
    assert stored.updated_at == old

    run = isolated_session.get(ProcessingRun, open_run.id)
    assert run is not None
    assert run.stage == "timeout"
    assert run.finished_at is not None


# ---------------------------------------------------------------------------
# Advisory lock
# ---------------------------------------------------------------------------


def _require_database() -> None:
    try:
        probe = psycopg.connect(psycopg_dsn(), connect_timeout=3)
        probe.close()
    except Exception as exc:
        pytest.skip("PostgreSQL not available: %s" % exc)


def test_advisory_lock_blocks_a_sweep_from_a_separate_connection(
    monkeypatch,
    caplog,
):
    _require_database()
    swept = []

    def fake_sweep(session, publisher):
        swept.append((session, publisher))
        return sweeper.SweepSummary(
            scanned=1,
            recovered=1,
            duration_ms=1,
        )

    monkeypatch.setattr(sweeper, "sweep", fake_sweep)

    lock_connection = engine.connect()
    lock_held = False
    try:
        lock_held = bool(
            lock_connection.execute(
                text("SELECT pg_try_advisory_lock(:key)"),
                {"key": sweeper.SWEEPER_ADVISORY_LOCK_KEY},
            ).scalar_one()
        )
        assert lock_held is True
        lock_connection.commit()

        publisher = RecordingTaskPublisher()
        with caplog.at_level(
            logging.INFO,
            logger="axiom.worker.sweeper",
        ):
            blocked = sweeper.run_sweeper(publisher)

        _assert_summary(
            blocked,
            scanned=0,
            recovered=0,
            failed=0,
            publish_failures=0,
            more_remaining=False,
        )
        assert swept == []
        assert any(
            "sweeper.lock_busy" in message
            for message in _summary_logs(caplog)
        )
    finally:
        if lock_held:
            lock_connection.execute(
                text("SELECT pg_advisory_unlock(:key)"),
                {"key": sweeper.SWEEPER_ADVISORY_LOCK_KEY},
            ).scalar_one()
            lock_connection.commit()
        lock_connection.close()

    publisher = RecordingTaskPublisher()
    recovered = sweeper.run_sweeper(publisher)

    assert recovered.scanned == 1
    assert recovered.recovered == 1
    assert len(swept) == 1
    assert swept[0][1] is publisher


def test_the_lock_is_released_when_the_sweep_raises(monkeypatch):
    _require_database()

    def exploding_sweep(session, publisher):
        raise RuntimeError("sweep failed")

    monkeypatch.setattr(sweeper, "sweep", exploding_sweep)

    with pytest.raises(RuntimeError, match="sweep failed"):
        sweeper.run_sweeper(RecordingTaskPublisher())

    lock_connection = engine.connect()
    lock_held = False
    try:
        lock_held = bool(
            lock_connection.execute(
                text("SELECT pg_try_advisory_lock(:key)"),
                {"key": sweeper.SWEEPER_ADVISORY_LOCK_KEY},
            ).scalar_one()
        )
        assert lock_held is True
    finally:
        if lock_held:
            lock_connection.execute(
                text("SELECT pg_advisory_unlock(:key)"),
                {"key": sweeper.SWEEPER_ADVISORY_LOCK_KEY},
            ).scalar_one()
            lock_connection.commit()
        lock_connection.close()


class _FakeLockConnection:
    """Minimal connection double for the unlock-failure paths."""

    def __init__(self, *, result=None, error=None) -> None:
        self.result = result
        self.error = error
        self.invalidated = False
        self.committed = False

    def execute(self, statement, parameters=None):
        if self.error is not None:
            raise self.error
        return SimpleNamespace(scalar_one=lambda: self.result)

    def commit(self) -> None:
        self.committed = True

    def invalidate(self) -> None:
        self.invalidated = True


@pytest.mark.parametrize(
    ("result", "error"),
    [
        (False, None),
        (None, RuntimeError("connection lost")),
    ],
    ids=["unlock-returned-false", "unlock-raised"],
)
def test_unlock_failure_invalidates_the_connection(result, error):
    connection = _FakeLockConnection(result=result, error=error)

    sweeper._release_lock(connection)

    assert connection.invalidated is True
    assert connection.committed is False


def test_successful_unlock_commits_without_invalidating():
    connection = _FakeLockConnection(result=True)

    sweeper._release_lock(connection)

    assert connection.invalidated is False
    assert connection.committed is True


# ---------------------------------------------------------------------------
# Task and Beat registration
# ---------------------------------------------------------------------------


def test_the_sweep_task_and_beat_entry_are_registered():
    from app.worker import tasks
    from app.worker.celery_app import celery_app

    assert tasks.SWEEP_DOCUMENTS_TASK_NAME == "documents.sweep"
    assert tasks.sweep_documents.name == "documents.sweep"
    assert tasks.sweep_documents.ignore_result is True
    assert (
        celery_app.tasks["documents.sweep"].name == "documents.sweep"
    )

    entry = celery_app.conf.beat_schedule["sweep-stuck-documents"]
    assert entry["task"] == "documents.sweep"
    assert entry["schedule"] == settings.sweeper_interval_seconds


def test_the_sweep_task_delegates_to_the_locked_runner(monkeypatch):
    from app.worker import tasks

    expected = sweeper.SweepSummary(
        scanned=4,
        recovered=3,
        failed=1,
        publish_failures=0,
        more_remaining=False,
        duration_ms=12,
    )
    calls = []

    def fake_run_sweeper():
        calls.append(True)
        return expected

    monkeypatch.setattr(tasks, "run_sweeper", fake_run_sweeper)

    assert tasks.sweep_documents.run() is expected
    assert calls == [True]
