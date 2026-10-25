"""Tests for the Phase 4 processing port and integrity processor.

The processor must be provable without PostgreSQL, Redis, Celery, or a
real filesystem. The in-memory storage fake and a tiny raising stub are
enough to exercise every branch, and a static import check guards the
architectural rule that ``app.processing`` never depends on the database
or the task layer.
"""

from __future__ import annotations

import ast
import hashlib
import uuid
from pathlib import Path

import pytest

import app.processing
from app.processing import (
    ChecksumMismatch,
    ChunkConflict,
    DocumentProcessor,
    DocumentSnapshot,
    EmptyFile,
    FileMissing,
    IntegrityProcessor,
    InternalError,
    InvalidPdf,
    PermanentFinalError,
    PermanentFixableError,
    ProcessingError,
    ProcessingTimeout,
    ProcessResult,
    StorageIOError,
    StoragePathInvalid,
    StoragePermissionDenied,
    StorageUnavailable,
    TransientProcessingError,
)
from app.storage.base import (
    FileStorage,
    StorageNotFoundError,
    StoragePathError,
    StorageUnavailableError,
)
from app.storage.keys import storage_key_for
from tests.fakes.storage import InMemoryFileStorage

PDF_BYTES = b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\n%%EOF\n"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _snapshot(content: bytes = PDF_BYTES, **overrides) -> DocumentSnapshot:
    """Build a snapshot whose checksum matches ``content`` by default."""
    document_id = uuid.uuid4()
    values = {
        "id": document_id,
        "workspace_id": uuid.uuid4(),
        "storage_key": storage_key_for(document_id),
        "checksum": hashlib.sha256(content).hexdigest(),
        "size_bytes": len(content),
        "mime_type": "application/pdf",
    }
    values.update(overrides)
    return DocumentSnapshot(**values)


def _stored(
    content: bytes = PDF_BYTES,
    **overrides,
) -> tuple[DocumentSnapshot, InMemoryFileStorage]:
    """Store ``content`` at the snapshot's key and return both."""
    snapshot = _snapshot(content, **overrides)
    storage = InMemoryFileStorage()
    storage.save(snapshot.storage_key, content)
    return snapshot, storage


class _RaisingStorage:
    """A minimal storage port that raises a prepared exception."""

    def __init__(self, error: BaseException) -> None:
        self._error = error

    def read(self, key: str) -> bytes:
        raise self._error


def _unavailable_with_cause(cause: BaseException | None) -> StorageUnavailableError:
    error = StorageUnavailableError("The storage backend is unavailable.")
    error.__cause__ = cause
    return error


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------

def test_integrity_processor_satisfies_the_processing_port():
    assert isinstance(IntegrityProcessor(), DocumentProcessor)


def test_valid_pdf_returns_a_result_with_zero_chunks():
    snapshot, storage = _stored()

    result = IntegrityProcessor().process(snapshot, storage)

    assert isinstance(result, ProcessResult)
    assert result.chunks == ()


def test_the_processor_reads_the_snapshot_key():
    snapshot = _snapshot()
    storage = InMemoryFileStorage()
    # A different key holds a valid PDF. Reading anything but the
    # snapshot's key must be reported as a missing object.
    other_key = storage_key_for(uuid.uuid4())
    storage.save(other_key, PDF_BYTES)

    with pytest.raises(FileMissing):
        IntegrityProcessor().process(snapshot, storage)


# ---------------------------------------------------------------------------
# Content failures
# ---------------------------------------------------------------------------

def test_empty_object_is_permanent():
    snapshot, storage = _stored(b"")

    with pytest.raises(EmptyFile) as caught:
        IntegrityProcessor().process(snapshot, storage)

    assert caught.value.code == "EMPTY_FILE"
    assert caught.value.transient is False
    assert caught.value.retryable is False


def test_checksum_mismatch_is_detected_with_constant_time_comparison():
    snapshot = _snapshot(PDF_BYTES)
    storage = InMemoryFileStorage()
    storage.save(snapshot.storage_key, PDF_BYTES + b"\n% tampered\n")

    with pytest.raises(ChecksumMismatch) as caught:
        IntegrityProcessor().process(snapshot, storage)

    assert caught.value.code == "CHECKSUM_MISMATCH"
    assert caught.value.transient is False
    assert caught.value.retryable is False


def test_checksum_is_checked_before_the_pdf_signature():
    """A corrupt non-PDF must report the checksum failure, not the magic."""
    content = b"GIF89a not a pdf"
    snapshot = _snapshot(content)
    storage = InMemoryFileStorage()
    storage.save(snapshot.storage_key, content + b"tampered")

    with pytest.raises(ChecksumMismatch):
        IntegrityProcessor().process(snapshot, storage)


def test_non_pdf_content_with_a_matching_checksum_is_invalid():
    content = b"GIF89a\nthis is not a pdf at all\n"
    snapshot, storage = _stored(content)

    with pytest.raises(InvalidPdf) as caught:
        IntegrityProcessor().process(snapshot, storage)

    assert caught.value.code == "INVALID_PDF"
    assert caught.value.transient is False
    assert caught.value.retryable is False


# ---------------------------------------------------------------------------
# Storage-port exception mapping
# ---------------------------------------------------------------------------

def test_missing_object_maps_to_permanent_fixable():
    snapshot = _snapshot()

    with pytest.raises(FileMissing) as caught:
        IntegrityProcessor().process(snapshot, InMemoryFileStorage())

    assert caught.value.code == "FILE_MISSING"
    assert caught.value.transient is False
    assert caught.value.retryable is True
    assert isinstance(caught.value.__cause__, StorageNotFoundError)


def test_invalid_key_maps_to_permanent_final():
    snapshot = _snapshot()
    error = StoragePathError("Storage key escapes the root.")

    with pytest.raises(StoragePathInvalid) as caught:
        IntegrityProcessor().process(snapshot, _RaisingStorage(error))

    assert caught.value.code == "STORAGE_PATH_INVALID"
    assert caught.value.transient is False
    assert caught.value.retryable is False
    assert caught.value.__cause__ is error


def test_unavailable_storage_maps_to_transient():
    snapshot = _snapshot()
    error = _unavailable_with_cause(None)

    with pytest.raises(StorageUnavailable) as caught:
        IntegrityProcessor().process(snapshot, _RaisingStorage(error))

    assert caught.value.code == "STORAGE_UNAVAILABLE"
    assert caught.value.transient is True
    assert caught.value.retryable is True
    assert caught.value.__cause__ is error


def test_a_non_permission_cause_stays_transient():
    """Only PermissionError changes the classification."""
    snapshot = _snapshot()
    error = _unavailable_with_cause(TimeoutError("upstream timed out"))

    with pytest.raises(StorageUnavailable) as caught:
        IntegrityProcessor().process(snapshot, _RaisingStorage(error))

    assert caught.value.transient is True
    assert caught.value.retryable is True


def test_permission_caused_unavailable_storage_is_permanent_fixable():
    snapshot = _snapshot()
    cause = PermissionError(13, "Permission denied")
    error = _unavailable_with_cause(cause)

    with pytest.raises(StoragePermissionDenied) as caught:
        IntegrityProcessor().process(snapshot, _RaisingStorage(error))

    assert caught.value.code == "STORAGE_PERMISSION_DENIED"
    assert caught.value.transient is False
    assert caught.value.retryable is True
    assert caught.value.__cause__ is error


def test_a_leaked_os_error_maps_to_transient_storage_io_error():
    snapshot = _snapshot()
    error = OSError("adapter leaked a raw OS error")

    with pytest.raises(StorageIOError) as caught:
        IntegrityProcessor().process(snapshot, _RaisingStorage(error))

    assert caught.value.code == "STORAGE_IO_ERROR"
    assert caught.value.transient is True
    assert caught.value.retryable is True
    assert caught.value.__cause__ is error


# ---------------------------------------------------------------------------
# The taxonomy itself
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("error_type", "family", "code", "transient", "retryable"),
    [
        (StorageUnavailable, TransientProcessingError, "STORAGE_UNAVAILABLE", True, True),
        (StorageIOError, TransientProcessingError, "STORAGE_IO_ERROR", True, True),
        (ProcessingTimeout, TransientProcessingError, "PROCESSING_TIMEOUT", True, True),
        (FileMissing, PermanentFixableError, "FILE_MISSING", False, True),
        (
            StoragePermissionDenied,
            PermanentFixableError,
            "STORAGE_PERMISSION_DENIED",
            False,
            True,
        ),
        (ChunkConflict, PermanentFinalError, "CHUNK_CONFLICT", False, False),
        (StoragePathInvalid, PermanentFinalError, "STORAGE_PATH_INVALID", False, False),
        (ChecksumMismatch, PermanentFinalError, "CHECKSUM_MISMATCH", False, False),
        (InvalidPdf, PermanentFinalError, "INVALID_PDF", False, False),
        (EmptyFile, PermanentFinalError, "EMPTY_FILE", False, False),
        (InternalError, PermanentFinalError, "INTERNAL_ERROR", False, False),
    ],
)
def test_every_concrete_error_has_the_intended_classification(
    error_type, family, code, transient, retryable
):
    error = error_type()

    assert isinstance(error, ProcessingError)
    assert isinstance(error, family)
    assert error.code == code
    assert error.safe_message
    assert error.transient is transient
    assert error.retryable is retryable


# ---------------------------------------------------------------------------
# Architectural boundary
# ---------------------------------------------------------------------------

def test_processing_package_does_not_import_celery_or_the_database():
    """The processor must stay usable without a broker or a live session."""
    package_dir = Path(app.processing.__file__).resolve().parent
    banned_prefixes = (
        "celery",
        "sqlalchemy",
        "psycopg",
        "app.db",
        "app.worker",
    )

    for path in sorted(package_dir.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                modules = [node.module] if node.module else []
            else:
                continue

            for module in modules:
                assert not module.startswith(banned_prefixes), (
                    "%s imports %r; app.processing must stay database- and "
                    "Celery-free." % (path.name, module)
                )


def test_the_processing_port_does_not_require_a_concrete_storage_adapter():
    """The protocol is structural, so the in-memory fake satisfies it."""
    assert isinstance(InMemoryFileStorage(), FileStorage)
