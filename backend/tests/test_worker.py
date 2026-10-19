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

import pytest


def test_the_task_module_can_be_imported():
    """The module the Celery app includes must exist."""
    module = importlib.import_module("app.worker.tasks")
    assert module is not None


def test_the_celery_app_can_be_created():
    from app.worker.celery_app import celery_app

    assert celery_app.main == "axiom"


def test_the_celery_app_uses_the_configured_broker():
    from app.config import settings
    from app.worker.celery_app import celery_app

    assert celery_app.conf.broker_url == settings.redis_url
    assert celery_app.conf.result_backend == settings.redis_url


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
    from app.worker.celery_app import celery_app

    conf = celery_app.conf
    assert conf.task_acks_late is True
    assert conf.worker_prefetch_multiplier == 1
    assert conf.task_soft_time_limit == 480
    assert conf.task_time_limit == 600
    assert conf.broker_transport_options["visibility_timeout"] == 1800


@pytest.mark.parametrize(
    "module_path", ["app.worker.celery_app", "app.worker.tasks"]
)
def test_the_worker_modules_import_cleanly(module_path):
    """Importing must not require a reachable broker."""
    assert importlib.import_module(module_path) is not None
