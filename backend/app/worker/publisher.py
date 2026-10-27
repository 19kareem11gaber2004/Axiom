"""The task-publication boundary.

The API and worker depend on :class:`TaskPublisher`, not on Celery. That
keeps publication testable (a recording fake is enough), gives the API a
stable dependency, and ensures a broker outage is a normal ``False``
result rather than an exception escaping a request or a task.

The Celery adapter imports the task lazily. Task modules import the
pipeline, and the task wrapper will import this publisher for retries, so
a module-level import here would create a cycle. The lazy import also
means importing the API never initialises Celery.
"""

from __future__ import annotations

import logging
import uuid
from typing import Protocol, runtime_checkable

logger = logging.getLogger("axiom.worker.publisher")

__all__ = ["CeleryTaskPublisher", "TaskPublisher", "get_task_publisher"]


@runtime_checkable
class TaskPublisher(Protocol):
    """Publish one document-processing task."""

    def enqueue_document(
        self,
        document_id: uuid.UUID,
        *,
        countdown: float = 0,
    ) -> bool:
        """Publish ``document_id``; return False rather than raising."""


class CeleryTaskPublisher:
    """Publish document tasks through the configured Celery application."""

    def enqueue_document(
        self,
        document_id: uuid.UUID,
        *,
        countdown: float = 0,
    ) -> bool:
        """Send one ``documents.process`` task.

        Any failure - a missing task module, a serialisation error, a
        broker outage, or a Celery publish error - is logged and reported
        as ``False``. The database transition that preceded the publish is
        already committed, so the sweeper can recover a lost publish.
        """
        try:
            # Lazy: app.worker.tasks imports the pipeline, and the task
            # wrapper imports this module.
            from app.worker.tasks import process_document

            process_document.apply_async(
                args=[str(document_id)],
                countdown=countdown,
                ignore_result=True,
            )
        except Exception:
            logger.warning(
                "document.enqueue document_id=%s published=false "
                "countdown=%s",
                document_id,
                countdown,
                exc_info=True,
            )
            return False
        return True


_publisher = CeleryTaskPublisher()


def get_task_publisher() -> TaskPublisher:
    """Return the process-wide publisher used by FastAPI dependencies."""
    return _publisher
