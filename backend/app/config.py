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


settings = Settings()
