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

import importlib
from urllib.parse import urlsplit

import pytest


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
