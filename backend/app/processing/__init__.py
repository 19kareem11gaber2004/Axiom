"""Document processing: the port, its error taxonomy, and Phase 4's processor.

The package is intentionally independent of the database and Celery:

* :mod:`app.processing.base` defines the data contract and the protocol.
* :mod:`app.processing.errors` defines how a failure is classified.
* :mod:`app.processing.integrity` is the Phase 4 processor.

The task/pipeline layer imports these; they never import the task layer.
That direction is what keeps the processor testable without PostgreSQL or
a broker, and it is enforced by a test.
"""

from app.processing.base import (
    DocumentProcessor,
    DocumentSnapshot,
    NewChunk,
    ProcessResult,
)
from app.processing.errors import (
    ChecksumMismatch,
    ChunkConflict,
    EmptyFile,
    FileMissing,
    InternalError,
    InvalidPdf,
    PermanentFinalError,
    PermanentFixableError,
    ProcessingError,
    ProcessingTimeout,
    StorageIOError,
    StoragePathInvalid,
    StoragePermissionDenied,
    StorageUnavailable,
    TransientProcessingError,
)
from app.processing.integrity import IntegrityProcessor

__all__ = [
    "ChecksumMismatch",
    "ChunkConflict",
    "DocumentProcessor",
    "DocumentSnapshot",
    "EmptyFile",
    "FileMissing",
    "IntegrityProcessor",
    "InternalError",
    "InvalidPdf",
    "NewChunk",
    "PermanentFinalError",
    "PermanentFixableError",
    "ProcessingError",
    "ProcessingTimeout",
    "ProcessResult",
    "StorageIOError",
    "StoragePathInvalid",
    "StoragePermissionDenied",
    "StorageUnavailable",
    "TransientProcessingError",
]
