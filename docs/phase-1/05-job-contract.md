# Axiom — Job-Reliability Contract (Phase 1, Step 5)

One page. Governs Celery + Redis + PostgreSQL recovery for v0.1. State machine: `04-state-machine.md`. Transaction detail: `03-erd-api.md §6`. Implementation guide: [`../phase-4/README4.md`](../phase-4/README4.md).

## Task payload [v0.1]

`documents.process` carries only `document_id` (D-017). The worker resolves `workspace_id` from the row, then repeats it in the claim. A forged or stale id cannot select another workspace.

`documents.sweep` carries no document id; it recovers by state.

## Claim [v0.1]

Only entry to `processing`. Exactly one worker wins; losers exit quietly (duplicate delivery safe).

```sql
-- Resolve the workspace from the row, not from the message.
SELECT workspace_id
FROM documents
WHERE id = :id;

UPDATE documents
   SET status = 'processing',
       processing_attempts = processing_attempts + 1,
       updated_at = now(),
       error_code = NULL,
       error_message = NULL,
       retryable = false
 WHERE id = :id
   AND workspace_id = :ws
   AND status IN ('queued', 'retrying')
   AND processing_attempts < :max_attempts
RETURNING *;
```

The claim inserts a `processing_runs` row with `stage='claimed'`, the returned `attempt_no`, and `started_at=now()` in the **same transaction**. Zero rows → rollback, no side effects. A failed run insert → rollback, so the document remains claimable.

`processing_attempts` is incremented only here, so it counts claims, never uploads and never retries of the endpoint. The cap is checked here as well: the fifth claim is allowed, a sixth is not.

## Guarded commit [v0.1]

One transaction, all-or-nothing:

1. `DELETE FROM chunks WHERE document_id=:id AND workspace_id=:ws`
2. `INSERT` the new chunk set; flush so a duplicate `(document_id, chunk_index)` raises `CHUNK_CONFLICT` before commit
3. `UPDATE documents SET status='indexed', indexed_at=now(), error_code=NULL, error_message=NULL, retryable=false WHERE id=:id AND workspace_id=:ws AND status IN ('processing','retrying') AND processing_attempts=:claimed RETURNING id`
4. Close the `processing_runs` row (`stage='indexed'`, `finished_at=now()`)

Zero-row update in step 3 → `ROLLBACK` (stale worker or concurrent delete won; the previous chunk set remains visible and nothing is re-inserted). A duplicate chunk index → `ROLLBACK` and a permanent-final `CHUNK_CONFLICT`.

The attempt number is the stale-worker barrier. A sweeper timeout followed by a new claim increments the counter, so the old worker's UPDATE matches zero rows even though the status is again `processing`.

## Failure classification [v0.1]

Two independent axes:

- `transient`: automatic retry may help while claims remain.
- `retryable`: the manual `POST /documents/{id}/retry` endpoint may re-queue the document from `failed`.

| Family | `transient` | `retryable` | Automatic retry | Manual retry |
|---|---|---|---|---|
| `TransientProcessingError` | yes | yes | yes, while claims remain | yes |
| `PermanentFixableError` | no | yes | no | yes, after repair |
| `PermanentFinalError` | no | no | no | no |

Concrete codes: `STORAGE_UNAVAILABLE`, `STORAGE_IO_ERROR`, `PROCESSING_TIMEOUT` (transient); `FILE_MISSING`, `STORAGE_PERMISSION_DENIED` (permanent-fixable); `CHUNK_CONFLICT`, `STORAGE_PATH_INVALID`, `CHECKSUM_MISMATCH`, `INVALID_PDF`, `EMPTY_FILE`, `INTERNAL_ERROR` (permanent-final). `ATTEMPTS_EXHAUSTED` is written at the cap with `retryable=false`.

A `SQLAlchemyError` is **not** a processing failure. The pipeline rolls back and the task re-raises it, so Celery records a database failure and the sweeper recovers the row; a database outage must not consume the document's attempt budget.

## Retries: bounded, classified [v0.1]

- A transient failure with a claim remaining → `retrying`, run closed as `retrying`, fresh task published after the transition commits.
- A transient failure at the cap → `failed`, `error_code=ATTEMPTS_EXHAUSTED`, `retryable=false`. No backoff is computed for the fifth claim.
- A permanent-fixable failure → `failed`, the original code, `retryable=true`.
- A permanent-final failure → `failed`, the original code, `retryable=false`.
- Backoff: `countdown = min(base * 2^(attempt-1) + rand(0, jitter), maximum)`, defaults `30 s`, `10 s`, `900 s`. With defaults, attempts 1–4 produce 30–40, 60–70, 120–130, and 240–250 seconds; attempt 5 is at the cap and does not retry.
- The next attempt is a **fresh task** published with a countdown. `self.retry()` is not used (D-020): Celery's retry state is not the source of truth, and a failed publish is recovered by the sweeper.
- A failed retry publish leaves the row `retrying`; the sweeper republishes it after the retrying stale threshold.

## Manual retry [v0.1]

`POST /documents/{id}/retry` accepts only `failed` with `retryable=true`:

```sql
UPDATE documents
   SET status = 'queued',
       error_code = NULL,
       error_message = NULL,
       retryable = false,
       updated_at = now()
 WHERE id = :id
   AND workspace_id = :ws
   AND status = 'failed'
   AND retryable = true
RETURNING id;
```

The endpoint does **not** increment `processing_attempts` and does **not** insert a `processing_runs` row; the next claim does both. It publishes best effort after the commit. Double-clicking retry is idempotent: the second call sees `queued`, matches zero rows, returns `409`, and starts no new attempt.

A manual retry at the cap is accepted into `queued` but cannot be claimed; the sweeper fails it as `ATTEMPTS_EXHAUSTED`. The claim budget is not reset by a user action.

## Broker and visibility [v0.1]

- Celery: `task_acks_late=True`, `worker_prefetch_multiplier=1`, `visibility_timeout=1800s` (must exceed the 600 s hard limit), soft limit 480 s, hard limit 600 s, `worker_max_tasks_per_child=1000`, 200 MB per child, `task_publish_retry` with a bounded policy, and 3 s broker connect/socket timeouts so a Redis outage cannot stall an API request.
- Redis: persistence ON (`appendonly yes`), `maxmemory-policy noeviction`, separate DBs for the rate limiter (0), broker (1), and results (2). The result backend is configured but ignored; PostgreSQL remains source of truth.
- Redis loss only delays work: queued messages disappear, rows remain, and the sweeper republishes them. Losing the result DB has no lifecycle effect.
- Worker and Beat run as non-root uid/gid `65532` in Compose. The worker has `stop_grace_period=30s`; a hard kill is recovered by the sweeper.

## Sweeper (periodic, every 60s) [v0.1]

The sweeper reads PostgreSQL and publishes after committing state. All four categories are selected oldest first, capped per category by `SWEEPER_BATCH_SIZE` (100).

| Stuck row | Threshold | Action (attempt-capped) |
|---|---|---|
| `uploaded` (supported recovery state) | `updated_at < now()-300s` | → `queued`, publish; if the row is at the cap, the queued rule fails it on a later sweep |
| `queued` (broker lost it / publish failed) | `updated_at < now()-600s` | attempts < cap → publish (status unchanged); attempts ≥ cap → `failed ATTEMPTS_EXHAUSTED`, `retryable=false` |
| `retrying` (retry task lost before consumption) | `updated_at < now()-600s` | attempts < cap → publish (status unchanged); attempts ≥ cap → `failed ATTEMPTS_EXHAUSTED` |
| `processing` (worker died / exceeded hard limit) | `updated_at < now()-900s` | attempts < cap → `retrying`, `PROCESSING_TIMEOUT`, `retryable=true`, close run as `timeout`, publish; attempts ≥ cap → `failed ATTEMPTS_EXHAUSTED`, close run as `timeout` |

Rules:

- The sweep does **not** increment `processing_attempts`; the next worker claim does. A recovery is skipped/failed when the budget is already spent.
- State transitions are committed before the first publish. A crash between commit and publish leaves the same stale row for the next sweep.
- `updated_at` is the retry marker for non-terminal states. It is touched only after a successful publish, with a status guard. A failed publish leaves the old timestamp, so the next sweep retries.
- A publish failure is logged and counted; it never aborts the rest of the batch.
- The sweeper holds a session-level PostgreSQL advisory lock (`pg_try_advisory_lock(0x41584F4D)`). A busy lock returns an empty summary. The lock is released in `finally`; an unlock failure invalidates the connection so a pooled session cannot keep the lock forever.
- Selection is not a lock. Every transition repeats the status and attempt guards, so a row claimed or deleted between selection and update is left alone.
- Sweeping is idempotent: re-running changes nothing once rows move, and a duplicate publish is harmless because the claim admits one worker.

## Outbox: deferred [later]

Trigger to add a transactional outbox (`outbox_events` + dispatcher): ≥2 publishers outside the API process, an audit need for publish events, or measured sweep delay >10 min p99. Until then the sweeper above is the recovery path; no FK or schema change is needed to add it later. D-004 remains the governing decision: the state-based sweeper closes the save-but-not-published, worker-death, and broker-loss windows; an outbox would close only the first.
