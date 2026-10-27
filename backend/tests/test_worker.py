"""Tests for the Celery application and the worker service.

The worker container used to crash on startup with:

    ModuleNotFoundError: No module named 'app.worker.tasks'

``app.worker.celery_app`` lists ``app.worker.tasks`` in its ``include``
list, and Celery imports every module in that list before the worker
starts. The module did not exist, so the container exited with code 1
and no worker ever connected to the broker. Nothing in the API noticed:
the API does not depend on the worker.

These tests guard the import path that the container depends on.
"""

import hashlib
import importlib
import logging
import uuid
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from app.db.models import Document, ProcessingRun, User, Workspace
from app.processing import (
    ChunkConflict,
    FileMissing,
    InvalidPdf,
    ProcessingTimeout,
    StorageUnavailable,
)
from app.storage.keys import storage_key_for
from tests.fakes.publisher import RecordingTaskPublisher


def test_the_task_module_can_be_imported():
    """The module the Celery app includes must exist."""
    module = importlib.import_module("app.worker.tasks")
    assert module is not None


def test_the_celery_app_can_be_created():
    from app.worker.celery_app import celery_app

    assert celery_app.main == "axiom"


def test_the_celery_app_uses_the_configured_broker_and_backend():
    from app.config import settings
    from app.worker.celery_app import celery_app

    assert celery_app.conf.broker_url == settings.celery_broker_url
    assert celery_app.conf.result_backend == settings.celery_result_backend
    assert urlsplit(celery_app.conf.broker_url).path == "/1"
    assert urlsplit(celery_app.conf.result_backend).path == "/2"


def test_every_included_module_exists():
    """Every module in the include list must be importable.

    This is the check that would have caught the worker crash before it
    reached the container.
    """
    from app.worker.celery_app import celery_app

    for module_path in celery_app.conf.include:
        assert importlib.import_module(module_path) is not None


def test_the_reliability_settings_are_present():
    """The job-reliability contract from docs/phase-1/05-job-contract.md."""
    from app.config import settings
    from app.worker.celery_app import celery_app

    conf = celery_app.conf
    assert conf.task_acks_late is True
    assert conf.worker_prefetch_multiplier == 1
    assert conf.task_soft_time_limit == (
        settings.celery_task_soft_time_limit_seconds
    )
    assert conf.task_time_limit == settings.celery_task_time_limit_seconds
    assert conf.broker_transport_options["visibility_timeout"] == (
        settings.celery_visibility_timeout_seconds
    )


def test_the_celery_transport_is_bounded_and_retries_briefly():
    """A Redis outage must not stall an API request indefinitely."""
    from app.worker.celery_app import celery_app

    conf = celery_app.conf
    assert conf.broker_connection_retry_on_startup is True
    assert conf.broker_transport_options["socket_connect_timeout"] == 3
    assert conf.broker_transport_options["socket_timeout"] == 3
    assert conf.task_publish_retry is True
    assert conf.task_publish_retry_policy == {
        "max_retries": 2,
        "interval_start": 0,
        "interval_step": 1,
        "interval_max": 1,
    }


def test_the_celery_serialisation_and_clock_settings_are_explicit():
    from app.worker.celery_app import celery_app

    conf = celery_app.conf
    assert conf.task_ignore_result is True
    assert conf.task_serializer == "json"
    assert conf.result_serializer == "json"
    assert conf.accept_content == ["json"]
    assert conf.timezone == "UTC"
    assert conf.enable_utc is True


def test_worker_process_signals_dispose_the_inherited_engine(monkeypatch):
    """Prefork children must not reuse the engine's inherited sockets."""
    from celery.signals import worker_process_init, worker_process_shutdown

    from app.db import session

    disposed = []
    monkeypatch.setattr(
        session, "dispose_engine", lambda: disposed.append(True)
    )

    worker_process_init.send(sender=None)
    worker_process_shutdown.send(sender=None)

    assert disposed == [True, True]


@pytest.mark.parametrize(
    "module_path", ["app.worker.celery_app", "app.worker.tasks"]
)
def test_the_worker_modules_import_cleanly(module_path):
    """Importing must not require a reachable broker."""
    assert importlib.import_module(module_path) is not None


# ---------------------------------------------------------------------------
# Task wrapper: fixtures and helpers
# ---------------------------------------------------------------------------

PDF_BYTES = b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\n%%EOF\n"


class _FixedRandom:
    def __init__(self, value: float = 0.0) -> None:
        self.value = value

    def uniform(self, low: float, high: float) -> float:
        return self.value


class _RaisingProcessor:
    """A processor that records its input and raises a fixed error."""

    def __init__(self, error: Exception) -> None:
        self.error = error
        self.calls: list[tuple] = []

    def process(self, snapshot, storage):
        self.calls.append((snapshot, storage))
        raise self.error


def _runs(session, document_id: uuid.UUID) -> list[ProcessingRun]:
    return list(
        session.execute(
            select(ProcessingRun)
            .where(ProcessingRun.document_id == document_id)
            .order_by(ProcessingRun.attempt_no)
        ).scalars()
    )


def _make_document(
    db_session,
    storage,
    *,
    status: str = "queued",
    processing_attempts: int = 0,
) -> Document:
    workspace = Workspace(name="ws-%s" % uuid.uuid4().hex)
    user = User(
        email="task-%s@example.com" % uuid.uuid4().hex,
        password_hash="not-used-by-the-worker",
    )
    db_session.add_all([workspace, user])
    db_session.flush()

    document_id = uuid.uuid4()
    storage_key = storage_key_for(document_id)
    storage.save(storage_key, PDF_BYTES)
    document = Document(
        id=document_id,
        workspace_id=workspace.id,
        uploaded_by=user.id,
        original_filename="report.pdf",
        storage_key=storage_key,
        mime_type="application/pdf",
        size_bytes=len(PDF_BYTES),
        checksum=hashlib.sha256(PDF_BYTES).hexdigest(),
        status=status,
        processing_attempts=processing_attempts,
    )
    db_session.add(document)
    db_session.commit()
    return document


def _run_task(document_id: str, *, task_id: str = "test-task") -> str:
    from app.worker import tasks

    tasks.process_document.push_request(id=task_id)
    try:
        return tasks.process_document.run(document_id)
    finally:
        tasks.process_document.pop_request()


@pytest.fixture
def task_runtime(db_session, memory_storage, monkeypatch):
    """Wire the task to the test connection, memory storage, and a fake."""
    from app.db.session import SessionLocal
    from app.worker import tasks

    connection = db_session.get_bind()

    def session_factory():
        return SessionLocal(
            bind=connection,
            join_transaction_mode="create_savepoint",
        )

    publisher = RecordingTaskPublisher()
    monkeypatch.setattr(tasks, "SessionLocal", session_factory)
    monkeypatch.setattr(tasks, "get_storage", lambda: memory_storage)
    monkeypatch.setattr(tasks, "get_task_publisher", lambda: publisher)
    monkeypatch.setattr(tasks, "_RNG", _FixedRandom(0))

    return SimpleNamespace(
        tasks=tasks,
        publisher=publisher,
        storage=memory_storage,
    )


# ---------------------------------------------------------------------------
# Task registration and the happy path
# ---------------------------------------------------------------------------

def test_the_process_task_is_registered_under_its_stable_name():
    from app.worker import tasks
    from app.worker.celery_app import celery_app

    assert tasks.PROCESS_DOCUMENT_TASK_NAME == "documents.process"
    assert tasks.SWEEP_DOCUMENTS_TASK_NAME == "documents.sweep"
    assert tasks.process_document.name == "documents.process"
    assert tasks.process_document.ignore_result is True
    assert (
        celery_app.tasks["documents.process"].name == "documents.process"
    )


def test_a_task_drives_claim_process_commit_and_indexes(
    db_session,
    task_runtime,
    caplog,
):
    document = _make_document(db_session, task_runtime.storage)

    with caplog.at_level(logging.INFO, logger="axiom.worker.tasks"):
        result = _run_task(str(document.id), task_id="task-success")

    assert result == "indexed"

    db_session.refresh(document)
    assert document.status == "indexed"
    assert document.processing_attempts == 1
    assert document.indexed_at is not None
    assert document.error_code is None
    assert document.retryable is False

    runs = _runs(db_session, document.id)
    assert len(runs) == 1
    assert runs[0].stage == "indexed"
    assert runs[0].finished_at is not None

    assert task_runtime.publisher.calls == []

    task_logs = [
        record.getMessage()
        for record in caplog.records
        if record.name == "axiom.worker.tasks"
    ]
    assert any(
        "document.indexed" in message
        and str(document.id) in message
        and "attempt=1" in message
        and "outcome=indexed" in message
        and "duration_ms=" in message
        and "task_id=task-success" in message
        for message in task_logs
    )

    # No filename, storage key, or host path may reach the task log.
    assert "report.pdf" not in caplog.text
    assert document.storage_key not in caplog.text


def test_duplicate_delivery_is_a_noop(db_session, task_runtime):
    document = _make_document(db_session, task_runtime.storage)

    assert _run_task(str(document.id)) == "indexed"
    assert _run_task(str(document.id)) == "noop"

    db_session.refresh(document)
    assert document.status == "indexed"
    assert document.processing_attempts == 1

    runs = _runs(db_session, document.id)
    assert len(runs) == 1
    assert runs[0].stage == "indexed"
    assert task_runtime.publisher.calls == []


def test_a_deleted_document_is_a_noop(db_session, task_runtime):
    document = _make_document(db_session, task_runtime.storage)
    document_id = document.id

    db_session.delete(document)
    db_session.commit()

    assert _run_task(str(document_id)) == "noop"

    assert (
        db_session.execute(
            select(Document).where(Document.id == document_id)
        ).scalar_one_or_none()
        is None
    )
    assert _runs(db_session, document_id) == []


def test_an_invalid_payload_never_opens_a_session(
    task_runtime,
    monkeypatch,
    caplog,
):
    session_calls = []

    def forbidden_session():
        session_calls.append(True)
        raise AssertionError("SessionLocal must not be called")

    monkeypatch.setattr(
        task_runtime.tasks,
        "SessionLocal",
        forbidden_session,
    )

    with caplog.at_level(logging.WARNING, logger="axiom.worker.tasks"):
        result = _run_task("not-a-uuid", task_id="task-invalid")

    assert result == "invalid"
    assert session_calls == []

    task_logs = [
        record.getMessage()
        for record in caplog.records
        if record.name == "axiom.worker.tasks"
    ]
    assert any(
        "document.invalid_payload" in message
        and "attempt=0" in message
        and "outcome=invalid" in message
        and "duration_ms=" in message
        and "task_id=task-invalid" in message
        for message in task_logs
    )


# ---------------------------------------------------------------------------
# Failure classification through the task
# ---------------------------------------------------------------------------

def test_a_soft_timeout_becomes_a_retrying_document(
    db_session,
    task_runtime,
    monkeypatch,
):
    document = _make_document(db_session, task_runtime.storage)
    processor = _RaisingProcessor(SoftTimeLimitExceeded())
    monkeypatch.setattr(
        task_runtime.tasks,
        "get_processor",
        lambda: processor,
    )

    assert _run_task(str(document.id)) == "retrying"

    db_session.refresh(document)
    assert document.status == "retrying"
    assert document.processing_attempts == 1
    assert document.error_code == "PROCESSING_TIMEOUT"
    assert document.error_message == ProcessingTimeout.safe_message
    assert document.retryable is True

    runs = _runs(db_session, document.id)
    assert len(runs) == 1
    assert runs[0].stage == "retrying"
    assert runs[0].finished_at is not None
    assert task_runtime.publisher.calls == [(document.id, 30)]

    snapshot, storage = processor.calls[0]
    assert snapshot.id == document.id
    assert snapshot.workspace_id == document.workspace_id
    assert snapshot.storage_key == document.storage_key
    assert snapshot.checksum == document.checksum
    assert snapshot.size_bytes == document.size_bytes
    assert snapshot.mime_type == document.mime_type
    assert storage is task_runtime.storage


def test_a_transient_failure_is_published_as_a_fresh_task(
    db_session,
    task_runtime,
    monkeypatch,
    caplog,
):
    document = _make_document(db_session, task_runtime.storage)
    processor = _RaisingProcessor(StorageUnavailable())
    monkeypatch.setattr(
        task_runtime.tasks,
        "get_processor",
        lambda: processor,
    )

    with caplog.at_level(logging.INFO, logger="axiom.worker.tasks"):
        assert _run_task(str(document.id)) == "retrying"

    db_session.refresh(document)
    assert document.status == "retrying"
    assert document.error_code == "STORAGE_UNAVAILABLE"
    assert document.error_message == StorageUnavailable.safe_message
    assert document.retryable is True

    assert task_runtime.publisher.calls == [(document.id, 30)]

    task_logs = [
        record.getMessage()
        for record in caplog.records
        if record.name == "axiom.worker.tasks"
    ]
    assert any(
        "document.retrying" in message
        and "outcome=retrying" in message
        and "countdown=30" in message
        and "published=True" in message
        for message in task_logs
    )


@pytest.mark.parametrize(
    ("error", "expected_code", "expected_retryable"),
    [
        (InvalidPdf(), "INVALID_PDF", False),
        (FileMissing(), "FILE_MISSING", True),
    ],
    ids=["permanent-final", "permanent-fixable"],
)
def test_permanent_failures_are_not_published(
    db_session,
    task_runtime,
    monkeypatch,
    error,
    expected_code,
    expected_retryable,
):
    document = _make_document(db_session, task_runtime.storage)
    monkeypatch.setattr(
        task_runtime.tasks,
        "get_processor",
        lambda: _RaisingProcessor(error),
    )

    assert _run_task(str(document.id)) == "failed"

    db_session.refresh(document)
    assert document.status == "failed"
    assert document.error_code == expected_code
    assert document.retryable is expected_retryable

    runs = _runs(db_session, document.id)
    assert len(runs) == 1
    assert runs[0].stage == "failed"
    assert runs[0].finished_at is not None
    assert task_runtime.publisher.calls == []


def test_a_commit_time_processing_error_is_classified(
    db_session,
    task_runtime,
    monkeypatch,
):
    document = _make_document(db_session, task_runtime.storage)

    def raise_conflict(*args, **kwargs):
        raise ChunkConflict()

    monkeypatch.setattr(
        task_runtime.tasks,
        "commit_success",
        raise_conflict,
    )

    assert _run_task(str(document.id)) == "failed"

    db_session.refresh(document)
    assert document.status == "failed"
    assert document.error_code == "CHUNK_CONFLICT"
    assert document.retryable is False

    runs = _runs(db_session, document.id)
    assert len(runs) == 1
    assert runs[0].stage == "failed"
    assert runs[0].finished_at is not None
    assert task_runtime.publisher.calls == []


def test_an_unexpected_processor_error_becomes_internal_error(
    db_session,
    task_runtime,
    monkeypatch,
    caplog,
):
    document = _make_document(db_session, task_runtime.storage)
    processor = _RaisingProcessor(
        RuntimeError("C:\\private\\report.pdf could not be parsed")
    )
    monkeypatch.setattr(
        task_runtime.tasks,
        "get_processor",
        lambda: processor,
    )

    with caplog.at_level(logging.WARNING, logger="axiom.worker.tasks"):
        assert _run_task(str(document.id)) == "failed"

    db_session.refresh(document)
    assert document.status == "failed"
    assert document.error_code == "INTERNAL_ERROR"
    assert document.error_message == "Document processing failed unexpectedly."
    assert document.retryable is False

    runs = _runs(db_session, document.id)
    assert len(runs) == 1
    assert runs[0].stage == "failed"
    assert runs[0].finished_at is not None
    assert task_runtime.publisher.calls == []

    task_logs = [
        record.getMessage()
        for record in caplog.records
        if record.name == "axiom.worker.tasks"
    ]
    assert any(
        "document.unexpected_error" in message
        and "error_type=RuntimeError" in message
        for message in task_logs
    )
    assert "C:\\private" not in caplog.text
    assert "report.pdf" not in caplog.text


def test_a_database_error_is_reraised_without_failure_writes(
    db_session,
    task_runtime,
    monkeypatch,
    caplog,
):
    document = _make_document(db_session, task_runtime.storage)
    error = OperationalError(
        "SELECT 1",
        {},
        RuntimeError("connection lost"),
    )
    monkeypatch.setattr(
        task_runtime.tasks,
        "get_processor",
        lambda: _RaisingProcessor(error),
    )

    with caplog.at_level(logging.ERROR, logger="axiom.worker.tasks"):
        with pytest.raises(OperationalError):
            _run_task(str(document.id), task_id="task-db-error")

    db_session.refresh(document)
    assert document.status == "processing"
    assert document.processing_attempts == 1
    assert document.error_code is None
    assert document.error_message is None
    assert document.retryable is False

    runs = _runs(db_session, document.id)
    assert len(runs) == 1
    assert runs[0].stage == "claimed"
    assert runs[0].finished_at is None
    assert task_runtime.publisher.calls == []

    task_logs = [
        record.getMessage()
        for record in caplog.records
        if record.name == "axiom.worker.tasks"
    ]
    assert any(
        "document.db_error" in message
        and "outcome=db_error" in message
        and "error_type=OperationalError" in message
        and "duration_ms=" in message
        and "task_id=task-db-error" in message
        for message in task_logs
    )
    assert not any("document.failed" in message for message in task_logs)


def test_a_publisher_failure_leaves_the_row_retrying(
    db_session,
    task_runtime,
    monkeypatch,
    caplog,
):
    document = _make_document(db_session, task_runtime.storage)
    processor = _RaisingProcessor(StorageUnavailable())
    monkeypatch.setattr(
        task_runtime.tasks,
        "get_processor",
        lambda: processor,
    )
    task_runtime.publisher.result = False

    with caplog.at_level(logging.INFO, logger="axiom.worker.tasks"):
        assert _run_task(str(document.id)) == "retrying"

    db_session.refresh(document)
    assert document.status == "retrying"
    assert document.error_code == "STORAGE_UNAVAILABLE"
    assert document.retryable is True

    assert task_runtime.publisher.calls == [(document.id, 30)]

    task_logs = [
        record.getMessage()
        for record in caplog.records
        if record.name == "axiom.worker.tasks"
    ]
    assert any(
        "document.retrying" in message
        and "outcome=retrying" in message
        and "published=False" in message
        for message in task_logs
    )
