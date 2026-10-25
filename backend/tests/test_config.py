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

from hmac import compare_digest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from app.config import DEV_JWT_SECRET, ROOT_ENV_FILE, Settings


@pytest.fixture
def isolated_settings(monkeypatch):
    """Build Settings without the real .env file or machine environment.

    Every environment variable that maps to a Settings field is removed
    for the duration of the test. ``_env_file=None`` disables both the
    repository-root .env and a working-directory .env. Tests can still
    set a variable explicitly after this fixture has run.
    """

    for field_name in Settings.model_fields:
        monkeypatch.delenv(field_name.upper(), raising=False)

    def build(**overrides):
        return Settings(_env_file=None, **overrides)

    return build


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


# ---------------------------------------------------------------------------
# Phase 4 background processing
# ---------------------------------------------------------------------------

def test_the_phase4_defaults_match_the_job_contract():
    settings = Settings()

    assert settings.max_processing_attempts == 5
    assert settings.processing_retry_base_seconds == 30
    assert settings.processing_retry_max_seconds == 900
    assert settings.processing_retry_jitter_seconds == 10
    assert settings.sweeper_interval_seconds == 60
    assert settings.sweeper_uploaded_stale_seconds == 300
    assert settings.sweeper_queued_stale_seconds == 600
    assert settings.sweeper_retrying_stale_seconds == 600
    assert settings.sweeper_processing_stale_seconds == 900
    assert settings.sweeper_batch_size == 100
    assert settings.celery_task_soft_time_limit_seconds == 480
    assert settings.celery_task_time_limit_seconds == 600
    assert settings.celery_visibility_timeout_seconds == 1800


def test_empty_celery_urls_are_derived_from_redis_url(isolated_settings):
    settings = isolated_settings(
        redis_url="redis://localhost:6379/0",
        celery_broker_url="",
        celery_result_backend="",
    )

    assert settings.celery_broker_url == "redis://localhost:6379/1"
    assert settings.celery_result_backend == "redis://localhost:6379/2"


def test_rediss_url_derivation_preserves_credentials_host_port_query_and_fragment(
    isolated_settings,
):
    password = "test-password"
    settings = isolated_settings(
        redis_url=(
            f"rediss://user:{password}@redis.example.com:6380/0"
            "?ssl_cert_reqs=required#primary"
        ),
        celery_broker_url="",
        celery_result_backend="",
    )

    expected_paths = {
        settings.celery_broker_url: "/1",
        settings.celery_result_backend: "/2",
    }
    for url, expected_path in expected_paths.items():
        parsed = urlsplit(url)
        assert parsed.scheme == "rediss"
        assert parsed.username == "user"
        assert compare_digest(parsed.password or "", password)
        assert parsed.hostname == "redis.example.com"
        assert parsed.port == 6380
        assert parsed.path == expected_path
        assert parse_qs(parsed.query) == {"ssl_cert_reqs": ["required"]}
        assert parsed.fragment == "primary"


@pytest.mark.parametrize(
    "redis_url, expected_broker, expected_result",
    [
        (
            "redis://localhost:6379",
            "redis://localhost:6379/1",
            "redis://localhost:6379/2",
        ),
        (
            "redis://localhost:6379/",
            "redis://localhost:6379/1",
            "redis://localhost:6379/2",
        ),
        (
            "redis://localhost:6379?ssl_cert_reqs=required#primary",
            "redis://localhost:6379/1?ssl_cert_reqs=required#primary",
            "redis://localhost:6379/2?ssl_cert_reqs=required#primary",
        ),
    ],
)
def test_missing_or_root_database_paths_are_replaced(
    isolated_settings, redis_url, expected_broker, expected_result
):
    settings = isolated_settings(
        redis_url=redis_url,
        celery_broker_url="",
        celery_result_backend="",
    )

    assert settings.celery_broker_url == expected_broker
    assert settings.celery_result_backend == expected_result


def test_broker_override_does_not_change_result_derivation(
    isolated_settings,
):
    settings = isolated_settings(
        redis_url="redis://base.example:6379/0",
        celery_broker_url="rediss://broker.example:6380/9",
        celery_result_backend="",
    )

    assert settings.celery_broker_url == (
        "rediss://broker.example:6380/9"
    )
    assert settings.celery_result_backend == "redis://base.example:6379/2"


def test_result_override_does_not_change_broker_derivation(
    isolated_settings,
):
    settings = isolated_settings(
        redis_url="redis://base.example:6379/0",
        celery_broker_url="",
        celery_result_backend="redis://results.example:6379/4",
    )

    assert settings.celery_broker_url == "redis://base.example:6379/1"
    assert settings.celery_result_backend == (
        "redis://results.example:6379/4"
    )


@pytest.mark.parametrize(
    "redis_url",
    [
        "http://localhost:6379/0",
        "postgresql://localhost:6379/0",
    ],
)
def test_unsupported_redis_url_schemes_are_rejected(
    isolated_settings, redis_url
):
    with pytest.raises(ValueError) as exc:
        isolated_settings(
            redis_url=redis_url,
            celery_broker_url="",
            celery_result_backend="",
        )

    assert "REDIS_URL must use redis:// or rediss://" in str(exc.value)


@pytest.mark.parametrize(
    "redis_url",
    [
        "redis://",
        "redis://localhost:not-a-port/0",
        "redis://[::1/0",
        "not-a-url",
        "redis://local host:6379/0",
    ],
)
def test_malformed_redis_urls_are_rejected(isolated_settings, redis_url):
    with pytest.raises(ValueError) as exc:
        isolated_settings(
            redis_url=redis_url,
            celery_broker_url="",
            celery_result_backend="",
        )

    assert "REDIS_URL" in str(exc.value)


@pytest.mark.parametrize(
    "database_path",
    ["/not-a-number", "/-1", "/1.5", "/1/extra", "//"],
)
def test_invalid_redis_database_paths_are_rejected(
    isolated_settings, database_path
):
    with pytest.raises(ValueError) as exc:
        isolated_settings(
            redis_url=f"redis://localhost:6379{database_path}",
            celery_broker_url="",
            celery_result_backend="",
        )

    assert "REDIS_URL has an invalid Redis database path" in str(
        exc.value
    )


def test_celery_urls_can_come_from_the_environment(
    monkeypatch, isolated_settings
):
    monkeypatch.setenv(
        "CELERY_BROKER_URL", "rediss://broker.example:6380/3"
    )
    monkeypatch.setenv(
        "CELERY_RESULT_BACKEND", "redis://results.example:6379/5"
    )

    settings = isolated_settings()

    assert settings.celery_broker_url == "rediss://broker.example:6380/3"
    assert settings.celery_result_backend == (
        "redis://results.example:6379/5"
    )


@pytest.mark.parametrize(
    "override, message",
    [
        ({"max_processing_attempts": 0}, "MAX_PROCESSING_ATTEMPTS"),
        (
            {"processing_retry_base_seconds": 0},
            "PROCESSING_RETRY_BASE_SECONDS",
        ),
        (
            {"processing_retry_jitter_seconds": -1},
            "PROCESSING_RETRY_JITTER_SECONDS",
        ),
        (
            {
                "processing_retry_base_seconds": 100,
                "processing_retry_max_seconds": 99,
            },
            "PROCESSING_RETRY_MAX_SECONDS",
        ),
        (
            {"celery_task_soft_time_limit_seconds": 0},
            "CELERY_TASK_SOFT_TIME_LIMIT_SECONDS",
        ),
        (
            {
                "celery_task_soft_time_limit_seconds": 600,
                "celery_task_time_limit_seconds": 600,
            },
            "CELERY_TASK_SOFT_TIME_LIMIT_SECONDS",
        ),
        (
            {
                "celery_task_time_limit_seconds": 1800,
                "celery_visibility_timeout_seconds": 1800,
            },
            "CELERY_TASK_SOFT_TIME_LIMIT_SECONDS",
        ),
        (
            {"sweeper_processing_stale_seconds": 0},
            "SWEEPER_PROCESSING_STALE_SECONDS",
        ),
        (
            {"sweeper_processing_stale_seconds": 600},
            "SWEEPER_PROCESSING_STALE_SECONDS",
        ),
        ({"sweeper_interval_seconds": 0}, "SWEEPER_INTERVAL_SECONDS"),
        (
            {"sweeper_uploaded_stale_seconds": -1},
            "SWEEPER_UPLOADED_STALE_SECONDS",
        ),
        (
            {"sweeper_queued_stale_seconds": 0},
            "SWEEPER_QUEUED_STALE_SECONDS",
        ),
        (
            {"sweeper_retrying_stale_seconds": 0},
            "SWEEPER_RETRYING_STALE_SECONDS",
        ),
        ({"sweeper_batch_size": 0}, "SWEEPER_BATCH_SIZE"),
    ],
)
def test_invalid_phase4_settings_are_rejected(override, message):
    with pytest.raises(ValueError) as exc:
        Settings(**override)
    assert message in str(exc.value)


def test_equal_retry_base_and_maximum_is_accepted():
    settings = Settings(
        processing_retry_base_seconds=30,
        processing_retry_max_seconds=30,
    )
    assert settings.processing_retry_max_seconds == 30


def test_processing_stale_threshold_equal_to_the_hard_limit_is_rejected():
    with pytest.raises(ValueError) as exc:
        Settings(
            celery_task_time_limit_seconds=600,
            sweeper_processing_stale_seconds=600,
        )
    assert "must be greater than" in str(exc.value)
