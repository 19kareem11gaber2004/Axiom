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

from app.config import settings

celery_app = Celery(
    "axiom",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["app.worker.tasks"],  # task modules are added in Phase 4
)

celery_app.conf.update(
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    broker_transport_options={"visibility_timeout": 1800},
    task_soft_time_limit=480,
    task_time_limit=600,
    result_expires=3600,
    worker_max_tasks_per_child=1000,
    worker_max_memory_per_child=200000,  # 200 MB
)

if __name__ == "__main__":
    celery_app.start()
