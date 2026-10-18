# Axiom — Decision Log (Phase 1, Step 8)

Append-only. Newest entries go at the bottom with date + reason. Never rewrite past entries; supersede by reference.

| ID | Date | Decision | Reason |
|---|---|---|---|
| D-001 | 2026-10-17 | Modular monolith, one codebase, worker as separate process | Clear module boundaries without distributed-system failure modes; extract services later only on measured need |
| D-002 | 2026-10-17 | Celery + Redis for background tasks; PostgreSQL is source of truth | Dedicated task lifecycle; Redis is transport only, never authoritative state |
| D-003 | 2026-10-17 | Composite FK `(id, workspace_id)` + `UNIQUE(id, workspace_id)` on parent tables | Cross-workspace chunk/history attach impossible at the DB level, not just app code |
| D-004 | 2026-10-17 | State-based sweeper over transactional outbox for v0.1 | Sweeper closes all three crash windows (save-but-not-queued, worker death, broker loss); outbox closes only the first. Outbox trigger documented in `05-job-contract.md` |
| D-005 | 2026-10-17 | Exact vector search first; no ANN index in v0.1 | No measured scale problem yet; HNSW only if exact search is measured too slow (Phase 6) with filtered-recall check |
