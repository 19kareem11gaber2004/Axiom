# Axiom — Understanding Phase 2 (Backend Foundation)

> **Who this is for.** You have the project and you want to *understand* it, not just run it. This document explains what Phase 2 built, why each piece exists, and how to read the code.
>
> **How to use it.** Read §1–§3 first (the story). Then read the code in the order given in §11. Every section names the exact file and function so you can follow along in your editor.
>
> The operational README stays in `README.md` (how to install and run). This one is about *why*.

---

## 1. The one-sentence version

Phase 2 turned Axiom from "a server that answers `/health`" into **a server that knows who is calling, decides what they may touch, and refuses everything else in a predictable way.**

---

## 2. The problem Phase 2 solves

Picture the simplest possible AI backend: you upload a PDF, you ask a question, you get an answer. Now think about the moment two different companies use the same deployment.

- Company A uploads their payroll spreadsheet.
- Company B asks: *"What is the CEO's salary?"*

If the retrieval query does not filter by tenant, Company B gets Company A's payroll. **That is the failure mode this entire phase is built to make impossible.**

A second problem: the LLM needs context, and that context must come from somewhere. If a document was uploaded but is still being processed, the system must not pretend it is searchable. And if the model answers, the human needs to see *which passage* it used.

Those two problems — **who may see what**, and **what is actually true right now** — are what Phase 2 solves. Everything else here (CORS, rate limiting, error envelopes) exists to make that core safe in practice.

---

## 3. What changed, concretely

| Before Phase 1 ended | After Phase 2 |
|---|---|
| 2 endpoints (`/health`, `/ready`) | 10 versioned endpoints under `/api/v1` |
| No identity at all | JWT bearer tokens, 4 workspace roles |
| No tenant boundary | `workspace_id` on every table + composite foreign keys |
| 9 tables, no API to reach them | full document lifecycle: upload → list → read → download → retry → delete |
| Plain `{"detail": ...}` errors | one envelope for every failure: `{"error": {code, message, retryable, request_id}}` |
| Any client could call the API | CORS restricted to configured browser origins |
| Unlimited login attempts | Redis-backed rate limiting, per account and per IP |
| 0 application tests | 159 tests against a real PostgreSQL and a real Redis |

---

## 4. The anatomy of a request

Read this once and the whole codebase makes sense. Two requests, traced end to end.

### 4.1 `POST /api/v1/auth/login`

```
Client:  POST /api/v1/auth/login
         {"email": "admin@example.com", "password": "..."}
              │
              ▼
[1] request_id middleware      app/main.py
    Reads X-Request-ID, or generates req_<16 hex chars>.
    Stores it on request.state so every later layer can echo it.
              │
              ▼
[2] CORS middleware            app/main.py
    If the browser sent an Origin, checks it against the allow-list.
    Adds the CORS headers to the reply.
              │
              ▼
[3] error boundary middleware  app/api/errors.py
    A safety net: if anything below raises, convert it into a clean
    500 JSON response instead of a stack trace.
              │
              ▼
[4] route handler              app/api/routes/auth.py → login()
    │
    ├─ Pydantic validates the body against TokenRequest
    │  (app/api/schemas.py). A bad email shape is rejected here,
    │  before any business logic runs.
    │
    ├─ rate limit check         app/auth/rate_limit.py
    │  Is this account (or this IP) over the failure limit?
    │  Over → 429, and we never run the expensive password check.
    │
    ├─ authenticate_user()      app/auth/service.py
    │  Look up the user, then bcrypt-verify the password.
    │  Returns None for BOTH "no such user" and "wrong password".
    │
    ├─ on failure: record the failure against account + IP, return 401
    │
    └─ on success: clear the counters, mint a JWT, list the user's
       workspaces, return 200
```

### 4.2 `GET /api/v1/auth/me` (a protected route)

```
Client:  GET /api/v1/auth/me
         Authorization: Bearer eyJhbGciOiJIUzI1NiIs...
              │
              ▼
[1..3] the same three middlewares
              │
              ▼
[4] route handler              app/api/routes/auth.py → me()
    │
    └─ Depends(get_current_membership)     app/auth/dependencies.py
       │
       ├─ Depends(get_current_user)
       │  ├─ HTTPBearer extracts the token
       │  ├─ decode_access_token() verifies SIGNATURE, EXPIRY and ALGORITHM
       │  └─ loads the user row from the database
       │       └─ if the account was deleted → 401
       │
       └─ resolves the workspace:
          - the X-Workspace-ID header, VERIFIED against workspace_memberships
          - or the user's only membership
          - no membership → 403
              │
              ▼
[5] handler runs, already knowing: user id, email, workspace_id, role
```

**The single most important line in this trace:** the workspace and role are read from the **database on every request**. They are never taken from the token, and never taken from the request body. See §7.

---

## 5. The seven pieces, one at a time

### 5.1 `app/config.py` — configuration

**What it does.** Reads `DATABASE_URL`, `REDIS_URL`, `JWT_SECRET_KEY` and friends from the environment (or a `.env` file).

**The concept: never hard-code a secret.** If the signing key lives in the code, everyone who reads the repo can mint valid access tokens for your system. It comes from the environment, and `.env` is in `.gitignore`.

**Why this file has a validator.** Look at `_reject_default_secret_outside_dev`. There is a development default so a fresh clone runs with zero setup — but the moment `ENV` is anything other than `dev`, the app **refuses to start**:

```python
if self.env.lower() != "dev" and self.jwt_secret_key == DEV_JWT_SECRET:
    raise ValueError("JWT_SECRET_KEY must be set to a strong random value ...")
```

Failing fast is much better than silently running with a guessable key.

---

### 5.2 `app/api/schemas.py` — request and response shapes

**What it does.** Defines the exact JSON shape of every request and response.

**The concept: validate at the boundary.** A Pydantic model is a contract. If `TokenRequest` says `email: EmailStr` and `password: str` with a length range, then a malformed body is rejected by FastAPI *before* your handler sees it. You never write `if not payload.get("email")` again.

**One subtlety worth knowing.** `EmailStr` is stricter than it looks. It rejects reserved domains such as `.test`, `.localhost` and `.invalid`, because those are not routable. This bit us: the bootstrap command used to create an administrator with an address the login endpoint then refused. Now `app/auth/bootstrap.py` validates with the *same* rule before touching the database.

**`from_attributes=True`** lets a schema be built straight from a SQLAlchemy row, so you do not hand-write dictionaries.

---

### 5.3 `app/api/errors.py` — one shape for every failure

**What it does.** Defines `AxiomError` and its subclasses, plus handlers that render them.

**The concept: a predictable error contract.** Every failure looks the same:

```json
{
  "error": {
    "code": "NOT_FOUND",
    "message": "Document not found.",
    "retryable": false,
    "request_id": "req_8f3a1c9d2e4b5a60"
  }
}
```

Why it matters in practice:
- A frontend branches on `code` (`"RATE_LIMITED"` → show a countdown), never on message text.
- `retryable` tells the client whether trying again could help.
- `request_id` lets a user quote one string that a human can grep in the server logs.
- **No stack trace ever reaches the client.** The catch-all handler logs the full exception server-side and returns a generic message.

**Read this if you want one important detail:** `ErrorBoundaryMiddleware` exists because of a *subtle* Starlette behaviour. Starlette attaches a plain `@app.exception_handler(Exception)` to `ServerErrorMiddleware`, which sits **outside** the user middleware stack. A 500 produced there gets no CORS headers, so the browser reports a CORS error instead of a server error — a genuinely confusing trap. The boundary middleware is registered *inside* CORS so the 500 passes through the CORS layer normally. The middleware order is `request_id → CORS → error boundary → router`, and there is a test that asserts this ordering.

---

### 5.4 `app/auth/service.py` — passwords and tokens

**What it does.** Hashes passwords, verifies them, mints JWTs, decodes JWTs.

**The concept: passwords are never stored.** A password is stored as a **bcrypt hash** — a one-way function with a random salt built in. Verifying means re-hashing the candidate and comparing. You cannot read the password back out of the database, which is exactly what you want if the database is ever stolen.

**The concept: a JWT is a signed claim, not a secret.** The token contains:

```json
{ "sub": "<user id>", "iat": 1784000000, "exp": 1784001800, "type": "access" }
```

The server signs it with `JWT_SECRET_KEY`. The server can therefore check "did I issue this, and is it still valid?" without storing anything. **But the content is readable by the client** — it is just base64, not encryption. Never put a secret in a JWT.

**Why this token carries almost nothing.** Look at what it does *not* contain: no workspace, no role. Deliberate. If the role were in the token, a demoted admin would keep admin powers until the token expired — up to 30 minutes of wrong access. Because the role is re-read from the database every request, a demotion takes effect **on the very next request**.

**`decode_access_token` is defensive on purpose:**
- `algorithms=[settings.jwt_algorithm]` pins the algorithm. Without this, a token claiming `alg: none` could slip through — the *algorithm confusion* attack.
- `options={"require": ["exp", "sub", "iat"]}` makes decoding fail when a claim is missing, so a token **without an expiry** can never be accepted.
- An expired token raises a distinct error so the client can say "session expired, log in again".

**One more detail:** `authenticate_user` returns `None` for both "no such account" and "wrong password", and it spends comparable CPU on a dummy hash when the account does not exist. Otherwise an attacker could measure response time to discover which emails are registered.

---

### 5.5 `app/auth/dependencies.py` — where authorization lives

**What it does.** Provides the FastAPI dependencies that resolve the caller.

| Dependency | Answers |
|---|---|
| `get_current_user` | *Who* are you? (401 if not) |
| `get_current_membership` | *Where* are you and *what* may you do? (403 if not) |
| `get_workspace_membership` | …in the workspace named in the **path**? |
| `require_role("admin")` | Are you at least an admin? |
| `require_workspace_role("admin")` | …in the **path's** workspace? |
| `ensure_role(membership, "admin")` | Check the role *after* the resource was resolved |

**The concept: dependency injection.** `Depends(get_current_user)` tells FastAPI: "before running this function, run `get_current_user` and pass me the result." Dependencies chain — `require_role` depends on `get_current_membership`, which depends on `get_current_user`. The route function stays about *your* logic; the plumbing lives in one place.

**The concept: the role hierarchy.** `ROLE_RANK = {viewer: 0, member: 1, admin: 2, owner: 2}`. Owner and admin share a rank because in v0.1 they are equivalent (see `docs/phase-1/03-erd-api.md` §5). `require_role("member")` therefore admits member, admin and owner.

**Why there are two "require role" variants.** `require_role` resolves the workspace from the `X-Workspace-ID` header. `require_workspace_role` resolves it from the **route path**. For `POST /workspaces/{id}/members` the path is authoritative — otherwise a caller could point a path-scoped route at a different workspace through a header.

---

### 5.6 `app/api/routes/` — the HTTP surface

Ten endpoints, in three groups.

**`auth.py`** — `POST /auth/login`, `GET /auth/me`, `POST /auth/logout`.

**`workspaces.py`** — `POST /workspaces`, `GET /workspaces`, `GET /workspaces/{id}`, `POST /workspaces/{id}/members`.

**`documents.py`** — `POST /documents`, `GET /documents`, `GET /documents/{id}`, `GET /documents/{id}/file`, `POST /documents/{id}/retry`, `DELETE /documents/{id}`.

**How a route is written** (the pattern to copy):

```python
@router.delete("/{document_id}", status_code=204)
def delete_document(
    document_id: uuid.UUID,
    membership: CurrentMembership = Depends(get_current_membership),
    db: Session = Depends(get_db),
):
    document = _load_document(db, document_id, membership)   # 404 if not yours
    ensure_role(membership, *DELETE_ROLE)                    # 403 if too low
    ...
```

**Why the resource is resolved *before* the role is checked.** A document in another workspace must answer `404`, not `403`. A `403` would *confirm the id exists*. So the order is: resolve the resource (404), then check the role (403), then check the state (409). The response therefore depends on the **resource**, not on who is asking.

**`app/storage/uploads.py` and `app/ingestion/validation.py`** handle uploads. The file goes to `storage/uploads/{document_id}/original.pdf` and the row is written in the same request — the `202` is only returned after **both** are durable. The user's filename is never used to build a path (it is normalised and sanitised), so a filename like `../../etc/passwd.pdf` cannot escape the storage directory.

**`app/auth/bootstrap.py`** creates the very first administrator, because there is no public registration: adding members requires an admin, and initially there is none. It refuses to run when the database already has users, so it can never mint a second administrator unnoticed.

---

### 5.7 `app/auth/rate_limit.py` — brute-force protection

**What it does.** Counts *failed* logins in Redis and blocks further attempts for a window.

**The concept: count failures, not attempts.** A legitimate user who mistypes their password once is not punished. The counter is cleared by a successful login. Only repeated failures accumulate.

**Two dimensions, two limits:**

| Scope | Limit | Why |
|---|---|---|
| Per account | 5 failures / 60 s | Stops a targeted attack on one account |
| Per source IP | 30 failures / 60 s | Stops credential stuffing across many accounts. Higher, because many users share one office address |

**The window is fixed.** The TTL starts at the first failure and is not extended by later ones, so the maximum attempts in any window is bounded.

**It fails open.** If Redis is unreachable, the limiter is bypassed and a warning is logged. The alternative — failing closed — would turn a Redis outage into a total login outage, which is a self-inflicted denial of service. The password check still applies either way; failing open only weakens one layer of defence.

**`X-Forwarded-For` is deliberately ignored.** A client could spoof that header to escape the per-IP limit. Honouring it is a deployment concern, handled by the reverse proxy, not by the application.

---

### 5.8 CORS — the browser-only rule

**What it does.** Decides whether *browser JavaScript* may read a response.

**The concept that trips everyone up: CORS is enforced by the browser, not the server.** The server always answers. The browser then decides whether to hand the response to the page. `curl`, `httpx` and the test suite are completely unaffected. **CORS is not access control** — it is a browser feature. Never confuse it with authentication.

**The concept that breaks integrations: the preflight.** For any request with a custom header, `Content-Type: application/json`, or `Authorization`, the browser first sends an `OPTIONS` request:

```
OPTIONS /api/v1/documents
Access-Control-Request-Method: GET
Access-Control-Request-Headers: x-workspace-id
```

The server must answer with `Access-Control-Allow-Origin` **and** list `x-workspace-id` in `Access-Control-Allow-Headers`. If it does not, **the browser never sends the real request at all** and you see a CORS error in the console. This is why `X-Workspace-ID` is explicitly listed in `allow_headers` — it is a header we invented in this phase, so it is the one most likely to be forgotten.

**Why credentials are off.** Axiom uses `Authorization: Bearer <token>`, not cookies. A bearer token is attached by your own code; the browser never adds it automatically. So `allow_credentials` is unnecessary, and turning it off means a wildcard origin can never become dangerous.

**Why a wildcard is rejected in configuration.** Read Starlette's source: with `allow_origins=["*"]` *and* credentials enabled, it does **not** error — it echoes the attacker's `Origin` back and sets `Access-Control-Allow-Credentials: true`. Any website could then make credentialed requests and read the responses. `Settings.cors_origin_list` refuses that combination outright.

---

## 6. The security model

| Threat | Defence | Where |
|---|---|---|
| Reading another workspace's data | Every query filters `workspace_id` **in SQL** | `app/api/routes/documents.py` |
| Guessing a document id from another workspace | `404`, not `403` (403 confirms existence) | `_load_document` |
| Attaching a chunk to the wrong workspace | Composite foreign key `(document_id, workspace_id)` | `app/db/models.py` |
| Forging a role | Roles come from `workspace_memberships`, never the client | `app/auth/dependencies.py` |
| Forging a token | HMAC signature verified with a pinned algorithm | `decode_access_token` |
| A token that never expires | `exp` is a *required* claim | `decode_access_token` |
| Using a token after the account is deleted | The user row is re-read on every request | `get_current_user` |
| A demoted admin keeping admin rights | Role re-read per request, not baked into the token | `get_current_membership` |
| Brute force | Redis rate limit, per account and per IP | `app/auth/rate_limit.py` |
| Passwords stolen from a database dump | bcrypt hashes, never plaintext | `hash_password` |
| Finding which emails exist | Identical response and timing for unknown email and wrong password | `authenticate_user` |
| Path traversal via a filename | Filename never builds a path; storage key is `{document_id}` | `app/storage/uploads.py` |
| A malicious PDF instructing the model | (Phase 5 — retrieved text is treated as evidence, not instructions) | not yet |
| Internal details leaking to the client | One error envelope; the catch-all logs details and returns a generic message | `app/api/errors.py` |
| A browser site calling the API | CORS restricted to configured origins | `app/main.py` |

---

## 7. The workspace isolation rule

> **The database is the authority. The client is never trusted.**

Concretely, four rules:

1. **Every user-data table has `workspace_id`.** Not "most" — every one.
2. **Every query filters on it in SQL.** Not "fetch, then filter in Python" — that leaks through wrong counts and wrong pagination.
3. **Foreign keys are composite.** `chunks` references `(document_id, workspace_id) → documents(id, workspace_id)`. The database itself refuses a chunk that points at another workspace's document. Application code can have a bug; this cannot.
4. **The client may *suggest* a workspace, never grant it.** The `X-Workspace-ID` header is checked against `workspace_memberships` before it is believed.

And one rule about *when*:

5. **Resolve the resource first, then the role.** So the answer depends on the resource, not on the asker.

---

## 8. Running it (Windows PowerShell)

```powershell
# 1. Environment
cd C:\Users\kioskUser0\Axiom
Copy-Item .env.example .env

# 2. Infrastructure
docker compose up -d postgres redis
docker compose ps          # wait for "healthy"

# 3. Dependencies
cd backend
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
cd ..

# 4. Schema
cd backend
python -m alembic upgrade head
cd ..

# 5. First administrator
cd backend
python -m app.auth.bootstrap --email admin@example.com --workspace "My Workspace"
cd ..

# 6. Run
docker compose up --build api worker
```

**Where things are:** API on `http://localhost:8000`, interactive docs on `http://localhost:8000/docs`, files under `backend/storage/`.

---

## 9. Verifying it

```powershell
cd backend

# The full suite (159 tests)
python -m pytest -q

# Point it at real infrastructure so the DB/Redis tests actually run
$env:DATABASE_URL = "postgresql+psycopg://axiom:axiom@localhost:5432/axiom"
$env:REDIS_URL   = "redis://localhost:6379/0"
python -m pytest -q
```

**A worked example** — prove isolation by hand:

```powershell
$login = Invoke-RestMethod -Method Post -Uri http://localhost:8000/api/v1/auth/login `
  -ContentType "application/json" -Body '{"email":"admin@example.com","password":"..."}'

# Who am I, according to the SERVER?
Invoke-RestMethod http://localhost:8000/api/v1/auth/me -Headers @{ Authorization = "Bearer $($login.access_token)" }
```

Try uploading a document, then ask for it with the *other* workspace's id. You get `404`, not `403`.

---

## 10. Design decisions and the alternatives that were rejected

| Decision | Rejected alternative | Why |
|---|---|---|
| Token carries only `user_id` | Put workspace + role in the JWT | A demotion would not take effect until the token expired |
| `404` for another workspace's resource | `403 Forbidden` | `403` confirms the id exists |
| Resource resolved before role | Role checked first (via `Depends`) | The answer would depend on the asker, not the resource |
| Passwords bcrypt, limit 72 bytes | Pre-hash with SHA-256 | More moving parts for a limit we can simply enforce and report |
| Rate limit counts failures | Counts attempts | One typo would lock a user out |
| Rate limit fails open | Fails closed | A Redis outage would become a total login outage |
| `X-Forwarded-For` ignored | Trust it | A client could spoof it to escape the per-IP limit |
| Credentials off in CORS | Credentials on | Bearer tokens do not need them, and off makes wildcard safe |
| No public registration | `POST /register` | Open signup is an abuse surface; an admin inviting a user is not |
| Composite FKs | App-level checks only | A bug in application code cannot break the database guarantee |
| Explicit `create_type=False` | `IF NOT EXISTS` on the ENUM | `IF NOT EXISTS` would *hide* a duplicate instead of fixing it |

---

## 11. A reading path

Read in this order. Each step builds on the previous.

1. **`app/main.py`** — the whole app in one file. See the three middlewares and the router registrations.
2. **`app/config.py`** — what the app is allowed to be told.
3. **`app/api/schemas.py`** — the shape of every message crossing the network.
4. **`app/api/errors.py`** — the failure contract, and the middleware-order subtlety.
5. **`app/auth/service.py`** — hashing, tokens, and the "same answer for unknown user and wrong password" rule.
6. **`app/auth/dependencies.py`** — the heart of authorization. Read `get_current_user` and `get_current_membership` slowly.
7. **`app/api/routes/auth.py`** — a small route that ties the service and the dependencies together.
8. **`app/api/routes/documents.py`** — the workspace isolation pattern, and the status checks.
9. **`app/api/routes/workspaces.py`** — the provisioning flow.
10. **`app/auth/rate_limit.py`** and **`app/auth/bootstrap.py`** — the two operational pieces.
11. **`app/db/models.py`** — the composite foreign keys that make isolation a database guarantee.
12. **The tests** — `backend/tests/` is the best documentation in the repo. Each test name states a rule in English.

**Then try this:** pick a test in `backend/tests/test_documents.py`, read the test, then read the route it exercises. Every test should be a sentence describing a rule the code enforces.

---

## 12. A glossary

| Term | Meaning |
|---|---|
| **JWT** | A signed JSON claim. Readable by anyone, trusted only because it is signed. |
| **Bearer token** | A token the client sends in the `Authorization: Bearer ...` header. |
| **bcrypt** | A deliberately slow password hash. Slow is the point: it makes guessing expensive. |
| **Hash vs encryption** | A hash is one-way; encryption is reversible with a key. Passwords need hashing. |
| **Workspace / tenant** | An organisation's isolated slice of the system. |
| **Dependency injection** | Passing what a function needs through the framework instead of constructing it inside. |
| **Middleware** | Code that wraps every request, before and after the route. |
| **Preflight (`OPTIONS`)** | The browser's permission check before a non-simple request. |
| **CORS** | The browser rule about which sites may read a response. Not access control. |
| **Composite foreign key** | A foreign key over several columns, so two tables must agree on *both*. |
| **Idempotent** | Safe to run twice. The same input produces the same result. |
| **Status code** | The HTTP number: 200 ok, 202 accepted, 400 bad input, 401 not authenticated, 403 not allowed, 404 not found, 409 conflict, 429 too many requests, 500 server error. |

---

## 13. What is deliberately *not* here yet

Being honest about the boundary of this phase:

- **No background processing.** Uploads are validated, stored and queued, but no worker consumes the queue yet. Documents sit in `queued` until Phase 4.
- **No answers.** There is no retrieval, no embedding, no LLM call. The `chunks` and `citations` tables exist and are correct, but nothing writes to them yet.
- **No conversations.** The tables exist; the `/chat` endpoints arrive in Phase 5.
- **Tokens cannot be revoked.** `POST /auth/logout` cannot invalidate a token — the server keeps no list. The client discards it. A token also stops working when it expires, and any change to the account takes effect immediately.
- **No refresh tokens.** Access tokens last 30 minutes and then you log in again.
- **No `audit_logs` table.** Document deletion writes a structured log line; the permanent audit trail is Phase 10.
- **Files are on local disk.** The `FileStorage` abstraction that allows an object store arrives in Phase 3.
- **Only PDF, and only a signature check.** `%PDF-` at the start. Page counts, character limits and timeouts arrive with the worker.

---

## 14. Where to go next

- **Phase 3 — Document Ingestion.** The `FileStorage` abstraction, checksum deduplication, and the upload lifecycle.
- **Phase 4 — Background Processing.** The Celery worker, the database claim, the sweeper, and retries. This is where `queued` documents start becoming `indexed`.
- **Phase 5 — PDF RAG MVP.** Parsing, chunking, embeddings, retrieval, cited answers and honest abstention.

The design documents that back all of this live in `docs/phase-1/`:
`01-user-stories.md`, `02-architecture.md`, `03-erd-api.md` (the API contract), `04-state-machine.md`, `05-job-contract.md`, `06-arabic-scope.md`, `07-decision-log.md`.

The evaluation foundation (a frozen corpus, a schema, and 20 seed questions) lives in `eval/v0.1/`.
