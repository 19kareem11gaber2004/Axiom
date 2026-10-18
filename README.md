# Axiom — Multimodal Enterprise RAG Platform

## Overview

Axiom is a web-based enterprise intelligence platform that enables organizations to upload, search, understand, and reason over company documents.

This README covers **Step 9 — Walking Skeleton**: the bare minimum that runs end-to-end (FastAPI + PostgreSQL/pgvector + Redis + Celery worker), with health/readiness endpoints, Alembic migrations, tests, Docker Compose, and GitHub Actions CI.

No PDF processing, authentication, RAG, or AI features are implemented yet. Those arrive in Phases 2–5.

## Prerequisites

- **Docker Desktop** (Windows) — provides `docker compose`
- **PowerShell** 5.1+ or PowerShell 7+ (built into Windows 10/11)

## Quick Start (PowerShell)

Run these commands from the repository root (`C:\Users\kioskUser0\Axiom`).

### 1. Create environment file

```powershell
cp .env.example .env
```

This copies the placeholder credentials. `.env` is excluded from git and from the Docker build context (`.dockerignore`). Do not commit `.env`.

### 2. Start PostgreSQL and Redis

```powershell
docker compose up -d postgres redis
```

Wait for both to become healthy (about 10–30 seconds). Check status:

```powershell
docker compose ps
```

### 3. Apply the database migration

```powershell
cd backend
alembic upgrade head
cd ..
```

This creates all 9 v0.1 tables, the `vector` extension, enum types, constraints, and indexes in PostgreSQL.

### 4. Start the API and worker

```powershell
docker compose up api worker
```

The API is now available at `http://localhost:8000`.

### 5. Verify health

```powershell
curl localhost:8000/health
```

Expected: `{"status":"ok"}`

```powershell
curl localhost:8000/ready
```

Expected (when PostgreSQL and Redis are healthy): `{"status":"ready","checks":{"postgres":"up","redis":"up"}}`
Expected (if either is down): HTTP 503 with `{"status":"not_ready",...}`

## Running Tests

### Unit tests (no infrastructure required)

```powershell
cd backend
python -m pytest -q
cd ..
```

These run against the in-process FastAPI app with mocked dependency checks. No database or Redis needed.

### Integration smoke test (requires running compose)

```powershell
cd backend
python -m pytest -q tests/test_smoke_integration.py
cd ..
```

This connects to real PostgreSQL and Redis via the `.env` connection strings and verifies `SELECT 1`, Redis `PING`, and that all 9 v0.1 tables exist.

## Stopping and Cleaning Up

### Stop containers (keeps named volumes — data persists)

```powershell
docker compose down
```

This stops and removes the API, worker, PostgreSQL, and Redis containers, but **keeps** the `pgdata` and `redisdata` named volumes. Your database data survives.

### Stop and remove everything (removes named volumes — data is lost)

```powershell
docker compose down -v
```

This stops containers **and deletes** the `pgdata` and `redisdata` volumes. All database data is permanently lost. Use this only when you want a completely fresh start.

| Command | What happens to data |
|---------|---------------------|
| `docker compose down` | Containers removed; volumes kept; data persists |
| `docker compose down -v` | Containers removed; volumes deleted; data lost |

## Project Structure

```
Axiom/
├── .env.example              # Placeholder credentials (copy to .env)
├── .dockerignore             # Files excluded from Docker build context
├── docker-compose.yml        # PostgreSQL, Redis, API, Worker services
├── README.md                 # This file
├── backend/
│   ├── alembic.ini           # Alembic configuration
│   ├── alembic/
│   │   ├── env.py            # Alembic environment (reads DATABASE_URL)
│   │   └── versions/
│   │       └── 0001_v01_init.py   # Initial v0.1 schema migration
│   ├── Dockerfile            # Python 3.12-slim image
│   ├── requirements.txt      # Runtime dependencies
│   ├── requirements-dev.txt  # Test dependencies
│   └── app/
│       ├── __init__.py
│       ├── config.py         # Pydantic settings (DATABASE_URL, REDIS_URL, ENV)
│       ├── main.py           # FastAPI app: /health, /ready, request ID middleware
│       ├── db/
│       │   ├── __init__.py
│   │   │   └── models.py     # SQLAlchemy models (9 tables, workspace-scoped)
│       └── worker/
│           ├── __init__.py
│           └── celery_app.py # Celery app (late ack, prefetch 1, visibility 1800s)
│       ├── ai_workflow/
│       ├── api/
│       ├── auth/
│       ├── ingestion/
│       ├── retrieval/
│       ├── storage/
│       └── tests/
│           ├── conftest.py
│           ├── test_health.py
│           ├── test_ready.py
│           └── test_smoke_integration.py
├── .github/
│   └── workflows/
│       └── ci.yml            # GitHub Actions CI (migration, tests, compose, build)
├── docs/
│   └── phase-1/              # Phase 1 design documents
└── eval/
    └── v0.1/                 # Evaluation foundation (schema, corpus lock, seed questions)
```

## Static Checks (no infrastructure)

These should pass on any machine:

| Check | Command |
|-------|---------|
| Python compilation | `python -m py_compile backend/app/*.py backend/app/db/*.py backend/app/worker/*.py backend/tests/*.py backend/alembic/env.py backend/alembic/versions/*.py` |
| Offline migration SQL | `cd backend && alembic upgrade head --sql` |
| Compose config valid | `docker compose config --quiet` |
| CI YAML parses | `python -c "import yaml; yaml.safe_load(open('.github/workflows/ci.yml'))"` |

## Runtime Checks (require Docker)

| Check | Command | Expected |
|-------|---------|----------|
| Migration applies | `cd backend && alembic upgrade head` | Exit 0 |
| Health endpoint | `curl localhost:8000/health` | 200 `{"status":"ok"}` |
| Ready (deps up) | `curl localhost:8000/ready` | 200 ready |
| Ready (dep down) | Stop postgres/redis, then `curl localhost:8000/ready` | 503 not_ready |
| Tests pass | `cd backend && python -m pytest -q` | All pass |
| Image builds | `docker compose build` | Success |

## Known Limitations

- **Docker daemon was down during initial setup.** Runtime checks 5–11 (real PostgreSQL migration, health endpoint over HTTP, worker startup, image build) have not been executed yet. Verify them once Docker Desktop is running.
- Worker runs as non-root UID/GID `65532:65532`.
- Default credentials in `.env.example` are disposable local-development values only — never use in production.
- No authentication, PDF parsing, or AI features yet (Phases 2–5).

## CI

GitHub Actions (`.github/workflows/ci.yml`) runs on every push and pull request:

1. Starts `pgvector/pgvector:pg16` and `redis:7` service containers
2. Sets up Python 3.12
3. Installs dev requirements
4. Runs `alembic upgrade head` against real PostgreSQL
5. Runs `pytest`
6. Validates `docker compose config`
7. Builds the Docker image via `docker compose build`
