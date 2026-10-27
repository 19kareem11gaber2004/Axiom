"""Recording TaskPublisher used by API and worker tests.

The fake stores every publish and returns a configurable result. Tests
can therefore assert both the payload (document id and countdown) and the
recovery behavior after a failed publish without touching Redis.
"""

from __future__ import annotations

import uuid

__all__ = ["RecordingTaskPublisher"]


class RecordingTaskPublisher:
    """Capture publish calls and return ``result`` for every call."""

    def __init__(self, result: bool = True) -> None:
        self.result = result
        self.calls: list[tuple[uuid.UUID, float]] = []

    def enqueue_document(
        self,
        document_id: uuid.UUID,
        *,
        countdown: float = 0,
    ) -> bool:
        self.calls.append((document_id, countdown))
        return self.result
