"""Application configuration loaded from environment variables.

No secrets are stored in code. All configuration is provided through
environment variables or a .env file (excluded from git and the Docker
build context).

Which configuration applies depends on *where the process runs*:

* Inside Docker (the recommended workflow) the API and the worker get
  DATABASE_URL and REDIS_URL from the Compose service names, so they
  reach PostgreSQL at ``postgres:5432`` and Redis at ``redis:6379``
  over the Compose network.
* On the Windows host the same two variables must point at
  ``127.0.0.1``. Those ports are only reachable when the operator
  explicitly publishes them with ``docker-compose.host-dev.yml``.

Real environment variables always win over a .env file, and the .env
file is looked up at a fixed location (the repository root) so that the
configuration does not depend on the directory the process was started
from. Running ``python -m uvicorn app.main:app`` from the repository
root fails with "No module named 'app'"; running it from ``backend/``
must still pick up the same ``.env``.
"""

from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings

# Development-only fallback secret. It exists so a fresh clone can start
# without any setup, and it is rejected outside the "dev" environment
# (see Settings._reject_default_secret_outside_dev).
DEV_JWT_SECRET = "dev-only-insecure-jwt-secret-change-me"

# The repository root is two levels above this file:
#   backend/app/config.py -> backend/app -> backend -> <repo root>
# The .env file lives in the repository root (see .env.example), which is
# where the README tells operators to create it. Resolving the path from
# __file__ instead of from the current working directory means the same
# file is found whether the API is started from the repository root, from
# backend/, or from inside the container.
REPO_ROOT = Path(__file__).resolve().parents[2]
ROOT_ENV_FILE = REPO_ROOT / ".env"


class Settings(BaseSettings):
    # The default host is 127.0.0.1, not "localhost", on purpose.
    # "localhost" resolves to ::1 first on Windows, and a port that is
    # published on IPv4 only (which is what the Compose stack does)
    # leaves that first attempt hanging until the connect timeout
    # instead of being refused - a "Timeout connecting to server" that
    # looks like an outage but is only a name-resolution trap.
    database_url: str = "postgresql+psycopg://axiom:axiom@127.0.0.1:5432/axiom"
    redis_url: str = "redis://127.0.0.1:6379/0"
    env: str = "dev"

    # --- Database connection limits ---
    # How long a single TCP connection attempt to PostgreSQL may take.
    # Without a bound, an unreachable database blocks the request until
    # the operating system gives up, which is minutes on some networks.
    # The request then fails with a timeout instead of hanging.
    db_connect_timeout_seconds: int = 10
    # How long a request may wait for a free connection from the pool.
    # The same reasoning applies: a saturated pool must produce an
    # error, not an indefinite wait.
    db_pool_timeout_seconds: int = 30
    # How long an idle pooled connection may be reused before it is
    # discarded, in seconds. Guards against a database or firewall
    # dropping connections that the pool still considers usable.
    db_pool_recycle_seconds: int = 1800

    # --- Authentication (Phase 2) ---
    # Signing key for HS256 access tokens. MUST be overridden via the
    # JWT_SECRET_KEY environment variable outside local development.
    jwt_secret_key: str = DEV_JWT_SECRET
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: int = 30
    # bcrypt work factor. 12 is a reasonable default; higher is slower and
    # safer but makes login deliberately expensive.
    bcrypt_rounds: int = 12

    # --- Uploads (Phase 2, step 2.5) ---
    # Root directory for uploaded files. A relative path is resolved
    # against the backend package directory.
    storage_root: str = "storage"
    # Maximum accepted upload size in bytes (25 MiB).
    max_upload_bytes: int = 25 * 1024 * 1024

    # --- CORS (Phase 2, step 2.6) ---
    # Explicit browser origins allowed to call the API. Comma separated in
    # the CORS_ORIGINS environment variable. The default is the Vite dev
    # server used by the React frontend.
    cors_origins: str = "http://localhost:5173"
    # Axiom authenticates with a Bearer token, not a cookie, so the
    # browser never needs to attach credentials automatically. Leaving
    # this off means a wildcard origin can never be combined with
    # credentials.
    cors_allow_credentials: bool = False

    # --- Login rate limiting (Phase 2, step 2.7) ---
    # Failed logins allowed per identifier inside the window.
    login_rate_limit_max_attempts: int = 5
    # Length of the rate limiting window in seconds.
    login_rate_limit_window_seconds: int = 60
    # Failed logins allowed per source IP inside the window. Higher than
    # the per-account limit because many users can share one address
    # (office NAT, VPN).
    login_rate_limit_max_attempts_per_ip: int = 30
    # What the login endpoint does when the rate limiter cannot reach
    # Redis:
    #
    #   "open"   - the attempt is allowed through and the outage is
    #              logged. The correct-credentials check still applies,
    #              so only this one defence-in-depth layer is weakened.
    #              This is the default, because failing closed would turn
    #              a Redis outage into a total login outage.
    #   "closed" - the endpoint answers 503 and no login is attempted at
    #              all. That is the right choice for a deployment that
    #              treats brute-force protection as a hard requirement.
    #
    # The mode is explicit and logged at startup, so nobody has to guess
    # which posture is active.
    login_rate_limit_failure_mode: str = "open"

    model_config = {
        # The repository-root .env is the documented location. A .env in
        # the current working directory is read as well and takes
        # precedence, so a developer can override one setting locally
        # without editing the shared file.
        "env_file": (str(ROOT_ENV_FILE), ".env"),
        "env_file_encoding": "utf-8",
    }

    @model_validator(mode="after")
    def _reject_default_secret_outside_dev(self) -> "Settings":
        """Fail fast if the development secret is used outside development.

        A predictable signing key means anyone can mint valid access
        tokens. Refusing to start is much safer than silently running with
        a guessable secret.
        """
        if self.env.lower() != "dev" and self.jwt_secret_key == DEV_JWT_SECRET:
            raise ValueError(
                "JWT_SECRET_KEY must be set to a strong random value when "
                "ENV is not 'dev'."
            )
        return self

    @model_validator(mode="after")
    def _validate_failure_mode(self) -> "Settings":
        """Reject an unknown rate-limit failure mode.

        A typo must not silently fall back to the default posture: "fail
        open" and "fail closed" are opposite security decisions, and the
        operator has to make them explicitly.
        """
        mode = self.login_rate_limit_failure_mode.strip().lower()
        if mode not in ("open", "closed"):
            raise ValueError(
                "LOGIN_RATE_LIMIT_FAILURE_MODE must be 'open' or 'closed', "
                "got %r." % self.login_rate_limit_failure_mode
            )
        # Normalise the spelling so the rest of the code compares
        # against one value.
        object.__setattr__(self, "login_rate_limit_failure_mode", mode)
        return self

    @property
    def cors_origin_list(self) -> list[str]:
        """The configured CORS origins as a list.

        A wildcard is rejected here rather than passed to the middleware:
        Starlette echoes the request Origin back when a wildcard is
        combined with credentials, which lets any site make credentialed
        cross-origin requests.
        """
        origins = [
            origin.strip()
            for origin in self.cors_origins.split(",")
            if origin.strip()
        ]
        if "*" in origins and self.cors_allow_credentials:
            raise ValueError(
                "CORS_ORIGINS must not contain '*' while credentials are "
                "allowed. List the explicit origins instead."
            )
        return origins


settings = Settings()
