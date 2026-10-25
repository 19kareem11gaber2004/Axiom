"""Phase 4 integrity-only document processor.

The upload path has already validated the PDF signature and computed the
SHA-256 checksum of the bytes it stored. This processor re-establishes
that the object the worker is about to read is still the object that was
uploaded. It deliberately does not extract text or build embeddings:
Phase 4 proves the queue, claim, commit, and retry lifecycle end to end,
and Phase 5 replaces this processor with the real pipeline.

The only I/O is ``FileStorage.read``. Raw storage and OS exceptions are
translated into the taxonomy in :mod:`app.processing.errors` here, at the
boundary, so the task layer never has to know about adapter internals.
"""

from __future__ import annotations

import hashlib
import hmac

from app.processing.base import DocumentSnapshot, ProcessResult
from app.processing.errors import (
    ChecksumMismatch,
    EmptyFile,
    FileMissing,
    InvalidPdf,
    StorageIOError,
    StoragePathInvalid,
    StoragePermissionDenied,
    StorageUnavailable,
)
from app.storage.base import (
    FileStorage,
    StorageNotFoundError,
    StoragePathError,
    StorageUnavailableError,
)

__all__ = ["PDF_MAGIC", "IntegrityProcessor"]

# Every PDF starts with this signature. The same constant is checked at
# upload time in app.ingestion.validation; the worker re-checks it because
# storage may have been corrupted or tampered with after the upload.
PDF_MAGIC = b"%PDF-"


class IntegrityProcessor:
    """Verify that a stored document is still the uploaded PDF.

    Returns an empty chunk tuple. ``indexed`` therefore means "the current
    processor completed", not "text and embeddings exist"; Phase 5 makes
    the latter true.
    """

    def process(
        self,
        snapshot: DocumentSnapshot,
        storage: FileStorage,
    ) -> ProcessResult:
        """Read, verify, and return no chunks.

        Checks run in a fixed order so the failure code is deterministic:
        empty object, checksum mismatch, then missing PDF signature.
        """
        content = self._read(snapshot.storage_key, storage)

        if not content:
            raise EmptyFile()

        digest = hashlib.sha256(content).hexdigest()
        if not hmac.compare_digest(digest, snapshot.checksum):
            raise ChecksumMismatch()

        if not content.startswith(PDF_MAGIC):
            raise InvalidPdf()

        return ProcessResult(chunks=())

    @staticmethod
    def _read(key: str, storage: FileStorage) -> bytes:
        """Read ``key`` and translate every adapter failure.

        The storage port is the only layer allowed to know about the
        filesystem or an object store. Mapping its exceptions here, rather
        than catching ``OSError`` around a raw ``open``, keeps the
        processor usable with any adapter.
        """
        try:
            return storage.read(key)
        except StorageNotFoundError as exc:
            raise FileMissing() from exc
        except StoragePathError as exc:
            raise StoragePathInvalid() from exc
        except StorageUnavailableError as exc:
            # The local adapter wraps every OSError in
            # StorageUnavailableError. A permission failure is not
            # transient in the retry sense: it will keep failing until an
            # operator fixes the file mode or mount, so it is reported as
            # permanent-fixable. The cause chain is the only signal the
            # port preserves.
            if isinstance(exc.__cause__, PermissionError):
                raise StoragePermissionDenied() from exc
            raise StorageUnavailable() from exc
        except OSError as exc:
            # An adapter that leaks a raw OS error violates the port, but
            # the worker must still classify it rather than crash the task.
            raise StorageIOError() from exc
