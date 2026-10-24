# Axiom — Understanding Phase 2 (Backend Foundation)

> **Who this is for.** You have the project and you want to *understand* it, not just run it. This document explains what Phase 2 built, why each piece exists, and how to read the code.
>
> **How to use it.** Read §1–§3 first (the story). Then read the code in the order given in §11. Every section names the exact file and function so you can follow along in your editor. When you want to *prove* that you understand it, work through the six exercises in §14.
>
> The operational README stays in `README.md` (how to install and run). This one is about *why*.
>
> **Phase 3 update (2026-10-23).** The storage and upload code described below now lives in `app/storage/base.py`, `app/storage/local.py`, `app/storage/keys.py`, and `app/ingestion/service.py`; the former local upload helper was removed. See `docs/phase-3/README3.md` for the current ingestion contract.

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
| 2 endpoints (`/health`, `/ready`) | 13 versioned operations across 10 paths under `/api/v1` |
| No identity at all | JWT bearer tokens, 4 workspace roles |
| No tenant boundary | `workspace_id` on every table + composite foreign keys |
| 9 tables, no API to reach them | full document lifecycle: upload → list → read → download → retry → delete |
| Plain `{"detail": ...}` errors | one envelope for every failure: `{"error": {code, message, retryable, request_id}}` |
| Any client could call the API | CORS restricted to configured browser origins |
| Unlimited login attempts | Redis-backed rate limiting, per account and per IP |
| A worker container that exited with `ModuleNotFoundError` | a worker that connects to the broker and waits for work |
| Requests that could hang for minutes on an unreachable database | every connection attempt and every pool wait is bounded |
| 0 application tests | 226 tests at Phase 2 close; 351 today, against a real PostgreSQL and a real Redis |

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
    │  Redis unreachable → the configured failure mode decides:
    │  "open" (default) allows the attempt, "closed" answers 503.
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

## 5. The ten pieces, one at a time

### 5.1 `app/config.py` — configuration

**What it does.** Reads `DATABASE_URL`, `REDIS_URL`, `JWT_SECRET_KEY` and friends from the environment (or a `.env` file).

**The concept: never hard-code a secret.** If the signing key lives in the code, everyone who reads the repo can mint valid access tokens for your system. It comes from the environment, and `.env` is in `.gitignore`.

**Why this file has a validator.** Look at `_reject_default_secret_outside_dev`. There is a development default so a fresh clone runs with zero setup — but the moment `ENV` is anything other than `dev`, the app **refuses to start**:

```python
if self.env.lower() != "dev" and self.jwt_secret_key == DEV_JWT_SECRET:
    raise ValueError("JWT_SECRET_KEY must be set to a strong random value ...")
```

Failing fast is much better than silently running with a guessable key.

**Why the `.env` path is absolute.** Look at `model_config`:

```python
"env_file": (str(ROOT_ENV_FILE), ".env"),   # repository root first, CWD second
```

A relative `env_file` is resolved against the **current working directory**, and that is a trap: the `.env` file lives in the repository root, but `app` only imports from `backend/` (running uvicorn from the repository root fails with `No module named 'app'`). So a process started from `backend/` silently ignored the root `.env` and fell back to the built-in defaults — `localhost:5432` and `localhost:6379`, which the Docker stack does not publish. The result was an API that started, served `/docs`, and could reach neither PostgreSQL nor Redis. Resolving the path from `__file__` makes the configuration independent of where the process was started.

**The settings that keep a dependency outage bounded:**

| Setting | Default | What it bounds |
|---|---|---|
| `DB_CONNECT_TIMEOUT_SECONDS` | 10 | One TCP connection attempt to PostgreSQL |
| `DB_POOL_TIMEOUT_SECONDS` | 30 | The wait for a free pooled connection |
| `DB_POOL_RECYCLE_SECONDS` | 1800 | How long an idle pooled connection is reused |
| `LOGIN_RATE_LIMIT_FAILURE_MODE` | `open` | What the login endpoint does when Redis is unreachable |

The failure mode is validated: a typo (`half-open`) is **rejected at start-up**, because "fail open" and "fail closed" are opposite security decisions and nobody should get one by accident.

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

**Two codes that are easy to confuse.** `RATE_LIMITED` (429) means *you* did something the server will not accept yet — you failed too many logins, slow down. `DEPENDENCY_UNAVAILABLE` (503) means *the server* cannot do this right now — the rate limiter cannot reach Redis and the deployment is configured to fail closed. A server-side outage must never be reported as 429: that tells the caller to back off and retry, which will not help, and it hides a real outage behind a client error.

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

**A password can only be changed by one command.** `add_workspace_member` creates an account if the email is unknown and **never** overwrites the password of an account that already exists — overwriting would be an account-takeover vector. The only path that replaces a stored hash is `app/auth/password_reset.py`, an explicit administrative command (§5.9). That is worth knowing when a login returns 401: the credential check is honest, so a 401 means the supplied password does not match the stored hash, not that the account is missing or the database is unreachable.

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

**The concept: a requirement the schema does not declare does not exist.** Look at how the workspace header is declared:

```python
def get_current_membership(
    current_user: CurrentUser = Depends(get_current_user),
    requested_workspace: Optional[str] = Header(default=None, alias=WORKSPACE_HEADER),
    db: Session = Depends(get_db),
) -> CurrentMembership:
```

It could have been written as `request.headers.get(WORKSPACE_HEADER)` — the obvious way, and it works perfectly at runtime. But **FastAPI builds the OpenAPI schema from function signatures only**. A header read imperatively from the raw request never reaches the schema, so Swagger UI renders no field for it, the generated cURL omits it, and the only symptom a caller ever sees is a `403` they cannot diagnose. The header was read that way, and the documentation interface could not offer it.

Declaring it with `Header(alias=...)` is what puts it in the contract:

```json
{"name": "X-Workspace-ID", "in": "header", "required": false, "description": "..."}
```

Note `required: false`, on purpose. The header is *demanded* only when the account belongs to several workspaces; an account with exactly one membership is served without it, and the server selects that membership itself. Marking it required would force single-workspace clients to send a value they do not have.

The rule to take away: **if the server enforces it, the schema must declare it** — otherwise the client cannot comply. `backend/tests/test_openapi.py` is the test that keeps the two in step.

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

**`app/ingestion/service.py`, `app/storage/base.py`, `app/storage/local.py`, and `app/ingestion/validation.py`** handle uploads. The file goes to `storage/uploads/{document_id}/original.pdf` through the `FileStorage` port; the row is written in the same request, and the `202` is only returned after **both** are durable. The user's filename is never used to build a path (it is normalised and sanitised), so a filename like `../../etc/passwd.pdf` cannot escape the storage directory. Phase 3 adds workspace-scoped SHA-256 deduplication and returns `200` with an existing document when the bytes already exist in that workspace.

**`app/auth/bootstrap.py`** creates the very first administrator, because there is no public registration: adding members requires an admin, and initially there is none. It refuses to run when the database already has users, so it can never mint a second administrator unnoticed.

**`app/auth/password_reset.py`** is the way back in when an operator loses the administrator password. It updates the hash of an **existing** account and nothing else: the id, the email, the creation timestamp and every workspace membership survive; it never creates an account and never grants a role; it reads the password from a prompt that does not echo it (or from `AXIOM_ADMIN_PASSWORD` for non-interactive use) and never prints it.

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

**It fails open *by default*, and that is a decision, not an accident.** If Redis is unreachable, the limiter is bypassed and the outage is logged. The alternative — failing closed — would turn a Redis outage into a total login outage, which is a self-inflicted denial of service. The password check still applies either way; failing open only weakens one layer of defence.

**The posture is explicit and configurable.** `LOGIN_RATE_LIMIT_FAILURE_MODE` selects it, the value is logged at start-up, and an unknown value is rejected rather than silently defaulted:

| Mode | Redis unreachable | Why you would choose it |
|---|---|---|
| `open` (default) | The attempt is allowed through; the outage is logged once | A Redis outage must not become a login outage |
| `closed` | No login is attempted; the endpoint answers `503 DEPENDENCY_UNAVAILABLE` | Brute-force protection is a hard requirement of the deployment |

The limiter reports three things, not two. `LoginDecision` carries `allowed`, `retry_after` **and** `limiter_available`, so the endpoint can tell "you are blocked" (429) from "the limiter is down" (503) instead of guessing.

**An outage that starts *during* a request is an outage too.** A counter that cannot be read is *unknown*, not zero. If Redis dies between the connection and the `GET`, the limiter drops the client, applies the reconnect back-off and reports the outage — exactly as it does for a connection that never succeeded. Silently treating a failed read as "0 failures" would quietly disable the limiter for everybody, and nothing in the log would say so.

**`X-Forwarded-For` is deliberately ignored.** A client could spoof that header to escape the per-IP limit. Honouring it is a deployment concern, handled by the reverse proxy, not by the application.

---

### 5.8 `app/db/session.py` — surviving an unreachable database

**What it does.** Owns the connection pool and the session factory.

**The concept: every wait must be bounded.** Three timeouts, all configurable:

```python
engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,                                  # a closed connection is replaced, not reused
    pool_timeout=settings.db_pool_timeout_seconds,       # how long to wait for a free connection
    pool_recycle=settings.db_pool_recycle_seconds,       # how long an idle connection is trusted
    connect_args={"connect_timeout": ...},               # how long one TCP connection attempt may take
)
```

Without `connect_timeout`, an unreachable database does not fail — it *hangs*, until the operating system gives up, which on some networks is minutes. A request that hangs forever is far worse than a request that fails in ten seconds, because the caller cannot tell the difference between "slow" and "gone".

**The concept: a session is not thread-safe, and shutdown is where that bites.** FastAPI runs a synchronous route handler in a worker thread, and it tears the same request's dependencies down in *another* thread. When a request is cancelled — the client disconnects, or the server is stopped with Ctrl+C — the teardown can reach `Session.close()` while the handler is still blocked inside `_connection_for_bind()`, waiting for a connection. SQLAlchemy refuses that close:

```
sqlalchemy.exc.IllegalStateChangeError: Method 'close()' can't be called here;
method '_connection_for_bind()' is already in progress
```

and the SQLAlchemy error replaces the real one (`asyncio.CancelledError`), so the log shows a confusing internal error instead of "the request was cancelled while it was waiting for the database".

`close_session()` handles exactly that case, and only that case: it catches `IllegalStateChangeError`, logs it, and returns. The original `CancelledError` propagates, which is the truth. The fix is deliberately narrow — a broad `except Exception` around the teardown would hide real bugs, and masking a race is not the same as fixing it.

**The concept: the application owns its connections.** The lifespan handler disposes the engine and closes the cached Redis client on shutdown, so a stopped process does not keep sockets open.

---

### 5.9 `app/main.py` — the lifespan, and knowing what you are running

**What it does.** Registers the middlewares and the routers, serves `/health` and `/ready`, and owns the startup/shutdown hooks.

**The concept: the process should say what it is connected to.** The lifespan handler logs the effective configuration at start-up — the environment, the database host, the Redis host, the rate-limit posture — **without any secret**:

```
2026-10-19 22:39:51 INFO axiom.main: Starting Axiom API in the dev environment.
PostgreSQL=postgres:5432/axiom Redis=redis:6379 rate-limit-failure-mode=open
```

That one line answers the question that costs the most time to answer by hand: *which database is this process actually talking to?* It is the difference between "the API is broken" and "this API is configured for a different environment".

**The concept: `/health` and `/ready` answer different questions.** `/health` says the process is alive. `/ready` performs bounded connectivity checks against PostgreSQL and Redis and answers `503` when a dependency is down. A load balancer should use `/ready`; a supervisor should use `/health`. Mixing them up means either restarting a perfectly healthy process or routing traffic to a process that cannot serve.

---

### 5.10 CORS — the browser-only rule

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
| The limiter silently stops working | The outage is reported and the posture is explicit | `app/auth/rate_limit.py`, `LOGIN_RATE_LIMIT_FAILURE_MODE` |
| A request hanging forever on a dead dependency | Every connection attempt and pool wait is bounded | `app/db/session.py` |
| A cancelled request reported as an internal error | The teardown tolerates exactly one race and lets the real error through | `close_session` |
| Two API servers sharing port 8000 | The API is published on `127.0.0.1` and the rule is documented | `docker-compose.yml`, `docker-compose.host-dev.yml` |
| Passwords stolen from a database dump | bcrypt hashes, never plaintext | `hash_password` |
| Finding which emails exist | Identical response and timing for unknown email and wrong password | `authenticate_user` |
| Path traversal via a filename | Filename never builds a path; the key is `uploads/{document_id}/original.pdf` | `app/storage/keys.py`, `app/storage/local.py` |
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

# 3. Dependencies (only needed for a host-side run or for the tests)
cd backend
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
cd ..

# 4. Schema - inside the Compose network, so it uses the same database
#    as the API
docker compose run --rm api alembic upgrade head

# 5. First administrator
docker compose run --rm api python -m app.auth.bootstrap --email admin@example.com --workspace "My Workspace"

# 6. Run
docker compose up --build api worker
```

**Where things are:** API on `http://localhost:8000`, interactive docs on `http://localhost:8000/docs`, files under `backend/storage/`.

**Where the API runs matters.** The API and the worker are meant to run *inside* the Compose network, where PostgreSQL is `postgres:5432` and Redis is `redis:6379`. Those names do not resolve on the Windows host, and the default stack does not publish those ports, so a uvicorn process started on the host would find neither database nor Redis.

**One API process. Never two.** This is the trap that costs the most time on Windows. `asyncio` sets `SO_REUSEADDR` on its listening socket, and on Windows that flag lets a second process bind a port that is already in use. So a host uvicorn can "successfully start on port 8000" while the container is publishing the same port — no "address already in use", no warning. From then on, requests are split between two servers with **different configuration**: the container talks to `postgres:5432` and works, the host process talks to `localhost:5432` and times out. The symptom is maddening because it looks random.

The rule is simple: pick one. If you want the API on the host, stop the containerised one first.

```powershell
# publish PostgreSQL and Redis for a host-side process
docker compose -f docker-compose.yml -f docker-compose.host-dev.yml up -d postgres redis

# stop the containerised API: two servers on port 8000 is a silent
# mess on Windows, because the socket layer lets both bind
docker compose stop api

cd backend
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Two details worth checking, because they are how the trap is detected:

```powershell
# who is actually holding port 8000?
Get-NetTCPConnection -State Listen | Where-Object LocalPort -eq 8000

# is the API exposed to the local network? it should be 127.0.0.1 only
docker compose ps
```

The Compose file publishes the API as `127.0.0.1:8000:8000` for exactly this reason: a development server should not be reachable from the LAN.

The `app` package only imports from the `backend` directory, which is why
`python -m uvicorn app.main:app` fails with `No module named 'app'` when it
is run from the repository root. The `.env` file is read from the repository
root either way.

**Lost the administrator password?** There is no "forgot password" flow, by
design. Use the explicit administrative procedure, which updates the hash of
an existing account and preserves the account itself:

```powershell
docker compose run --rm api python -m app.auth.password_reset --email admin@example.com
```

It never creates an account, never grants a role, never prints the password,
and refuses to do anything for an address that does not exist.

---

## 9. Verifying it

```powershell
# The full suite, against the real infrastructure, inside the Compose
# network (the default stack does not publish PostgreSQL or Redis to
# the host, so a host-side run cannot reach them)
docker compose run --rm api sh -c "pip install --no-cache-dir -r requirements-dev.txt && python -m pytest -q"

# ...or on the host, after publishing the ports
docker compose -f docker-compose.yml -f docker-compose.host-dev.yml up -d postgres redis
cd backend
$env:DATABASE_URL = "postgresql+psycopg://axiom:axiom@127.0.0.1:5432/axiom"
$env:REDIS_URL   = "redis://127.0.0.1:6379/0"
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

**And prove the dependencies, from inside the stack:**

```powershell
docker compose exec redis redis-cli ping                                    # PONG
docker compose exec api   python -c "import psycopg; from app.db.session import psycopg_dsn; c=psycopg.connect(psycopg_dsn(), connect_timeout=3); print('postgres ok'); c.close()"
docker compose exec worker python -c "import redis; print('redis ok', redis.Redis.from_url('redis://redis:6379/0', socket_connect_timeout=3).ping())"
```

Run those from **inside** the containers, not from the host. The host has no PostgreSQL and no Redis — the default stack does not publish those ports on purpose — so a host-side probe tells you about your firewall, not about the application.

---

## 10. Design decisions and the alternatives that were rejected

| Decision | Rejected alternative | Why |
|---|---|---|
| Token carries only `user_id` | Put workspace + role in the JWT | A demotion would not take effect until the token expired |
| `404` for another workspace's resource | `403 Forbidden` | `403` confirms the id exists |
| Resource resolved before role | Role checked first (via `Depends`) | The answer would depend on the asker, not the resource |
| Passwords bcrypt, limit 72 bytes | Pre-hash with SHA-256 | More moving parts for a limit we can simply enforce and report |
| Rate limit counts failures | Counts attempts | One typo would lock a user out |
| Rate limit fails open by default | Fails closed | A Redis outage would become a total login outage. The mode is configurable (`LOGIN_RATE_LIMIT_FAILURE_MODE=closed`) and logged at startup, so the posture is a decision, not an accident |
| `503` when the limiter is down and failing closed | `429` | `429` blames the caller for a server-side outage |
| `.env` resolved from the repository root | `.env` relative to the CWD | The CWD and the file's location are different things; the API then silently uses the wrong addresses |
| Bounded connect and pool timeouts | Let the OS decide | An unreachable database must fail in seconds, not hang for minutes |
| A narrow `IllegalStateChangeError` guard in the session teardown | A broad `except Exception` | Masking a race is not fixing it; a broad handler hides real bugs |
| The API published on `127.0.0.1` only | `0.0.0.0` | A development server should not be reachable from the local network |
| PostgreSQL and Redis not published to the host | Publish them by default | The default stack has no reason to expose a database to the host; `docker-compose.host-dev.yml` publishes them for the documented host workflow |
| `AXIOM_`-prefixed variables in Compose | The bare `DATABASE_URL` | Compose also reads the app's `.env` for interpolation, and that file holds the *host* addresses |
| `X-Forwarded-For` ignored | Trust it | A client could spoof it to escape the per-IP limit |
| Credentials off in CORS | Credentials on | Bearer tokens do not need them, and off makes wildcard safe |
| No public registration | `POST /register` | Open signup is an abuse surface; an admin inviting a user is not |
| A password reset that only updates an existing account | A "forgot password" email flow, or a self-service reset | It is an explicit operator action; it cannot mint accounts or grant roles |
| Composite FKs | App-level checks only | A bug in application code cannot break the database guarantee |
| Explicit `create_type=False` | `IF NOT EXISTS` on the ENUM | `IF NOT EXISTS` would *hide* a duplicate instead of fixing it |

---

## 11. A reading path

Read in this order. Each step builds on the previous.

1. **`app/main.py`** — the whole app in one file. See the three middlewares, the router registrations and the lifespan.
2. **`app/config.py`** — what the app is allowed to be told, and where it reads it from.
3. **`app/api/schemas.py`** — the shape of every message crossing the network.
4. **`app/api/errors.py`** — the failure contract, and the middleware-order subtlety.
5. **`app/auth/service.py`** — hashing, tokens, and the "same answer for unknown user and wrong password" rule.
6. **`app/auth/dependencies.py`** — the heart of authorization. Read `get_current_user` and `get_current_membership` slowly.
7. **`app/api/routes/auth.py`** — a small route that ties the service and the dependencies together.
8. **`app/api/routes/documents.py`** — the workspace isolation pattern, and the status checks.
9. **`app/api/routes/workspaces.py`** — the provisioning flow.
10. **`app/auth/rate_limit.py`** — the limiter, the failure mode, and what an outage does to it.
11. **`app/db/session.py`** — the pool, the timeouts, and the session teardown.
12. **`app/auth/bootstrap.py`** and **`app/auth/password_reset.py`** — the two administrative commands.
13. **`app/worker/celery_app.py`** and **`app/worker/tasks.py`** — the worker, and why the second file must exist.
14. **`app/db/models.py`** — the composite foreign keys that make isolation a database guarantee.
15. **The tests** — `backend/tests/` is the best documentation in the repo. Each test name states a rule in English.

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
| **Fail open / fail closed** | What a component does when a dependency it needs is unavailable. *Open* lets the request through with reduced protection; *closed* refuses it. |
| **Lifespan** | The start-up and shutdown hooks of an ASGI application. |
| **Connection pool** | A set of kept-open database connections that requests borrow instead of dialling every time. |
| **`pool_pre_ping`** | A cheap "SELECT 1" before a pooled connection is handed out, so a connection the database closed while idle is replaced instead of used. |
| **`SO_REUSEADDR`** | A socket option that, on Windows, allows two processes to bind the same port — the reason two API servers can silently share port 8000. |
| **Status code** | The HTTP number: 200 ok, 202 accepted, 400 bad input, 401 not authenticated, 403 not allowed, 404 not found, 409 conflict, 429 too many requests, 500 server error, 503 service unavailable. |

---

## 13. What is deliberately *not* here yet

Being honest about the boundary of this phase:

- **No background processing.** The worker container now starts, connects to the broker and waits for work — but there are no task modules yet, so nothing is ever consumed. Documents sit in `queued` until Phase 4.
- **No answers.** There is no retrieval, no embedding, no LLM call. The `chunks` and `citations` tables exist and are correct, but nothing writes to them yet.
- **No conversations.** The tables exist; the `/chat` endpoints arrive in Phase 5.
- **Tokens cannot be revoked.** `POST /auth/logout` cannot invalidate a token — the server keeps no list. The client discards it. A token also stops working when it expires, and any change to the account takes effect immediately.
- **No refresh tokens.** Access tokens last 30 minutes and then you log in again.
- **No `audit_logs` table.** Document deletion writes a structured log line; the permanent audit trail is Phase 10.
- **Files are on local disk.** The `FileStorage` port and `LocalFileStorage` now exist (Phase 3), but an object-store adapter is still future work.
- **Only PDF, and only a signature check.** `%PDF-` at the start. Page counts, character limits and timeouts arrive with the worker.

---

## 14. A challenge

Reading a system is not the same as understanding it. These six exercises are built from real failures this codebase actually had — each one was found by running the thing, not by reading it. Do them in order; each one is a small, self-contained investigation.

**Rules of the game.** Do not change the schema, do not delete data, and do not "fix" anything by weakening a check. If a challenge tempts you to make a security control softer, that is the point where you have to stop and think.

---

### Challenge 1 — Two servers, one port

**The situation.** The API answers on `http://localhost:8000`, but some requests fail with "Timeout connecting to server" (Redis) or a PostgreSQL connection timeout, while `/docs` works fine. The symptom looks random.

**Your task.** Find out why the same URL behaves like two different servers.

**Where to look.** `docker-compose.yml` (what the API is published as), `app/config.py` (where `DATABASE_URL` comes from), and the operating system's own answer:

```powershell
Get-NetTCPConnection -State Listen | Where-Object LocalPort -eq 8000
Get-CimInstance Win32_Process | Where-Object { $_.Name -match 'python' }
```

**How to verify.** Start a second API on the host *while* the container is running:

```powershell
cd backend
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Then compare the two servers. Use your own machine's LAN address for the second one (`Get-NetIPAddress -AddressFamily IPv4`):

```powershell
(Invoke-WebRequest http://localhost:8000/ready -UseBasicParsing).Content      # the container
(Invoke-WebRequest http://192.168.100.33:8000/ready -UseBasicParsing).Content  # your host process
```

The container answers `{"status":"ready", ...}`. The host process answers **503**, because it cannot reach a database that is not published to the host.

**What a good answer says.** On Windows, `SO_REUSEADDR` (which asyncio sets) lets a second process bind a port that is already in use, so the host process starts *without an error*. The container reaches `postgres:5432` and `redis:6379`; the host process reaches `localhost:5432` and `localhost:6379`, which the default stack does not publish — so it times out. The fix is not "retry", it is **one API process at a time**, plus a configuration whose addresses match where the process actually runs.

---

### Challenge 2 — The honest 401

**The situation.** `POST /api/v1/auth/login` answers `401` for `admin@example.com`, and the account certainly exists.

**Your task.** Establish, with evidence, whether this is an infrastructure problem, a code problem or a credential problem — before you touch anything.

**Where to look.** `app/auth/service.py` (`authenticate_user`, `verify_password`), `app/api/routes/auth.py` (`login`), `app/db/session.py`.

**How to verify.**

```powershell
# is the hash even readable, and does a wrong password really fail?
docker compose exec api python -c "from sqlalchemy import select; from app.db.session import SessionLocal; from app.db.models import User; from app.auth.service import verify_password; s=SessionLocal(); u=s.execute(select(User)).scalars().first(); print(u.email, u.password_hash[:4], len(u.password_hash), verify_password('not-the-password', u.password_hash))"

# does the API talk to the right database?
docker compose exec api python -c "from sqlalchemy import select; from app.db.session import SessionLocal; from app.db.models import User; s=SessionLocal(); print([u.email for u in s.execute(select(User)).scalars()])"
```

**What a good answer says.** The stored value is a valid bcrypt hash; `verify_password` returns `False` for a wrong candidate and `True` for the right one; the API queries the database that holds the account. So the 401 is the *correct* answer to a wrong password — the credential check is honest. The fix is not to weaken the check and not to create a second administrator; it is the explicit procedure in `app/auth/password_reset.py`, which replaces the hash of an **existing** account and preserves its id, email and memberships.

---

### Challenge 3 — Change the posture, on purpose

**The situation.** The login rate limiter fails open by default: if Redis is unreachable, the limiter is bypassed and the outage is logged.

**Your task.** Make the deployment fail *closed* instead, and prove that the difference is visible to a client — and that it is a *server* problem, not a *caller* problem.

**Where to look.** `app/auth/rate_limit.py` (`check_login_allowed`, `LoginDecision`), `app/api/routes/auth.py` (`login`), `app/api/errors.py` (`DependencyUnavailableError`), `app/config.py` (`login_rate_limit_failure_mode`).

**How to verify.**

```powershell
# switch the posture to "closed" and restart the API with it
$env:AXIOM_LOGIN_RATE_LIMIT_FAILURE_MODE = "closed"
docker compose up -d --force-recreate api

# take Redis away
docker compose stop redis

# a login now cannot be limited, so it cannot be trusted
Invoke-WebRequest -Method Post -Uri http://localhost:8000/api/v1/auth/login `
  -ContentType "application/json" -Body '{"email":"admin@example.com","password":"wrong-password-1"}'

# put everything back
docker compose start redis
Remove-Item Env:AXIOM_LOGIN_RATE_LIMIT_FAILURE_MODE
docker compose up -d --force-recreate api
```

With `closed` the answer is **503 `DEPENDENCY_UNAVAILABLE`**. With `open` (the default) the same request reaches the credential check and answers **401**. Note that the password in the body must be at least 8 characters, or the request is rejected with 422 before any of this happens.

**What a good answer says.** In `open` mode the login proceeds and the credential check still decides the answer. In `closed` mode the endpoint answers **503 `DEPENDENCY_UNAVAILABLE`**, never 429: a 429 would tell the caller to back off and retry, which will not help, and it would disguise a server-side outage as a client error. An unknown mode value must be **rejected at start-up**, because "fail open" and "fail closed" are opposite security decisions and a typo must not pick one for you.

---

### Challenge 4 — The race at shutdown

**The situation.** Stopping the server while a request is waiting for the database produces:

```
asyncio.exceptions.CancelledError
sqlalchemy.exc.IllegalStateChangeError: Method 'close()' can't be called here;
method '_connection_for_bind()' is already in progress
```

**Your task.** Decide whether this is a harmless shutdown artefact or a real defect — then fix it *without* hiding it.

**Where to look.** `app/db/session.py` (`get_db`, `close_session`), and how FastAPI runs a synchronous handler (a worker thread) and a dependency teardown (possibly another thread).

**How to verify.** The race is reproducible without any timing luck — take the only pooled connection, then close the session from another thread. Run it inside the API container, which already has the right `DATABASE_URL`:

```python
# docker compose exec api python -
import threading, time
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.config import settings

engine = create_engine(settings.database_url, pool_size=1, max_overflow=0)
S = sessionmaker(bind=engine)
holder = S(); holder.execute(text("SELECT 1"))      # takes the only connection

db = S()
started = threading.Event()

def blocked():
    started.set()
    try:
        db.execute(text("SELECT 1"))               # blocks in the checkout
    except Exception:
        pass

t = threading.Thread(target=blocked); t.start()
started.wait(); time.sleep(0.3)
db.close()                                          # -> IllegalStateChangeError
holder.close(); t.join()
```

There is already a regression test for this: `backend/tests/test_db_session.py::test_closing_a_session_while_a_statement_is_in_flight_does_not_raise`.

**What a good answer says.** It is a real defect, but a *secondary* one: the underlying cause was an unbounded wait for a connection to a database that was not there. The fix is in two parts — bound every wait (`connect_timeout`, `pool_timeout`), and make the teardown tolerate exactly this one race so the original `CancelledError` is not replaced by a confusing internal error. Catching `IllegalStateChangeError` specifically is right; catching `Exception` would hide real bugs.

---

### Challenge 5 — The container that exited with code 1

**The situation.** `docker compose ps` shows the worker as `Exited (1)`. Nothing else in the system complains: the API is healthy, the tests pass, and no document ever moves out of `queued`.

**Your task.** Find the cause, fix it, and add a test that would have caught it *before* it reached a container.

**Where to look.** `docker compose logs worker`, `app/worker/celery_app.py` (the `include` list), and the `app/worker/` package.

**How to verify.**

```powershell
docker compose logs worker --tail 40
docker compose run --rm worker celery -A app.worker.celery_app worker --loglevel=info
```

**What a good answer says.** Celery imports every module in `include` before it starts; `app.worker.tasks` was listed but did not exist, so the worker died with `ModuleNotFoundError` — and because the API does not depend on the worker, nothing noticed. The regression test is `backend/tests/test_worker.py::test_every_included_module_exists`, which imports every module in the include list. The general lesson: **a container that exits quietly is a failure that no test reports unless something imports the same thing.**

---

### Challenge 6 — Write the rule as a test

**The situation.** Some of the rules in this document are only prose. Prose is not enforced.

**Your task.** Pick one rule that is currently *not* covered by a test and turn it into one. Good candidates:

- "the API must never answer a request with an unbounded wait" (assert the engine's timeouts are set);
- "a failed password reset must not change anything" (assert the hash and the id are untouched);
- "the rate limiter must not report a failed counter read as zero" (assert `limiter_available` is `False` when Redis fails mid-check);
- "the worker must be able to import everything it claims to" (already done — find a different one).

**How to verify.** Write the test so that it **fails** when you break the rule, and passes again when you restore it. A test that never fails proves nothing.

```powershell
docker compose run --rm api sh -c "pip install --no-cache-dir -r requirements-dev.txt && python -m pytest -q tests/test_your_new_file.py"
```

**What a good answer says.** The test name states the rule in English, the test needs no infrastructure that the rule does not need, and — most importantly — you can describe the exact change that makes it go red. If you cannot make it go red, it is not testing the rule.

---

**When you are done**, you should be able to answer these without looking anything up:

1. Which database is *this* process talking to, and how do you know?
2. What happens to a login when Redis is down — and who decided that?
3. Why did `admin@example.com` return 401, and what is the safe way to fix it?
4. What is the difference between a request that is slow and a request that is gone?
5. Where would you look first if the worker stopped consuming?

---

## 15. Where to go next

- **Phase 3 — Document Ingestion (completed).** The `FileStorage` port and local adapter, workspace-scoped SHA-256 deduplication, the upload lifecycle, and shared API/worker storage. See `docs/phase-3/README3.md`.
- **Phase 4 — Background Processing (next).** The Celery worker, the database claim, the sweeper, and retries. This is where `queued` documents start becoming `indexed`.
- **Phase 5 — PDF RAG MVP.** Parsing, chunking, embeddings, retrieval, cited answers and honest abstention.

The design documents that back all of this live in `docs/phase-1/`:
`01-user-stories.md`, `02-architecture.md`, `03-erd-api.md` (the API contract), `04-state-machine.md`, `05-job-contract.md`, `06-arabic-scope.md`, `07-decision-log.md`.

The evaluation foundation (a frozen corpus, a schema, and 20 seed questions) lives in `eval/v0.1/`.
