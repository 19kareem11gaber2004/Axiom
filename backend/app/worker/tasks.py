"""Task modules for the Axiom Celery worker.

This module exists so the Celery application has something to import.
``app.worker.celery_app`` lists it in ``include``, and Celery imports
every module in that list when the worker starts. Without the module the
worker refuses to start at all:

    ModuleNotFoundError: No module named 'app.worker.tasks'

That failure is silent in the sense that the container exits with code 1
and no worker ever connects to the broker, so no background job is ever
picked up.

Phase 4 adds the document-processing tasks here. They must be idempotent
(see the module docstring of app/worker/celery_app.py): the same task can
be delivered more than once.
"""
