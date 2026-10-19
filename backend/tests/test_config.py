"""Tests for configuration loading and environment selection.

These guard a real trap: the application used to read its .env file
relative to the current working directory. The .env file lives in the
repository root, but the application package lives in backend/, so
starting the API from backend/ (which is required - "app" is not
importable from the repository root) silently ignored the .env file and
the process fell back to the built-in defaults.

The defaults point at localhost, which is correct for a process running
on the host and wrong for a process running in Docker, so a silently
ignored .env file is how an API container ends up talking to nothing.
"""

from pathlib import Path

import pytest
from app.config import DEV_JWT_SECRET, ROOT_ENV_FILE, Settings


def test_the_env_file_is_looked_up_in_the_repository_root():
    """The .env path must not depend on the working directory."""
    assert Path(ROOT_ENV_FILE) == Path(__file__).resolve().parents[2] / ".env"
    assert Path(ROOT_ENV_FILE).is_absolute()


def test_the_working_directory_env_file_is_also_loaded():
    """A local .env may override the shared one, but never the other way."""
    env_files = Settings.model_config["env_file"]
    assert str(ROOT_ENV_FILE) in env_files
    assert ".env" in env_files
    # The working-directory file comes last, so it wins.
    assert env_files.index(".env") > env_files.index(str(ROOT_ENV_FILE))


def test_values_are_read_from_an_env_file(tmp_path, monkeypatch):
    env_file = tmp_path / "test.env"
    env_file.write_text("CORS_ORIGINS=http://from-file.test\n", encoding="utf-8")
    settings = Settings(_env_file=str(env_file))
    assert settings.cors_origins == "http://from-file.test"


def test_a_real_environment_variable_beats_the_env_file(tmp_path, monkeypatch):
    """The environment always has the last word."""
    env_file = tmp_path / "test.env"
    env_file.write_text("CORS_ORIGINS=http://from-file.test\n", encoding="utf-8")
    monkeypatch.setenv("CORS_ORIGINS", "http://from-environment.test")
    settings = Settings(_env_file=str(env_file))
    assert settings.cors_origins == "http://from-environment.test"


def test_the_database_url_comes_from_the_environment_when_it_is_set(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@db:5432/x")
    settings = Settings()
    assert settings.database_url == "postgresql+psycopg://u:p@db:5432/x"


def test_the_redis_url_comes_from_the_environment_when_it_is_set(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://cache:6379/2")
    settings = Settings()
    assert settings.redis_url == "redis://cache:6379/2"


# ---------------------------------------------------------------------------
# Rate limit failure mode
# ---------------------------------------------------------------------------

def test_the_default_failure_mode_fails_open():
    settings = Settings()
    assert settings.login_rate_limit_failure_mode == "open"


def test_the_failure_mode_is_normalised():
    settings = Settings(login_rate_limit_failure_mode="  CLOSED ")
    assert settings.login_rate_limit_failure_mode == "closed"


def test_an_environment_variable_selects_the_failure_mode(monkeypatch):
    monkeypatch.setenv("LOGIN_RATE_LIMIT_FAILURE_MODE", "closed")
    assert Settings().login_rate_limit_failure_mode == "closed"


def test_an_unknown_failure_mode_is_rejected():
    """A typo must not silently fall back to a security posture."""
    with pytest.raises(ValueError) as exc:
        Settings(login_rate_limit_failure_mode="half-open")
    assert "LOGIN_RATE_LIMIT_FAILURE_MODE" in str(exc.value)


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------

def test_the_development_secret_is_rejected_outside_dev():
    with pytest.raises(ValueError) as exc:
        Settings(env="prod", jwt_secret_key=DEV_JWT_SECRET)
    assert "JWT_SECRET_KEY" in str(exc.value)


def test_a_strong_secret_is_accepted_outside_dev():
    settings = Settings(env="prod", jwt_secret_key="a-real-random-secret-value")
    assert settings.env == "prod"


# ---------------------------------------------------------------------------
# Connection limits
# ---------------------------------------------------------------------------

def test_the_connection_timeouts_are_positive():
    settings = Settings()
    assert settings.db_connect_timeout_seconds > 0
    assert settings.db_pool_timeout_seconds > 0
    assert settings.db_pool_recycle_seconds > 0


def test_the_timeouts_can_be_configured(monkeypatch):
    monkeypatch.setenv("DB_CONNECT_TIMEOUT_SECONDS", "3")
    monkeypatch.setenv("DB_POOL_TIMEOUT_SECONDS", "7")
    settings = Settings()
    assert settings.db_connect_timeout_seconds == 3
    assert settings.db_pool_timeout_seconds == 7


def test_the_database_target_hides_the_password(monkeypatch):
    """The human-readable target is logged at startup; it must be safe."""
    from app import main

    monkeypatch.setattr(
        main.settings,
        "database_url",
        "postgresql+psycopg://user:s3cret@db:5432/x",
    )
    target = main._database_target()
    assert "s3cret" not in target
    assert "db:5432/x" in target
