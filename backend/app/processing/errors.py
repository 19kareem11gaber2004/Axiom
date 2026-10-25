"""The processing error taxonomy.

Every processor failure is classified along two independent axes:

``transient``
    The same input may succeed later without any human intervention.
    The worker retries these with exponential backoff and moves the
    document to ``retrying`` while attempts remain.

``retryable``
    The manual ``POST /documents/{id}/retry`` endpoint may re-queue the
    document from ``failed``. This is persisted as ``documents.retryable``.

The combination yields three concrete families:

===========================  ==========  ===========
Family                       transient   retryable
===========================  ==========  ===========
``TransientProcessingError``  True        True
``PermanentFixableError``     False       True
``PermanentFinalError``       False       False
===========================  ==========  ===========

"Permanent-fixable" means the worker must not retry on its own, but an
operator can repair the condition (restore a missing file, correct storage
permissions) and the user can then retry. "Permanent-final" means neither
automatic nor manual retry can help: the bytes or the database row are
what they are.

Only ``safe_message`` may be shown to a client or persisted as
``documents.error_message``. Exception chaining (``raise ... from exc``)
preserves the internal cause for logs without leaking it through the API.
"""

from __future__ import annotations

__all__ = [
    "ChecksumMismatch",
    "ChunkConflict",
    "EmptyFile",
    "FileMissing",
    "InternalError",
    "InvalidPdf",
    "PermanentFinalError",
    "PermanentFixableError",
    "ProcessingError",
    "ProcessingTimeout",
    "StorageIOError",
    "StoragePathInvalid",
    "StoragePermissionDenied",
    "StorageUnavailable",
    "TransientProcessingError",
]


class ProcessingError(Exception):
    """Base class for every processor failure.

    Subclasses set ``code``, ``safe_message``, ``transient``, and
    ``retryable`` as class attributes. The constructor accepts an optional
    safe message for the rare case where a concrete failure needs more
    detail; it must never contain paths, user input, or provider output.
    """

    code: str = "PROCESSING_FAILED"
    safe_message: str = "Document processing failed."
    transient: bool = False
    retryable: bool = False

    def __init__(self, safe_message: str | None = None) -> None:
        message = (
            safe_message if safe_message is not None else type(self).safe_message
        )
        self.safe_message = message
        super().__init__(message)


class TransientProcessingError(ProcessingError):
    """A failure that automatic retry may resolve."""

    transient = True
    retryable = True


class PermanentFixableError(ProcessingError):
    """A failure that requires a repair, after which manual retry may work."""

    transient = False
    retryable = True


class PermanentFinalError(ProcessingError):
    """A failure that no retry can resolve."""

    transient = False
    retryable = False


class StorageUnavailable(TransientProcessingError):
    """The storage backend could not be reached."""

    code = "STORAGE_UNAVAILABLE"
    safe_message = "The storage backend is temporarily unavailable."


class StorageIOError(TransientProcessingError):
    """A leaked operating-system error interrupted the read."""

    code = "STORAGE_IO_ERROR"
    safe_message = "The stored file could not be read."


class ProcessingTimeout(TransientProcessingError):
    """The task exceeded its soft time limit.

    The integrity processor itself is fast; the task layer converts
    Celery's ``SoftTimeLimitExceeded`` into this error so ``processing/``
    never imports Celery.
    """

    code = "PROCESSING_TIMEOUT"
    safe_message = "Document processing exceeded its time limit."


class FileMissing(PermanentFixableError):
    """The document row exists but its storage object does not."""

    code = "FILE_MISSING"
    safe_message = "The stored file for this document is missing."


class StoragePermissionDenied(PermanentFixableError):
    """The storage backend denied access to the object."""

    code = "STORAGE_PERMISSION_DENIED"
    safe_message = "The storage backend denied access to the document."


class ChunkConflict(PermanentFixableError):
    """The chunk set could not be committed because of a conflict.

    A manual retry re-runs the guarded commit and normally succeeds; the
    worker does not retry automatically because a persistent conflict
    would otherwise consume every attempt.
    """

    code = "CHUNK_CONFLICT"
    safe_message = "The document's chunk set could not be committed."


class StoragePathInvalid(PermanentFinalError):
    """The stored key is malformed or escapes the storage root."""

    code = "STORAGE_PATH_INVALID"
    safe_message = "The document's storage location is invalid."


class ChecksumMismatch(PermanentFinalError):
    """The bytes at the storage key are not the bytes that were uploaded."""

    code = "CHECKSUM_MISMATCH"
    safe_message = "The stored file does not match the uploaded checksum."


class InvalidPdf(PermanentFinalError):
    """The stored object does not begin with the PDF signature."""

    code = "INVALID_PDF"
    safe_message = "The stored file is not a valid PDF."


class EmptyFile(PermanentFinalError):
    """The stored object contains no bytes."""

    code = "EMPTY_FILE"
    safe_message = "The stored file is empty."


class InternalError(PermanentFinalError):
    """The processor failed for a reason it does not understand.

    Retrying the same input cannot help; a code or deployment fix is
    required. The task layer may use this when it has nothing more
    specific to report.
    """

    code = "INTERNAL_ERROR"
    safe_message = "Document processing failed unexpectedly."
