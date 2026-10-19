# Axiom — Multimodal Enterprise RAG Platform

**Building reliable enterprise AI systems, one verified milestone at a time.**

Axiom is an enterprise knowledge platform designed to help organizations ingest, search, and reason over internal information. Its long-term vision is to provide grounded answers with traceable citations, multimodal retrieval, and controlled AI-powered workflows.

The project follows an engineering-first approach, prioritizing secure data access, measurable retrieval quality, reliable infrastructure, and incremental delivery over a basic chatbot implementation.

> **Current milestone:** Step 9 — Walking Skeleton  
> **Current focus:** Backend infrastructure, database migrations, health checks, automated testing, Docker, and CI  
> **Application status:** Infrastructure foundation; RAG capabilities are planned, not yet implemented.

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

The current milestone is the **Walking Skeleton**: a minimal, connected application environment that establishes the infrastructure required for subsequent development.

| Component | Current scope |
|---|---|
| FastAPI | Backend application and health endpoints |
| PostgreSQL + pgvector | Persistent storage and vector-search infrastructure |
| SQLAlchemy | Database models and persistence layer |
| Alembic | Version-controlled database migrations |
| Redis | Message-broker infrastructure |
| Celery | Asynchronous task-processing foundation |
| Docker Compose | Local multi-service environment |
| Pytest | Unit and integration test foundation |
| GitHub Actions | Automated CI workflow |

The repository also contains initial application module boundaries for authentication, ingestion, retrieval, storage, and AI workflows. Their presence does not mean those capabilities are implemented.

### Not implemented yet

- Authentication and workspace authorization
- Document upload and ingestion
- PDF extraction and chunking
- Embedding generation and retrieval pipelines
- RAG answer generation and citation validation
- Hybrid retrieval and reranking
- Multimodal processing
- Agentic workflows and human-in-the-loop approval

These capabilities will be introduced in later milestones and verified through explicit acceptance criteria.

---

## Architecture

Axiom begins as a **modular monolith**: one backend application with clearly separated internal responsibilities, supported by dedicated infrastructure services.

```text
                    Client Applications
                            |
                            v
                      FastAPI API
                            |
             +--------------+--------------+
             |                             |
             v                             v
        PostgreSQL                      Redis
        + pgvector                        |
             |                            v
             |                       Celery Worker
             |                            |
             +----------------------------+
                   Shared Infrastructure
```

The diagram represents the planned service relationships at the infrastructure level. Document-processing and retrieval workflows will be added in subsequent milestones.

### Architectural responsibilities

- **API layer:** Exposes HTTP endpoints and handles incoming requests.
- **Database layer:** Persists application data and will support vector retrieval.
- **Message broker:** Redis provides the transport infrastructure for background tasks.
- **Worker layer:** Celery provides the execution environment for asynchronous jobs.
- **Migration layer:** Alembic manages version-controlled database schema changes.
- **CI pipeline:** Validates migrations, tests, configuration, and the backend image.

The initial migration is intended to establish nine application tables, together with the required PostgreSQL extension, enum types, constraints, and indexes. The actual migration must be executed successfully against PostgreSQL to verify the resulting schema.

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
| Message broker | Redis | Background-task transport |
| Task processing | Celery | Asynchronous job execution |
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

Create your local environment file:

```powershell
Copy-Item .env.example .env
```

Review `.env` and confirm that the PostgreSQL and Redis connection strings match the service names, ports, and credentials configured in `docker-compose.yml`.

**Security notes:**

- Never commit `.env` or real credentials.
- Keep `.env.example` limited to safe placeholders.
- Use a secrets manager or deployment-platform secrets for production credentials.
- Do not reuse disposable development credentials in production.

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

### 6. Start the API and worker

Build the backend image and start the application services:

```powershell
docker compose up --build api worker
```

The services run in the foreground, allowing you to inspect their logs.

To start them in the background instead:

```powershell
docker compose up -d --build api worker
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

---

## Running Tests

Run tests from the repository root unless a command explicitly changes directories.

### Unit tests

```powershell
Set-Location backend
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

It is designed to validate the project on pushes and pull requests.

The intended pipeline includes:

1. Provisioning PostgreSQL with pgvector and Redis service containers.
2. Setting up Python 3.12.
3. Installing development dependencies.
4. Applying Alembic migrations.
5. Running automated tests.
6. Validating the Docker Compose configuration.
7. Building the backend Docker image.

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
│   │       └── 0001_v01_init.py
│   ├── app/
│   │   ├── ai_workflow/
│   │   ├── api/
│   │   ├── auth/
│   │   ├── db/
│   │   │   └── models.py
│   │   ├── ingestion/
│   │   ├── retrieval/
│   │   ├── storage/
│   │   ├── worker/
│   │   │   └── celery_app.py
│   │   ├── config.py
│   │   └── main.py
│   ├── tests/
│   │   ├── conftest.py
│   │   ├── test_health.py
│   │   ├── test_ready.py
│   │   └── test_smoke_integration.py
│   ├── Dockerfile
│   ├── alembic.ini
│   ├── requirements.txt
│   └── requirements-dev.txt
├── docs/
│   └── phase-1/
├── eval/
│   └── v0.1/
│       ├── corpus.lock
│       ├── schema.json
│       └── seed-questions.jsonl
├── .dockerignore
├── .env.example
├── .gitignore
├── docker-compose.yml
└── README.md
```

The tree illustrates the intended repository layout. Empty modules and evaluation assets are scaffolding until their associated functionality and validation are implemented.

---

## Development Roadmap

Axiom will progress through incremental milestones with explicit acceptance criteria.

| Phase | Milestone | Primary deliverables |
|---|---|---|
| 1 | Walking Skeleton | Application startup, infrastructure, migrations, health checks, tests, and CI |
| 2 | Backend Foundations | Configuration, persistence, and workspace-scoped data access |
| 3 | Authentication and Authorization | Secure identities and enforced workspace isolation |
| 4 | Document Ingestion | Upload validation, background processing, and document lifecycle management |
| 5 | PDF RAG MVP | PDF extraction, chunking, embeddings, retrieval, citations, and abstention |
| 6 | Retrieval Evaluation | Baselines, vector-versus-hybrid comparisons, reranking experiments, and quality metrics |
| 7 | Multimodal Retrieval | Evaluated extensions for additional document modalities |
| 8 | Agentic Workflows | Controlled tool use, workflow orchestration, and human review where appropriate |

Phase 6 will build on the PDF MVP rather than expanding the system before a measurable retrieval baseline exists. Its comparisons will be driven by evaluation results instead of assumptions that more complex retrieval is automatically better.

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

- Axiom is currently an infrastructure foundation, not a functioning RAG application.
- Authentication and authorization have not yet been implemented.
- Document upload, parsing, indexing, and answer generation are not available yet.
- pgvector infrastructure does not, by itself, constitute an implemented retrieval pipeline.
- The evaluation directory provides scaffolding; it does not establish retrieval or generation quality.
- Docker and CI configuration must be verified through actual runtime and workflow executions.
- Local credentials and development settings are not suitable for production.
- Container privilege settings, worker execution permissions, secret handling, and other security controls require verification against the actual configuration before deployment.

---

## Stopping the Environment

### Stop containers and preserve data

```powershell
docker compose down
```

This stops and removes the Compose containers and network while preserving named volumes.

### Stop containers and delete persistent data

```powershell
docker compose down -v
```

**Warning:** This removes the named volumes associated with the Compose project, including persistent PostgreSQL and Redis data stored in those volumes. Use it only when a complete local reset is intentional.

| Command | Containers | Named volumes |
|---|---|---|
| `docker compose down` | Removed | Preserved |
| `docker compose down -v` | Removed | Removed |

---

## Project Direction

Axiom is being developed as a practical exploration of the engineering required to build reliable enterprise AI systems.

The immediate objective is to establish a verified backend foundation, then deliver a secure PDF-based RAG MVP with workspace isolation, evidence-backed citations, automated tests, and measurable retrieval quality.

Multimodal retrieval and agentic workflows will follow only when the core system is reliable and their additional complexity can be justified through testing and evaluation.

**The goal is not simply to make an LLM answer questions. It is to build an AI system whose behavior, evidence, and engineering decisions can be inspected and tested.**
