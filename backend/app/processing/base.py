"""The document-processing port.

A processor is a pure transformation: given a persisted document snapshot
and the storage port, it reads the stored bytes and returns the chunks the
pipeline should write. It does not open database sessions, claim documents,
start or close ``processing_runs`` rows, touch Celery, or decide how a
failure is retried. Those responsibilities belong to the task/pipeline
layer.

Keeping the port this small has two consequences that matter in Phase 4:

* The same processor can be tested without PostgreSQL, Redis, or a broker.
* The task layer can replace the integrity-only processor in Phase 5
  without changing any lifecycle code.

``NewChunk`` deliberately carries only the columns a processor can know.
The pipeline owns identity and placement: it assigns ``id``,
``document_id``, ``workspace_id``, ``chunk_index``, and ``created_at``
when it inserts the returned chunks.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.storage.base import FileStorage

__all__ = [
    "DocumentProcessor",
    "DocumentSnapshot",
    "NewChunk",
    "ProcessResult",
]


@dataclass(frozen=True)
class NewChunk:
    """One chunk a processor wants the pipeline to persist.

    The fields mirror the processor-owned columns of ``chunks``. The
    pipeline fills in the document identity and the zero-based
    ``chunk_index``.
    """

    content: str
    page_number: int | None
    source_type: str
    chunk_version: str
    embedding_model: str
    embedding_dim: int
    embedding: tuple[float, ...]


@dataclass(frozen=True)
class DocumentSnapshot:
    """The persisted facts a processor needs about one document.

    A snapshot is passed instead of a SQLAlchemy ``Document`` so the
    processor cannot accidentally lazy-load relationships, mutate the row,
    or depend on a live session.
    """

    id: uuid.UUID
    workspace_id: uuid.UUID
    storage_key: str
    checksum: str
    size_bytes: int
    mime_type: str


@dataclass(frozen=True)
class ProcessResult:
    """Everything a processor produced for one document.

    Phase 4's integrity processor returns no chunks. Phase 5's extractor
    will return the chunk set; the pipeline still owns the run stage,
    transaction, and guarded commit.
    """

    chunks: tuple[NewChunk, ...] = ()


@runtime_checkable
class DocumentProcessor(Protocol):
    """The processing contract the worker depends on.

    Implementations raise :class:`app.processing.errors.ProcessingError`
    subclasses rather than raw storage or OS exceptions. That lets the
    task layer apply one classification policy without knowing which
    processor ran.
    """

    def process(
        self,
        snapshot: DocumentSnapshot,
        storage: FileStorage,
    ) -> ProcessResult:
        """Read ``snapshot`` from ``storage`` and return its chunks."""
