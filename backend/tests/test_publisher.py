"""Tests for the task-publication boundary.

Publication is deliberately small: convert the document id to the Celery
payload, publish the process task, and turn any normal publish failure
into ``False``. These tests replace the lazily imported task with a
recording stub, so they never contact Redis or a worker.
"""

from __future__ import annotations

import logging
import uuid

from app.worker import tasks
from app.worker.publisher import (
    CeleryTaskPublisher,
    TaskPublisher,
    get_task_publisher,
)
from tests.fakes.publisher import RecordingTaskPublisher


class _RecordingTask:
    """Stand-in for the Celery task object returned by the lazy import."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def apply_async(self, **kwargs) -> None:
        self.calls.append(kwargs)


class _FailingTask:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def apply_async(self, **kwargs) -> None:
        raise self.error


def test_enqueue_publishes_only_the_document_id(monkeypatch):
    task = _RecordingTask()
    monkeypatch.setattr(tasks, "process_document", task, raising=False)
    document_id = uuid.uuid4()

    published = CeleryTaskPublisher().enqueue_document(
        document_id,
        countdown=42,
    )

    assert published is True
    assert task.calls == [
        {
            "args": [str(document_id)],
            "countdown": 42,
            "ignore_result": True,
        }
    ]


def test_enqueue_defaults_to_no_countdown(monkeypatch):
    task = _RecordingTask()
    monkeypatch.setattr(tasks, "process_document", task, raising=False)
    document_id = uuid.uuid4()

    assert CeleryTaskPublisher().enqueue_document(document_id) is True

    assert task.calls == [
        {
            "args": [str(document_id)],
            "countdown": 0,
            "ignore_result": True,
        }
    ]


def test_a_publish_failure_is_logged_and_returns_false(
    monkeypatch,
    caplog,
):
    monkeypatch.setattr(
        tasks,
        "process_document",
        _FailingTask(RuntimeError("broker unavailable")),
        raising=False,
    )
    document_id = uuid.uuid4()

    with caplog.at_level(
        logging.WARNING,
        logger="axiom.worker.publisher",
    ):
        published = CeleryTaskPublisher().enqueue_document(
            document_id,
            countdown=7,
        )

    assert published is False

    enqueue_logs = [
        record.getMessage()
        for record in caplog.records
        if record.name == "axiom.worker.publisher"
        and "document.enqueue" in record.getMessage()
    ]
    assert len(enqueue_logs) == 1
    assert "published=false" in enqueue_logs[0]
    assert str(document_id) in enqueue_logs[0]
    assert "countdown=7" in enqueue_logs[0]


def test_a_missing_task_import_is_a_publish_failure(monkeypatch, caplog):
    # The publisher imports the task lazily so importing the API never
    # initialises Celery. A broken import must still be a False result.
    monkeypatch.delattr(tasks, "process_document", raising=False)

    with caplog.at_level(
        logging.WARNING,
        logger="axiom.worker.publisher",
    ):
        published = CeleryTaskPublisher().enqueue_document(uuid.uuid4())

    assert published is False
    assert any(
        "document.enqueue" in record.getMessage()
        and "published=false" in record.getMessage()
        for record in caplog.records
    )


def test_the_task_publisher_is_a_process_wide_singleton():
    publisher = get_task_publisher()

    assert publisher is get_task_publisher()
    assert isinstance(publisher, CeleryTaskPublisher)
    assert isinstance(publisher, TaskPublisher)


def test_the_recording_fake_captures_calls_and_controls_the_result():
    document_id = uuid.uuid4()
    publisher = RecordingTaskPublisher()

    assert publisher.enqueue_document(document_id, countdown=12) is True
    assert publisher.calls == [(document_id, 12)]

    failing = RecordingTaskPublisher(result=False)
    assert failing.enqueue_document(document_id) is False
    assert failing.calls == [(document_id, 0)]

    assert isinstance(publisher, TaskPublisher)
    assert isinstance(failing, TaskPublisher)
