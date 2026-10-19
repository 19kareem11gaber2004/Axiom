"""Application configuration loaded from environment variables.

No secrets are stored in code. All configuration is provided through
environment variables or a .env file (excluded from git and the Docker
build context).
"""

from pydantic import model_validator
from pydantic_settings import BaseSettings

# Development-only fallback secret. It exists so a fresh clone can start
# without any setup, and it is rejected outside the "dev" environment
# (see Settings._reject_default_secret_outside_dev).
DEV_JWT_SECRET = "dev-only-insecure-jwt-secret-change-me"


class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg://axiom:axiom@localhost:5432/axiom"
    redis_url: str = "redis://localhost:6379/0"
    env: str = "dev"

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

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

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
