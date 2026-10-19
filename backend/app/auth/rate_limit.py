"""Redis-backed rate limiting for the login endpoint.

Design notes
------------
* **Failures are counted, not attempts.** A legitimate user who mistypes
  a password once is not punished; the counter is cleared on a successful
  login. Only repeated *failures* accumulate.
* **Two dimensions.** The limit is applied per account (stopping a
  targeted attack on one account) and per source IP (stopping
  credential-stuffing across many accounts from one host). The per-IP
  limit is higher because many users legitimately share one address.
* **Fixed window.** The TTL starts at the first failure and is not
  extended by later failures, so the maximum number of attempts in any
  window is bounded.
* **The outage posture is explicit.** When Redis cannot be reached the
  limiter does not decide silently. ``LOGIN_RATE_LIMIT_FAILURE_MODE``
  selects the behaviour:

  - ``open`` (default): the attempt is allowed through and the outage is
    logged once. The alternative - failing closed - would turn a Redis
    outage into a complete login outage, which is a self-inflicted
    denial of service. The correct-credentials check still applies
    either way, so failing open only weakens this one defence in depth
    layer.
  - ``closed``: no login is attempted while the limiter is unavailable
    and the endpoint answers 503. This is the right choice for a
    deployment that treats brute-force protection as a hard requirement.

  The active mode is logged at startup and reported by /ready, so the
  posture is never a matter of guesswork.
"""

import logging
import time
from typing import NamedTuple, Optional, Tuple

import redis

from app.config import settings

logger = logging.getLogger("axiom.ratelimit")

# Namespace for every key this module writes.
KEY_PREFIX = "axiom:ratelimit:login:"

# How long to wait before trying Redis again after a failure. Without this
# every call would pay the full connection timeout while Redis is down.
RECONNECT_BACKOFF_SECONDS = 30

_client: Optional[redis.Redis] = None
_next_attempt_at: float = 0.0
_redis_reported_down: bool = False


class LoginDecision(NamedTuple):
    """The outcome of a rate-limit check for a login attempt.

    Attributes:
        allowed: Whether the login may proceed.
        retry_after: Seconds the caller should wait before retrying.
        limiter_available: Whether the limiter could actually be
            consulted. False means Redis was unreachable, so the decision
            was made without it.
    """

    allowed: bool
    retry_after: int
    limiter_available: bool


def _report_down(exc: Exception) -> None:
    """Record that Redis is unreachable and log the outage once."""
    global _next_attempt_at, _redis_reported_down

    _next_attempt_at = time.monotonic() + RECONNECT_BACKOFF_SECONDS
    if not _redis_reported_down:
        logger.warning(
            "Rate limiting unavailable: %s. Login attempts are not being "
            "limited until Redis is reachable again.", exc
        )
        _redis_reported_down = True


def _report_up() -> None:
    """Record that Redis is reachable again."""
    global _next_attempt_at, _redis_reported_down

    _next_attempt_at = 0.0
    if _redis_reported_down:
        logger.info("Rate limiting is available again.")
        _redis_reported_down = False


def _drop_client() -> None:
    """Forget the cached client so the next call reconnects."""
    global _client

    client, _client = _client, None
    if client is not None:
        try:
            client.close()
        except Exception:  # noqa: BLE001 - best effort cleanup
            pass


def get_client() -> Optional[redis.Redis]:
    """Return a Redis client, or None when Redis is not reachable.

    The client is created lazily so importing this module never opens a
    connection, and a failed connection never raises.

    When Redis is down the failure is remembered for a short while, so a
    burst of requests does not each pay the connection timeout, and the
    warning is logged on the transition rather than on every call.
    """
    global _client, _next_attempt_at, _redis_reported_down

    if _client is not None:
        return _client

    now = time.monotonic()
    if now < _next_attempt_at:
        return None

    try:
        client = redis.Redis.from_url(
            settings.redis_url,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
        client.ping()
    except Exception as exc:  # noqa: BLE001 - boundary
        _report_down(exc)
        return None

    _client = client
    _report_up()
    return _client


def close_client() -> None:
    """Release the cached Redis client.

    Called when the application shuts down so the process does not keep
    a Redis connection open after the server stopped serving requests.
    """
    global _next_attempt_at, _redis_reported_down

    _drop_client()
    _next_attempt_at = 0.0
    _redis_reported_down = False


def _key(scope: str, identifier: str) -> str:
    return "%s%s:%s" % (KEY_PREFIX, scope, identifier)


def _current_count(client: redis.Redis, key: str) -> Tuple[int, bool]:
    """Return (count, ok).

    ok is False when Redis failed while the counter was being read. The
    counter is then not "zero", it is unknown, and the caller has to
    treat the limiter as unavailable rather than as an empty counter.
    """
    try:
        raw = client.get(key)
        return (int(raw) if raw else 0), True
    except Exception as exc:  # noqa: BLE001 - boundary
        # Redis became unreachable while the limiter was in use. Treat it
        # exactly like a connection failure: drop the client so the next
        # call reconnects, and report the outage.
        _drop_client()
        _report_down(exc)
        return 0, False


def _is_blocked(
    client: redis.Redis, scope: str, identifier: str, max_attempts: int
) -> Tuple[bool, int, bool]:
    """Return (blocked, retry_after_seconds, limiter_still_usable)."""
    key = _key(scope, identifier)
    count, ok = _current_count(client, key)
    if not ok:
        return (False, 0, False)
    if count < max_attempts:
        return (False, 0, True)

    try:
        ttl = client.ttl(key)
    except Exception as exc:  # noqa: BLE001 - boundary
        _drop_client()
        _report_down(exc)
        return (False, 0, False)
    if ttl is None or ttl < 0:
        ttl = settings.login_rate_limit_window_seconds
    return (True, int(ttl), True)


def is_blocked(scope: str, identifier: str, max_attempts: int) -> Tuple[bool, int]:
    """Return (blocked, retry_after_seconds).

    retry_after_seconds is 0 when the caller is not blocked. When Redis
    is unavailable the caller is never blocked (fail open).
    """
    client = get_client()
    if client is None:
        return (False, 0)

    blocked, retry_after, _usable = _is_blocked(
        client, scope, identifier, max_attempts
    )
    return (blocked, retry_after)


def record_failure(scope: str, identifier: str) -> None:
    """Record one failed attempt for the identifier."""
    client = get_client()
    if client is None:
        return

    key = _key(scope, identifier)
    try:
        pipe = client.pipeline()
        pipe.incr(key)
        # nx=True: only the first failure sets the TTL, so the window is
        # fixed rather than sliding.
        pipe.expire(key, settings.login_rate_limit_window_seconds, nx=True)
        pipe.execute()
    except Exception as exc:  # noqa: BLE001 - boundary
        _drop_client()
        _report_down(exc)
        logger.warning("Could not record rate limit failure: %s", exc)


def reset(scope: str, identifier: str) -> None:
    """Clear the counter for the identifier (used after a successful login)."""
    client = get_client()
    if client is None:
        return
    try:
        client.delete(_key(scope, identifier))
    except Exception as exc:  # noqa: BLE001 - boundary
        _drop_client()
        _report_down(exc)
        logger.warning("Could not reset rate limit counter: %s", exc)


def _unavailable_decision() -> LoginDecision:
    """The decision to apply when the limiter could not be consulted.

    The configured failure mode decides: "open" lets the attempt through
    (the credential check still applies), "closed" refuses it.
    """
    if settings.login_rate_limit_failure_mode == "closed":
        return LoginDecision(allowed=False, retry_after=0, limiter_available=False)
    return LoginDecision(allowed=True, retry_after=0, limiter_available=False)


def check_login_allowed(email: str, ip_address: Optional[str]) -> LoginDecision:
    """Check both limits for a login attempt.

    The result states explicitly whether the limiter could be consulted,
    so the caller can distinguish "you are blocked" from "the limiter is
    down" and answer accordingly.
    """
    client = get_client()
    if client is None:
        return _unavailable_decision()

    blocked, retry_after, usable = _is_blocked(
        client, "email", email.lower(), settings.login_rate_limit_max_attempts
    )
    if not usable:
        # Redis failed while the counter was being read.
        return _unavailable_decision()
    if blocked:
        return LoginDecision(
            allowed=False, retry_after=retry_after, limiter_available=True
        )

    if ip_address:
        blocked_ip, retry_after_ip, usable = _is_blocked(
            client,
            "ip",
            ip_address,
            settings.login_rate_limit_max_attempts_per_ip,
        )
        if not usable:
            return _unavailable_decision()
        if blocked_ip:
            return LoginDecision(
                allowed=False, retry_after=retry_after_ip, limiter_available=True
            )

    return LoginDecision(allowed=True, retry_after=0, limiter_available=True)


def record_login_failure(email: str, ip_address: Optional[str]) -> None:
    """Record a failed login against both the account and the source IP."""
    record_failure("email", email.lower())
    if ip_address:
        record_failure("ip", ip_address)


def reset_login_failures(email: str, ip_address: Optional[str]) -> None:
    """Clear both counters after a successful login."""
    reset("email", email.lower())
    if ip_address:
        reset("ip", ip_address)
