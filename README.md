# Axiom — Multimodal Enterprise RAG Platform

**Building reliable enterprise AI systems, one verified milestone at a time.**

Axiom is an enterprise knowledge platform designed to help organizations ingest, search, and reason over internal information. Its long-term vision is to provide grounded answers with traceable citations, multimodal retrieval, and controlled AI-powered workflows.

The project follows an engineering-first approach, prioritizing secure data access, measurable retrieval quality, reliable infrastructure, and incremental delivery over a basic chatbot implementation.

> **Current milestone:** Phase 5 — PDF RAG MVP
>
> **Current focus:** Baseline decisions, the frozen `eval/v0.1` corpus, and the provider/chunking/abstention contract that implementation must follow
>
> **Application status:** Phase 4 background processing is complete. The Phase 5 baseline is recorded, but the worker still verifies stored bytes only and does not extract text or generate embeddings yet. Retrieval and generation are planned, not yet implemented.

---

## Table of Contents

- [Project Overview](#project-overview)
- [Current Status](#current-status)
- [Architecture](#architecture)
- [Technology Stack](#technology-stack)
- [Prerequisites](#prerequisites)
- [Getting Started](#getting-started)
- [Running Tests](#running-tests)
- [Validation Checks](#validation-checks)
- [Health and Readiness Checks](#health-and-readiness-checks)
- [Continuous Integration](#continuous-integration)
- [Project Structure](#project-structure)
- [Development Roadmap](#development-roadmap)
- [Engineering Principles](#engineering-principles)
- [Current Limitations](#current-limitations)
- [Stopping the Environment](#stopping-the-environment)

---

## Project Overview

Enterprise knowledge is often distributed across documents and internal systems. Axiom aims to provide a foundation for retrieving relevant information and generating answers grounded in verifiable evidence.

The platform is being developed incrementally, beginning with a reliable backend foundation before introducing document processing, retrieval, and generation.

### Core objectives

- **Grounded generation:** Generate answers supported by retrieved evidence and traceable citations.
- **Workspace isolation:** Prevent unauthorized access to other workspaces' documents and data.
- **Reliable ingestion:** Process documents asynchronously with explicit lifecycle states.
- **Retrieval evaluation:** Measure retrieval quality and track regressions using evaluation datasets.
- **Operational reliability:** Apply database migrations, automated tests, health checks, and continuous integration.
- **Controlled AI workflows:** Introduce tool use and human approval when justified by the use case.

## Current Status

The current milestone is **PDF RAG MVP** (Phase 5). Step 1 is complete: the provider/model boundary, 768-dimensional embedding contract, `retrieval-trace-v1` schema, `pdf-text-v1` chunking rules, two-stage abstention, and the frozen `eval/v0.1` corpus are recorded in [`docs/phase-5/README5.md`](docs/phase-5/README5.md) and ADRs D-025–D-029.

Phase 4 is complete: a document committed as `queued` is published to Celery, claimed exactly once, processed outside the claim transaction, committed under a stale-worker guard, and recovered by a Beat-driven sweeper when a message or worker is lost. Its processor remains the **integrity processor**. It reads the stored object, verifies the uploaded checksum and PDF signature, and returns zero chunks. `indexed` currently means "the processing lifecycle completed"; Phase 5 replaces the processor and backfills the zero-chunk documents before retrieval can treat them as evidence.

| Component | Current scope |
|---|---|
| FastAPI | Application, structured error envelope, request IDs, 13 versioned operations across 10 paths under `/api/v1` |
| PostgreSQL + pgvector | Persistent storage, workspace-scoped schema, vector column, document lifecycle state |
| SQLAlchemy | 9 models, composite foreign keys enforcing workspace isolation |
| Alembic | Version-controlled migrations (`0001` initial schema, `0002` retryable state and sweeper index), verified for upgrade/downgrade on a real pgvector server |
| Redis | Three logical databases: DB 0 login rate limiting, DB 1 Celery broker, DB 2 Celery result backend (results ignored) |
| Celery worker | `documents.process`: claim, process, guarded commit, classified failure, fresh retry publish |
| Celery Beat | `documents.sweep` every 60 seconds under a PostgreSQL advisory lock |
| File storage | `FileStorage` port with a local atomic adapter; the API and worker share a Compose bind mount |
| Deduplication | Workspace-scoped SHA-256, advisory; new uploads `202`, duplicates `200`, duplicates are not published |
| Docker Compose | Local multi-service environment: PostgreSQL, Redis, API, worker, Beat |
| Pytest | 503 tests: unit, HTTP, real PostgreSQL/Redis integration, and recovery drills |
| GitHub Actions | Migration reversibility, full pytest, Compose validation, image build, Compose database migration, container smoke test, task registration, worker end-to-end flow, shared-storage check |

### Implemented endpoints

Thirteen versioned operations across ten `/api/v1` paths:

```
POST   /api/v1/auth/login                  POST   /api/v1/auth/logout
GET    /api/v1/auth/me                     POST   /api/v1/workspaces
GET    /api/v1/workspaces                  GET    /api/v1/workspaces/{id}
POST   /api/v1/workspaces/{id}/members     POST   /api/v1/documents
GET    /api/v1/documents                   GET    /api/v1/documents/{id}
GET    /api/v1/documents/{id}/file         POST   /api/v1/documents/{id}/retry
DELETE /api/v1/documents/{id}
```

Every error uses one envelope: `{"error": {"code", "message", "retryable", "request_id"}}`.

### Upload identity and duplicate behavior

`POST /api/v1/documents` identifies content by the **SHA-256 digest of its bytes**, scoped to the caller's workspace. The filename is metadata only.

| Request | Result |
|---|---|
| New content | `202` with a new `document_id`, `status: "queued"`, `duplicate: false` |
| Same bytes already in this workspace | `200` with the existing `document_id`, its current status, and `duplicate: true` |
| Same bytes with `?allow_duplicate=true` | `202` with a new document and a new file |
| Same bytes in another workspace | `202` with a new document; dedup never crosses a workspace |
| Existing match is `failed` | `200` with `duplicate: true`; retry it or force a new upload |

A duplicate is a successful lookup, not an error. It does **not** create a new file or row, and it does not change the existing document's metadata or status. Deduplication is **advisory**: two concurrent identical uploads can both create documents. The resulting `duplicate` field is part of the upload response, and both `200` and `202` are documented in the OpenAPI schema.

A new document is committed as `queued` and then published to the worker. A duplicate is not published: returning the existing row and starting another task for the same bytes would create duplicate work for a document that may already be indexed. If the broker is unavailable, the upload still succeeds because the row is durable; the sweeper republishes it later.

For the Phase 3 design rationale, the storage port, and transaction ordering, see [`docs/phase-3/README3.md`](docs/phase-3/README3.md). For the Phase 4 lifecycle, claim/commit SQL, failure taxonomy, and recovery drills, see [`docs/phase-4/README4.md`](docs/phase-4/README4.md).

### Background processing

A document moves through this lifecycle:

```text
POST /documents -> queued -> processing -> indexed
                       ^          |
                       |          +-> retrying -> processing -> indexed
                       |          |
                       +----------+-> failed (permanent or attempts exhausted)
```

- **Claim.** The worker performs a conditional `UPDATE ... WHERE status IN ('queued','retrying') AND processing_attempts < max_attempts RETURNING ...` and inserts a `processing_runs` row in the same transaction. Exactly one worker wins; duplicate delivery is a no-op for the loser.
- **Process.** The integrity processor reads the stored object outside the claim transaction, verifies its checksum and PDF signature, and returns no chunks.
- **Commit.** The worker replaces the chunk set and marks the document `indexed` only if the row is still owned by the same attempt. A stale worker's commit rolls back.
- **Retry.** A transient failure moves the document to `retrying`, closes the run row, and publishes a fresh task with exponential backoff. A permanent failure moves it to `failed` with a persisted `retryable` flag.
- **Manual retry.** `POST /documents/{id}/retry` accepts only `failed` and `retryable` documents. It sets `queued` without incrementing `processing_attempts`; the next claim consumes the attempt.
- **Recovery.** Beat runs `documents.sweep` every 60 seconds. The sweeper republishes stale `queued`/`retrying` rows, moves stale `uploaded` rows to `queued`, and turns stale `processing` rows into `retrying` or `failed` at the attempt cap.

The worker runs as a separate non-root Compose service with `task_acks_late=True`, `worker_prefetch_multiplier=1`, a 480 s soft limit, a 600 s hard limit, and a 1800 s visibility timeout. PostgreSQL is the source of truth; Redis only transports tasks.

### Phase 4 configuration

| Variable | Default | Effect |
|---|---|---|
| `MAX_PROCESSING_ATTEMPTS` | `5` | Claims allowed per document; manual retries do not consume the budget |
| `PROCESSING_RETRY_BASE_SECONDS` | `30` | Exponential backoff base |
| `PROCESSING_RETRY_MAX_SECONDS` | `900` | Backoff ceiling after jitter |
| `PROCESSING_RETRY_JITTER_SECONDS` | `10` | Uniform jitter added before the ceiling |
| `SWEEPER_INTERVAL_SECONDS` | `60` | Beat schedule for `documents.sweep` |
| `SWEEPER_UPLOADED_STALE_SECONDS` | `300` | Age before `uploaded -> queued` |
| `SWEEPER_QUEUED_STALE_SECONDS` | `600` | Age before a queued row is republished or failed at cap |
| `SWEEPER_RETRYING_STALE_SECONDS` | `600` | Age before a retrying row is republished or failed at cap |
| `SWEEPER_PROCESSING_STALE_SECONDS` | `900` | Age before a processing row is treated as a dead worker |
| `SWEEPER_BATCH_SIZE` | `100` | Rows per stale category per sweep |
| `CELERY_BROKER_URL` | Redis DB 1 | Celery broker; derived from `REDIS_URL` when blank |
| `CELERY_RESULT_BACKEND` | Redis DB 2 | Celery result backend; results are ignored but the backend is explicit |
| `CELERY_TASK_SOFT_TIME_LIMIT_SECONDS` | `480` | Soft task limit; becomes `PROCESSING_TIMEOUT` |
| `CELERY_TASK_TIME_LIMIT_SECONDS` | `600` | Hard task limit |
| `CELERY_VISIBILITY_TIMEOUT_SECONDS` | `1800` | Broker redelivery timeout |

The application refuses to start when `soft < hard < visibility` is violated or when the processing stale threshold is not greater than the hard limit. These relationships keep a live task from being redelivered or swept while it is still running. Host runs read the unprefixed names from `.env`; inside Compose, the Phase 4 tunables keep their defaults unless they are added to the forwarded environment block or passed with `-e`.

### Choosing a workspace

The endpoints that act "in a workspace" accept an optional `X-Workspace-ID` header. It is declared in the OpenAPI schema, so Swagger UI shows the field and the generated cURL includes it.

- An account that belongs to **exactly one** workspace does not need to send it; the server selects that workspace.
- An account that belongs to **several** workspaces must send the id of the one it wants to act in, otherwise the request is refused with `403` and a message naming the header.
- The value only *selects* among the caller's own workspaces; it never grants access to one.

Paths that already name the workspace (`/api/v1/workspaces/{id}`, `/api/v1/workspaces/{id}/members`) do not use the header at all.

### Not implemented yet

- PDF text extraction and page-aware chunking (the Phase 4 worker verifies bytes only)
- Embedding generation and vector retrieval
- Backfilling the zero-chunk documents indexed by the Phase 4 integrity processor
- RAG answer generation and citation validation
- Conversations and chat
- Hybrid retrieval and reranking
- Multimodal processing
- Agentic workflows and human-in-the-loop approval

These capabilities will be introduced in later milestones and verified through explicit acceptance criteria. Phase 5 Step 1 records the baseline decisions and freezes the evaluation corpus; it does not add extraction, embedding, retrieval, or generation code.

---

## Architecture

Axiom begins as a **modular monolith**: one backend application with clearly separated internal responsibilities, supported by dedicated infrastructure services.

```text
                         Client Applications
                                 |
                                 v
                           FastAPI API
                                 |
             +-------------------+-------------------+
             |                   |                   |
             v                   v                   v
      PostgreSQL           Redis DB 0          FileStorage
      + pgvector          login limiter      (local root or
             ^                   |             bind mount)
             |                   |                   ^
             |                   v                   |
             |            Redis DB 1 (broker)        |
             |                   |                   |
             |                   v                   |
             |            Celery Worker ------------+
             |                   ^
             |                   |
             |            Celery Beat
             |         (documents.sweep)
             |
             +------ claim / commit / runs
```

Redis DB 2 is the Celery result backend. It is configured explicitly so Celery never falls back to another transport, but the lifecycle ignores results; PostgreSQL is the system of record. Retrieval and generation will be added in later milestones.

### Architectural responsibilities

- **API layer:** Exposes HTTP endpoints, authenticates callers, commits upload state, publishes best-effort tasks, and maps domain errors to the common envelope.
- **Database layer:** Persists application data, workspace memberships, document metadata, processing runs, chunks, and vector-ready tables.
- **File storage layer:** Stores original bytes behind the `FileStorage` port; `LocalFileStorage` is the only adapter, and the API and worker share the same root.
- **Message broker:** Redis Database 1 transports Celery tasks; Database 2 is the result backend, and Database 0 is reserved for login rate limiting.
- **Worker layer:** Celery claims documents, runs the processor outside the transaction, commits under an attempt guard, and classifies failures.
- **Scheduler layer:** Celery Beat enqueues the stale-document sweeper under a PostgreSQL advisory lock.
- **Migration layer:** Alembic manages version-controlled database schema changes, including the Phase 4 retryable state and sweeper index.
- **CI pipeline:** Validates migration reversibility, tests, Compose configuration, the backend image, task registration, the end-to-end upload-to-indexed flow, and shared API/worker storage.

The schema has nine application tables, together with the required PostgreSQL extension, enum types, constraints, and indexes. Revision `0002` adds `documents.retryable` and the `(status, updated_at)` index the sweeper scans; both migrations have been executed against PostgreSQL with upgrade/downgrade checks.

---

## Technology Stack

| Category | Technology | Purpose |
|---|---|---|
| Language | Python 3.12 | Backend development |
| API framework | FastAPI | HTTP API and health endpoints |
| Database | PostgreSQL | Persistent application data |
| Vector infrastructure | pgvector | Vector storage and similarity-search support |
| ORM | SQLAlchemy | Database interaction |
| Migrations | Alembic | Schema versioning |
| File storage | `FileStorage` port + local adapter | Durable upload bytes with an object-store seam |
| Deduplication | Workspace-scoped SHA-256 | Advisory content identity for idempotent uploads |
| Message broker | Redis | Background-task transport (DB 1) and result backend (DB 2) |
| Task processing | Celery | Asynchronous job execution and retry publication |
| Scheduling | Celery Beat | Periodic stale-document sweeper |
| Containerization | Docker | Reproducible application environment |
| Orchestration | Docker Compose | Local multi-service development |
| Testing | Pytest | Automated verification |
| CI/CD foundation | GitHub Actions | Automated validation on pushes and pull requests |

---

## Prerequisites

Ensure the following tools are installed:

- Windows 10 or 11
- Docker Desktop with the Docker Engine running
- Python 3.12
- Git
- PowerShell 5.1 or later

Verify your installations:

```powershell
python --version
git --version
docker --version
docker compose version
```

Python should report version 3.12.x. Docker commands should work with the running Docker Engine.

---

## Getting Started

The following instructions describe the intended local-development workflow on Windows PowerShell.

### 1. Clone the repository

If you have not already cloned Axiom:

```powershell
git clone https://github.com/19kareem11gaber2004/Axiom.git
Set-Location Axiom
```

If the repository is already available locally, navigate to its root directory instead.

### 2. Configure environment variables

The recommended workflow runs the API and the worker **inside Docker**, and Docker Compose supplies their configuration. No `.env` file is needed for that.

Create a local environment file only if you intend to run the API on the host:

```powershell
Copy-Item .env.example .env
```

`.env` is read from the repository root, no matter which directory the process is started from. Its `DATABASE_URL` and `REDIS_URL` point at `localhost`, which is the correct configuration for a process running **on the host** and the wrong one for a process running **in Docker** (inside the Compose network the services are reachable as `postgres:5432` and `redis:6379`).

**Security notes:**

- Never commit `.env` or real credentials.
- Keep `.env.example` limited to safe placeholders.
- Use a secrets manager or deployment-platform secrets for production credentials.
- Do not reuse disposable development credentials in production.

> **Do not mix the two workflows.** Running the API on the host while the containerised API is running gives two servers on port 8000. On Windows this does not produce a clear "address already in use" error - the socket layer allows both to bind - so requests are split between two processes that use different configuration. Pick one; see [Running the API on the host](#running-the-api-on-the-host-optional).

### 3. Start infrastructure services

Make sure Docker Desktop is running, then execute:

```powershell
docker compose up -d postgres redis
```

Check the service status:

```powershell
docker compose ps
```

Wait for PostgreSQL and Redis to become healthy before proceeding. If either service fails, inspect its logs:

```powershell
docker compose logs postgres
docker compose logs redis
```

### 4. Install backend dependencies

The backend package lives in `backend/`, so `app` is only importable from that directory. Commands that import `app` (uvicorn, alembic, the bootstrap command) must run with `backend/` as the working directory.

Create and activate a virtual environment if you do not already have one:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
```

Install the backend dependencies:

```powershell
python -m pip install -r backend/requirements.txt
```

If the development requirements file is separate, install it as well:

```powershell
python -m pip install -r backend/requirements-dev.txt
```

Run these commands from the repository root. If PowerShell blocks virtual-environment activation, review your local execution policy rather than changing system-wide settings unnecessarily.

### 5. Apply database migrations

Run:

```powershell
Set-Location backend
python -m alembic upgrade head
Set-Location ..
```

This applies the migration history to the configured PostgreSQL database.

A successful migration is required before the application can rely on the schema. If it fails, inspect the database connection settings, service health, extension availability, and migration logs.

If the API runs in Docker, run the migration inside the Compose network instead, so it reaches the same database the API uses:

```powershell
docker compose run --rm --no-deps api alembic upgrade head
```

This runs against the Compose PostgreSQL service, which is the same database the API and worker use. `--no-deps` is safe because the migration command only needs PostgreSQL, which step 3 already started.

### 5a. Create the first administrator

Axiom has **no public registration**. The only way a user account is created is an administrator adding it to a workspace, and the very first administrator has to be created by hand:

```powershell
Set-Location backend
python -m app.auth.bootstrap --email admin@example.com --workspace "My Workspace"
Set-Location ..
```

With the API running in Docker, run the command inside the Compose network so it uses the same database:

```powershell
docker compose run --rm api python -m app.auth.bootstrap --email admin@example.com --workspace "My Workspace"
```

The command prompts for the password (or reads it from `BOOTSTRAP_PASSWORD`). It refuses to run when the database already contains users, so it can never mint a second administrator unnoticed.

Generate a strong signing key for anything other than local development:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Put the result in `JWT_SECRET_KEY` in `.env` (host workflow) or in `AXIOM_JWT_SECRET_KEY` for the Compose workflow. The application **refuses to start** with the placeholder secret when `ENV` is not `dev`.

### 6. Start the API, worker, and scheduler

Build the backend image and start the application services:

```powershell
docker compose up --build api worker beat
```

The services run in the foreground, allowing you to inspect their logs. Beat is a separate service because the sweeper must run even when no API request or worker task is active.

To start them in the background instead:

```powershell
docker compose up -d --build api worker beat
```

Or start the whole stack (PostgreSQL, Redis, API, worker, Beat) in one command:

```powershell
docker compose up -d --build
```

The API is expected to be available at:

- API base URL: http://localhost:8000
- Interactive API documentation: http://localhost:8000/docs

The actual routes and documentation settings are determined by the running FastAPI application.

### 7. Verify the application

Check API liveness:

```powershell
Invoke-RestMethod http://localhost:8000/health
```

Expected response:

```json
{
  "status": "ok"
}
```

Check dependency readiness:

```powershell
Invoke-RestMethod http://localhost:8000/ready
```

Expected response when both dependencies are available:

```json
{
  "status": "ready",
  "checks": {
    "postgres": "up",
    "redis": "up"
  }
}
```

If a required dependency is unavailable, the readiness endpoint is expected to return HTTP 503 with a `not_ready` status.

These are expected response contracts, not claims that the current implementation has already passed a live runtime check.

### Authenticate and call a protected endpoint

```powershell
$login = Invoke-RestMethod -Method Post `
  -Uri http://localhost:8000/api/v1/auth/login `
  -ContentType "application/json" `
  -Body '{"email":"admin@example.com","password":"<the password you chose>"}'

$headers = @{ Authorization = "Bearer $($login.access_token)" }
Invoke-RestMethod http://localhost:8000/api/v1/auth/me -Headers $headers
```

Expected: the caller as resolved **on the server** - id, email, `workspace_id` and `role`. The workspace and role come from the database, never from the token.

Create a workspace (the caller becomes its `owner`):

```powershell
Invoke-RestMethod -Method Post -Uri http://localhost:8000/api/v1/workspaces `
  -ContentType "application/json" -Body '{"name":"My Workspace"}' -Headers $headers
```

---

## Running the API on the host (optional)

The Docker workflow above is the recommended one: the API and the worker then reach PostgreSQL and Redis over the Compose network, at `postgres:5432` and `redis:6379`.

Running the API on the host is supported, but it needs two extra things.

**1. Publish PostgreSQL and Redis to the host.** They are not published by default, which is deliberate: the default stack does not expose a database to the host (or the network). Use the override file:

```powershell
docker compose -f docker-compose.yml -f docker-compose.host-dev.yml up -d postgres redis
```

It binds both to `127.0.0.1`, so they are reachable from the host only.

**2. Stop the containerised API.** Two servers on port 8000 is a silent problem on Windows:

```powershell
docker compose stop api
```

Then start the API from the `backend` directory (the `app` package is not importable from the repository root):

```powershell
Set-Location backend
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

With `backend/` as the working directory, `.env` is still read from the repository root, so `DATABASE_URL` and `REDIS_URL` keep their `localhost` values.

To go back to the containerised API:

```powershell
Set-Location ..
docker compose up -d api
```

---

## Recovering access to an account

Axiom has no public registration and no "forgot password" flow, so an operator who loses the administrator password needs an explicit procedure. `python -m app.auth.password_reset` is it:

```powershell
docker compose run --rm api python -m app.auth.password_reset --email admin@example.com
```

(With the API on the host: `Set-Location backend` first, then `python -m app.auth.password_reset --email admin@example.com`.)

For a non-interactive run, put the new password in `AXIOM_ADMIN_PASSWORD` and pass it through:

```powershell
$env:AXIOM_ADMIN_PASSWORD = Read-Host "New password"
docker compose run --rm -e AXIOM_ADMIN_PASSWORD api python -m app.auth.password_reset --email admin@example.com
Remove-Item Env:AXIOM_ADMIN_PASSWORD
```

The command:

- only ever **updates an existing account**. It never creates a user and never grants a role, so it cannot mint an administrator behind an operator's back;
- preserves the account: the id, the email, the creation timestamp and every workspace membership stay exactly as they were. Only the stored password hash is replaced;
- reads the password from a prompt that does not echo it, or from the `AXIOM_ADMIN_PASSWORD` environment variable for non-interactive use. It never accepts the password on the command line by default, so it does not end up in shell history or in the process list;
- never prints the password or a token.

If the account does not exist, the command says so and changes nothing.

---

## Rate limiting and Redis outages

Login attempts are rate limited per account and per source IP. The counters live in Redis, so the behaviour when Redis is unreachable is an explicit decision, not an accident:

| `LOGIN_RATE_LIMIT_FAILURE_MODE` | Behaviour when Redis is unreachable |
|---|---|
| `open` (default) | The attempt is allowed through and the outage is logged once. The credential check still applies, so only this one defence-in-depth layer is weakened. |
| `closed` | No login is attempted. The endpoint answers `503` with the error code `DEPENDENCY_UNAVAILABLE`. |

The active mode is logged at startup and can be set per environment:

- host workflow: `LOGIN_RATE_LIMIT_FAILURE_MODE` in `.env`
- Compose workflow: `AXIOM_LOGIN_RATE_LIMIT_FAILURE_MODE` (defaults to `open`)

The `/ready` endpoint reports Redis as `down` while it is unreachable, so a load balancer stops routing traffic to an API whose limiter is not working.

---

## Running Tests

Run tests from the repository root unless a command explicitly changes directories.

### Unit tests

```powershell
Set-Location backend
python -m pytest -q
Set-Location ..
```

The suite contains 503 tests. Tests that need PostgreSQL or Redis are **skipped** when those services are unreachable, so the suite still runs offline. Database-backed worker tests include real claim concurrency, guarded commits, sweeper transitions, and the no-Redis recovery path:

```powershell
Set-Location backend
$env:DATABASE_URL = "postgresql+psycopg://axiom:axiom@127.0.0.1:5432/axiom"
$env:REDIS_URL = "redis://127.0.0.1:6379/0"
python -m pytest -q
Set-Location ..
```

Unit tests are intended to verify application behavior without requiring live infrastructure when dependencies are appropriately mocked.

### Integration smoke test

Start PostgreSQL and Redis first, then run:

```powershell
Set-Location backend
python -m pytest -q tests/test_smoke_integration.py
Set-Location ..
```

The integration test is intended to verify:

- PostgreSQL connectivity
- Redis connectivity
- The existence of the nine expected application tables

The test must use the correct environment configuration and a successfully migrated database.

### Running the whole suite against the Compose services

The default stack does not publish PostgreSQL or Redis to the host, so a host-side test run cannot reach them and the database-backed tests are skipped. Run the suite inside the Compose network instead, where the same `postgres:5432` and `redis:6379` addresses are used:

```powershell
docker compose run --rm api sh -c "pip install --no-cache-dir -r requirements-dev.txt && python -m pytest -q"
```

The image installs only the runtime dependencies, so the development requirements (pytest, httpx) are installed for this one-off run.

To run the suite on the host instead, publish the database ports first:

```powershell
docker compose -f docker-compose.yml -f docker-compose.host-dev.yml up -d postgres redis
Set-Location backend
python -m pytest -q
Set-Location ..
```

---

## Validation Checks

These checks provide additional confidence in the repository before integration or deployment.

### Python compilation

From the repository root:

```powershell
python -m compileall -q backend/app backend/tests backend/alembic
```

This checks Python syntax and compiles the selected modules. It does not validate application behavior or external dependencies.

### Offline migration SQL

From the `backend` directory:

```powershell
python -m alembic upgrade head --sql
```

This generates migration SQL without applying it to the database. A successful run does not prove that the migration will execute successfully against a live PostgreSQL instance.

### Docker Compose configuration

From the repository root:

```powershell
docker compose config --quiet
```

This checks whether Docker Compose can parse and resolve the configuration. It does not prove that every service will start successfully.

### GitHub Actions workflow syntax

If PyYAML is installed:

```powershell
python -c "import yaml; yaml.safe_load(open('.github/workflows/ci.yml', encoding='utf-8')); print('YAML parsed successfully')"
```

This checks YAML parsing only. A successful parse does not guarantee that GitHub Actions will execute the workflow successfully.

---

## Health and Readiness Checks

Axiom distinguishes between two operational checks:

| Endpoint | Responsibility |
|---|---|
| `/health` | Reports whether the API process is alive and responding |
| `/ready` | Checks whether the API's required dependencies are available |

**Liveness** helps determine whether the application process is running.

**Readiness** helps determine whether the application can serve requests that depend on its infrastructure.

Keeping these checks separate makes local debugging and future deployment health monitoring more reliable.

---

## Continuous Integration

The GitHub Actions workflow is located at:

```text
.github/workflows/ci.yml
```

It validates the project on pushes and pull requests.

The workflow includes:

1. Provisioning PostgreSQL with pgvector and Redis service containers.
2. Setting up Python 3.12.
3. Installing development dependencies.
4. Applying Alembic migrations and verifying downgrade/upgrade reversibility.
5. Running the full automated test suite.
6. Validating the Docker Compose configuration.
7. Building the backend Docker image.
8. Migrating the separate Compose PostgreSQL database before startup.
9. Starting the API, worker, and Beat and waiting for their readiness logs.
10. Verifying that `documents.process` and `documents.sweep` are registered.
11. Running the end-to-end upload-to-indexed worker smoke test.
12. Verifying that the API and worker can read and write the same storage root.

CI helps identify regressions before changes are merged. It does not automatically repair faulty code or guarantee production readiness.

A workflow is considered verified only after an actual GitHub Actions run completes successfully. Review the run logs and individual job results to diagnose failures.

---

## Project Structure

```text
Axiom/
├── .github/
│   └── workflows/
│       └── ci.yml
├── backend/
│   ├── alembic/
│   │   ├── env.py
│   │   └── versions/
│   │       ├── 0001_v01_init.py
│   │       └── 0002_phase4_background_processing.py
│   ├── app/
│   │   ├── ai_workflow/
│   │   ├── api/
│   │   │   ├── errors.py
│   │   │   ├── schemas.py
│   │   │   └── routes/
│   │   │       ├── auth.py
│   │   │       ├── documents.py
│   │   │       └── workspaces.py
│   │   ├── auth/
│   │   │   ├── bootstrap.py
│   │   │   ├── dependencies.py
│   │   │   ├── password_reset.py
│   │   │   ├── rate_limit.py
│   │   │   └── service.py
│   │   ├── db/
│   │   │   ├── models.py
│   │   │   └── session.py
│   │   ├── ingestion/
│   │   │   ├── service.py
│   │   │   └── validation.py
│   │   ├── processing/
│   │   │   ├── base.py
│   │   │   ├── errors.py
│   │   │   └── integrity.py
│   │   ├── retrieval/
│   │   ├── storage/
│   │   │   ├── __init__.py
│   │   │   ├── base.py
│   │   │   ├── keys.py
│   │   │   └── local.py
│   │   ├── worker/
│   │   │   ├── celery_app.py
│   │   │   ├── pipeline.py
│   │   │   ├── publisher.py
│   │   │   ├── sweeper.py
│   │   │   └── tasks.py
│   │   ├── config.py
│   │   └── main.py
│   ├── scripts/
│   │   └── worker_smoke.py
│   ├── tests/
│   │   ├── conftest.py
│   │   ├── fakes/
│   │   │   ├── publisher.py
│   │   │   └── storage.py
│   │   ├── test_auth_api.py
│   │   ├── test_auth_dependencies.py
│   │   ├── test_auth_service.py
│   │   ├── test_bootstrap.py
│   │   ├── test_config.py
│   │   ├── test_cors.py
│   │   ├── test_db_session.py
│   │   ├── test_documents.py
│   │   ├── test_errors.py
│   │   ├── test_health.py
│   │   ├── test_ingestion_service.py
│   │   ├── test_lifespan.py
│   │   ├── test_migration_sql.py
│   │   ├── test_openapi.py
│   │   ├── test_processing_integrity.py
│   │   ├── test_publisher.py
│   │   ├── test_rate_limit.py
│   │   ├── test_readiness.py
│   │   ├── test_ready.py
│   │   ├── test_smoke_integration.py
│   │   ├── test_storage.py
│   │   ├── test_storage_port.py
│   │   ├── test_sweeper.py
│   │   ├── test_worker.py
│   │   ├── test_worker_claim_concurrency.py
│   │   ├── test_worker_pipeline.py
│   │   ├── test_worker_recovery.py
│   │   └── test_workspaces.py
│   ├── .dockerignore
│   ├── Dockerfile
│   ├── alembic.ini
│   ├── requirements.txt
│   └── requirements-dev.txt
├── docs/
│   ├── phase-1/
│   │   ├── 01-user-stories.md
│   │   ├── 02-architecture.md
│   │   ├── 03-erd-api.md
│   │   ├── 04-state-machine.md
│   │   ├── 05-job-contract.md
│   │   ├── 06-arabic-scope.md
│   │   └── 07-decision-log.md
│   ├── phase-2/
│   │   └── README2.md
│   ├── phase-3/
│   │   └── README3.md
│   ├── phase-4/
│   │   └── README4.md
│   └── phase-5/
│       ├── PLAN.md
│       └── README5.md
├── eval/
│   └── v0.1/
│       ├── FREEZE.md
│       ├── MANIFEST.sha256
│       ├── corpus.lock
│       ├── schema.json
│       ├── seed-questions.jsonl
│       └── verify.py
├── .dockerignore
├── .env.example
├── .gitignore
├── docker-compose.yml
├── docker-compose.host-dev.yml
└── README.md
```

At runtime, `backend/storage/` holds uploaded files. It is excluded from the image and from git, and Compose bind-mounts it into both the API and worker containers. The `retrieval/` module is still empty: retrieval is not implemented yet.

---

## Development Roadmap

Axiom will progress through incremental milestones with explicit acceptance criteria.

| Phase | Milestone | Primary deliverables | Status |
|---|---|---|---|
| 1 | Walking Skeleton | Application startup, infrastructure, migrations, health checks, tests, and CI | Done |
| 2 | Backend Foundation | Configuration, persistence, authentication, workspace-scoped authorization, document management, error envelope, CORS, rate limiting | Done |
| 3 | Document Ingestion | `FileStorage` port and local adapter, workspace-scoped SHA-256 deduplication, upload lifecycle, and shared API/worker storage | Done |
| 4 | Background Processing | Celery worker and Beat, database claim, guarded commit, classified retries, sweeper, recovery drills | Done |
| 5 | PDF RAG MVP | PDF extraction, chunking, embeddings, backfill of Phase 4 zero-chunk documents, retrieval, citations, and abstention | In progress — baseline and frozen corpus complete; implementation next |
| 6 | Retrieval Evaluation | Baselines, vector-versus-hybrid comparisons, reranking experiments, and quality metrics | Planned |
| 7 | Multimodal Retrieval | Evaluated extensions for additional document modalities | Planned |
| 8 | Agentic Workflows | Controlled tool use, workflow orchestration, and human review where appropriate | Planned |

Phase 6 will build on the PDF MVP rather than expanding the system before a measurable retrieval baseline exists. Its comparisons will be driven by evaluation results instead of assumptions that more complex retrieval is automatically better.

Phase 3 is explained in depth in [`docs/phase-3/README3.md`](docs/phase-3/README3.md); Phase 4 is explained in [`docs/phase-4/README4.md`](docs/phase-4/README4.md); the Phase 5 baseline is in [`docs/phase-5/README5.md`](docs/phase-5/README5.md); the Phase 2 guide remains at [`docs/phase-2/README2.md`](docs/phase-2/README2.md).

---

## Engineering Principles

### Modular monolith first
Maintain clear module boundaries without introducing microservices before there is a demonstrated need.

### Security by design
Enforce workspace authorization at the data-access layer and test for cross-workspace data leakage.

### Grounded answers
Require evidence-backed responses, traceable citations, and abstention when retrieved evidence is insufficient.

### Evaluation over intuition
Use versioned datasets, measurable retrieval metrics, and regression tests to assess changes in quality.

### Operational reliability
Use schema migrations, asynchronous processing, health checks, structured error handling, and automated validation.

### Incremental delivery
Complete and verify one milestone before adding complexity. Document important architectural decisions and their trade-offs.

---

## Current Limitations

- Axiom is currently a secure API foundation, not a functioning RAG application. No answer is generated yet.
- **The worker does not index documents yet.** Phase 4 runs an integrity processor: it re-reads the stored object, verifies the checksum and PDF signature, and returns zero chunks. It does not extract text, count pages, chunk, or embed.
- **`indexed` does not guarantee searchable content.** Every document indexed by Phase 4 has zero chunks. Phase 5 must replace the processor and backfill those documents before retrieval can treat `indexed` as evidence.
- **Recovery is periodic, not immediate.** A lost publish is repaired by the sweeper after the relevant stale threshold (10 minutes for `queued`/`retrying`, 15 minutes for `processing` by default), and Beat only runs every 60 seconds. The state-based sweeper is a deliberate alternative to a transactional outbox, so the guarantee is eventual recovery rather than instantaneous redelivery.
- **Processing is at least once.** Redis may deliver the same task more than once, and a worker may be killed after committing but before acknowledging. The claim and guarded commit make duplicate delivery harmless; they do not prevent duplicate delivery.
- **Manual retries share the claim budget.** The retry endpoint does not increment `processing_attempts`, but the next claim does. A retry of a document already at the cap is accepted into `queued` and then failed by the sweeper as `ATTEMPTS_EXHAUSTED`; there is no per-user reset of the budget.
- **Access tokens are stateless.** `POST /auth/logout` cannot revoke a token on the server; the client discards it. There is no token blocklist and no refresh token. Workspace and role are re-read from the database on every request, so a revoked membership or a demotion takes effect immediately.
- **The login rate limiter fails open by default.** If Redis is unreachable the limiter is bypassed and a warning is logged, because failing closed would turn a Redis outage into a total login outage. The credential check still applies. The posture is configurable (`LOGIN_RATE_LIMIT_FAILURE_MODE=closed` refuses logins with 503 while the limiter is unreachable) and is logged at startup.
- **`X-Forwarded-For` is not trusted.** The per-IP login limit sees the proxy address behind a reverse proxy. Honouring the header is a deployment concern, because a client could otherwise spoof it to escape the limit.
- **The document file store is local.** `LocalFileStorage` is the only implemented `FileStorage` adapter; an object-store implementation is not included yet. Files live under `backend/storage/` on the host and are removed when the document is deleted.
- **The shared storage mount is a local-development arrangement.** Compose bind-mounts `./backend/storage` into both the API and worker at `/srv/backend/storage`, so both processes see the same bytes. A multi-host production deployment needs an object store or a shared filesystem; the bind mount is not that solution.
- **Deduplication is advisory, not a uniqueness guarantee.** The `(workspace_id, checksum)` index is non-unique, and the service performs a check-then-insert. Two concurrent identical uploads can both create documents. This is intentional so `allow_duplicate=true` and legitimate re-uploads remain possible; strict idempotency would need a separate idempotency key or conflict path.
- **The file store and database are not atomic.** Uploads write the file before committing the row, and failures are compensated best-effort. A process crash between the file write and the commit can leave an unreferenced object; the current system has no orphan-file collector. Delete has the symmetric crash window: a file removed before a failed commit leaves a row with a missing file until a retry completes the delete.
- **Only PDF uploads are accepted**, and the upload and integrity processor check only a plain `%PDF-` signature. Page count, character count, and extraction-time limits arrive with real PDF parsing in Phase 5; the worker already has 480 s soft / 600 s hard Celery limits.
- **No `audit_logs` table yet.** Document deletion writes a structured log line; the permanent audit trail is Phase 10.
- **The evaluation corpus is frozen, but no quality has been measured.** `eval/v0.1` provides the locked sources, schema, 20 seed questions, manifest, and verifier; retrieval and generation metrics are Phase 6 work.
- **Local credentials and development settings are not suitable for production.** The application refuses to start with the placeholder `JWT_SECRET_KEY` when `ENV` is not `dev`.
- Container privilege settings, worker execution permissions, and secret handling require verification against the deployed configuration before production use.

---

## Stopping the Environment

Uploaded documents live in the host directory `backend/storage/`, which Compose bind-mounts into both the `api` and `worker` containers at `/srv/backend/storage`. Both services therefore see the same files, and the files survive container recreation and `docker compose down -v`.

### Stop containers and preserve data

```powershell
docker compose down
```

This stops and removes the Compose containers and network while preserving named volumes and the bind-mounted uploads.

### Stop containers and delete persistent data

```powershell
docker compose down -v
```

**Warning:** This removes the named volumes associated with the Compose project, including persistent PostgreSQL and Redis data stored in those volumes. It does **not** delete files in the bind-mounted `backend/storage/` directory, because Docker does not remove bind-mounted host directories.

To delete uploaded files as well, stop the stack first and then remove the host directory:

```powershell
docker compose down -v
Remove-Item -Recurse -Force backend\storage -ErrorAction SilentlyContinue
```

Use this only when a complete local reset is intentional.

| Command | Containers | Named volumes | Uploaded files (`backend/storage/`) |
|---|---|---|---|
| `docker compose down` | Removed | Preserved | Preserved |
| `docker compose down -v` | Removed | Removed | Preserved |

---

## Project Direction

Axiom is being developed as a practical exploration of the engineering required to build reliable enterprise AI systems.

The immediate objective is to establish a verified backend foundation, then deliver a secure PDF-based RAG MVP with workspace isolation, evidence-backed citations, automated tests, and measurable retrieval quality.

Multimodal retrieval and agentic workflows will follow only when the core system is reliable and their additional complexity can be justified through testing and evaluation.

**The goal is not simply to make an LLM answer questions. It is to build an AI system whose behavior, evidence, and engineering decisions can be inspected and tested.**
