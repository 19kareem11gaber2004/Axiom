"""Celery application for background task execution.

Reliability settings (per the job-reliability contract, docs/phase-1/05-job-contract.md):

- task_acks_late=True: a task is acknowledged only after it finishes, so a
  worker crash mid-task redelivers the task to another worker.
- worker_prefetch_multiplier=1: a worker reserves at most one task at a time,
  preventing task hoarding and straggler-head-of-line blocking.
- broker_transport_options visibility_timeout=1800s: must exceed the maximum
  task runtime (hard limit 600s) so a slow-but-live task is not redelivered
  while still running.
- task_soft_time_limit=480s / task_time_limit=600s: bounded runtime so no
  task runs forever.

IMPORTANT: Because Redis redelivers unacknowledged tasks after the visibility
timeout and because workers may crash mid-task, the same Celery task can be
delivered more than once. Future document-processing tasks MUST be idempotent:
repeating a task must not create duplicate chunks, duplicate final outputs, or
inconsistent document status. Use the database claim pattern
(UPDATE ... WHERE status IN (...) RETURNING) plus a guarded commit to achieve
this.
"""

from celery import Celery
from celery.signals import worker_process_init, worker_process_shutdown

from app.config import settings

celery_app = Celery(
    "axiom",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=["app.worker.tasks"],  # task modules are added in Phase 4
)

celery_app.conf.update(
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    # Task results are not used anywhere in the lifecycle. PostgreSQL is
    # the system of record; the Redis result database exists so Celery
    # never silently falls back to a different transport.
    task_ignore_result=True,
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    broker_connection_retry_on_startup=True,
    broker_transport_options={
        "visibility_timeout": settings.celery_visibility_timeout_seconds,
        # Bound both establishing the broker connection and waiting for
        # a response, so a Redis outage cannot stall an API request.
        "socket_connect_timeout": 3,
        "socket_timeout": 3,
    },
    task_publish_retry=True,
    task_publish_retry_policy={
        "max_retries": 2,
        "interval_start": 0,
        "interval_step": 1,
        "interval_max": 1,
    },
    task_soft_time_limit=settings.celery_task_soft_time_limit_seconds,
    task_time_limit=settings.celery_task_time_limit_seconds,
    result_expires=3600,
    worker_max_tasks_per_child=1000,
    worker_max_memory_per_child=200000,  # 200 MB
)


def _dispose_worker_engine(**_kwargs) -> None:
    """Replace the database pool inherited across ``fork``.

    A prefork child inherits the parent's pooled file descriptors. Those
    descriptors refer to the same server-side sessions, so two processes
    using them can corrupt each other's protocol state. Disposing the
    inherited pool in the child makes the next checkout open a fresh
    connection owned by that child. The import is lazy so importing the
    Celery app never needs a database connection.
    """

    from app.db.session import dispose_engine

    dispose_engine()


# weak=False: the receiver must outlive the module-level registration for
# the lifetime of the worker process.
worker_process_init.connect(_dispose_worker_engine, weak=False)
worker_process_shutdown.connect(_dispose_worker_engine, weak=False)


if __name__ == "__main__":
    celery_app.start()
