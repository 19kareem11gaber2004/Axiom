"""Application configuration loaded from environment variables.

No secrets are stored in code. All configuration is provided through
environment variables or a .env file (excluded from git and the Docker
build context).
"""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg://axiom:axiom@localhost:5432/axiom"
    redis_url: str = "redis://localhost:6379/0"
    env: str = "dev"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
