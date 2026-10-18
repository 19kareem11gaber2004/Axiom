# Axiom — Job-Reliability Contract (Phase 1, Step 5)

One page. Governs Celery + Redis + Postgres recovery for v0.1. State machine: `04-state-machine.md`. Transaction detail: `03-erd-api.md §6`.

## Claim [v0.1]

Only entry to `processing`. Exactly one worker wins; losers exit quietly (duplicate delivery safe).

```sql
UPDATE documents
   SET status = 'processing',
       processing_attempts = processing_attempts + 1,
       updated_at = now()
 WHERE id = :id AND workspace_id = :ws
   AND status IN ('queued', 'retrying')
RETURNING *;
```

Zero rows → exit, no side effects. `processing_attempts` is incremented only here, so it counts claims, never retries of the endpoint.

## Guarded commit [v0.1]

One transaction, all-or-nothing:

1. `DELETE FROM chunks WHERE document_id=:id`
2. `INSERT` new chunk set (conflict on `(document_id, chunk_index)` aborts)
3. `UPDATE documents SET status='indexed', indexed_at=now(), error_code=NULL ... WHERE id=:id AND status IN ('processing','retrying') AND processing_attempts=:claimed`
4. Close the `processing_runs` row (finished_at, stage `indexed`)

Zero-row update in step 3 → `ROLLBACK` (stale worker or concurrent delete won; clean temp files, never recreate the parent row).

## Retries: bounded, classified [v0.1]

- Transient (retry): provider timeout/5xx/rate-limit, connection reset. Permanent (no retry): corrupt/empty/unsupported file, validation failure.
- Cap: `MAX_ATTEMPTS = 5` claims per document (tunable, Phase 4). Exhaustion → `failed` with `error_code=ATTEMPTS_EXHAUSTED`, `retryable=false`.
- Backoff: `countdown = min(30 * 2^(attempt-1) + rand(0,10)s, 900s)` between worker retries; endpoint `POST retry` is manual and bypasses backoff (resets to `queued`).
- Time limits: soft 480s / hard 600s per task. Worker runs non-root, memory-capped (Phase 4 values); kill → sweeper path below.

## Broker and visibility [v0.1]

- Celery: `task_acks_late=True`, `worker_prefetch_multiplier=1`, `visibility_timeout=1800s` (3× hard limit; must always exceed max task runtime), soft/hard limits above, `worker_max_tasks_per_child` set (Phase 4 number).
- Redis: persistence ON (`appendonly yes`), `maxmemory-policy noeviction`, separate DBs for broker vs results. Postgres remains source of truth; Redis loss only delays, never corrupts (sweeper re-derives state).

## Sweeper (periodic, every 60s) [v0.1]

| Stuck row | Threshold | Action (attempt-capped) |
|---|---|---|
| `uploaded` older than 5 min (saved but never enqueued) | `updated_at < now()-5m` | → `queued` + publish, log warning |
| `queued` older than 10 min (broker lost it) | `updated_at < now()-10m` | re-publish (claim guards duplicates), log warning |
| `processing` older than 15 min (worker died / visibility lost) | `updated_at < now()-15m` | attempts < cap → `retrying`; else → `failed TIMEOUT`, log warning |

Every sweep action counts toward `MAX_ATTEMPTS` (a sweep re-queue is a claim like any other). Sweeping is idempotent: re-running changes nothing once rows move.

## Outbox: deferred [later]

Trigger to add a transactional outbox (`outbox_events` + dispatcher): ≥2 publishers outside the API process, an audit need for publish events, or measured sweep delay >10 min p99. Until then the sweeper above is the recovery path; no FK or schema change is needed to add it later.
