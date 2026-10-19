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
* **Fail open.** If Redis is unreachable the login is allowed through and
  a warning is logged. The alternative - failing closed - would turn a
  Redis outage into a complete login outage, which is a self-inflicted
  denial of service. The correct-credentials check still applies either
  way, so failing open only weakens this one defence in depth layer.
"""

import logging
from typing import Optional, Tuple

import redis

from app.config import settings

logger = logging.getLogger("axiom.ratelimit")

# Namespace for every key this module writes.
KEY_PREFIX = "axiom:ratelimit:login:"

_client: Optional[redis.Redis] = None


def get_client() -> Optional[redis.Redis]:
    """Return a Redis client, or None when Redis is not reachable.

    The client is created lazily so importing this module never opens a
    connection, and a failed connection never raises.
    """
    global _client
    if _client is None:
        try:
            client = redis.Redis.from_url(
                settings.redis_url,
                socket_connect_timeout=2,
                socket_timeout=2,
            )
            client.ping()
            _client = client
        except Exception as exc:  # noqa: BLE001 - boundary
            logger.warning("Rate limiting unavailable: %s", exc)
            return None
    return _client


def _key(scope: str, identifier: str) -> str:
    return "%s%s:%s" % (KEY_PREFIX, scope, identifier)


def _current_count(client: redis.Redis, key: str) -> int:
    try:
        raw = client.get(key)
        return int(raw) if raw else 0
    except Exception:  # noqa: BLE001 - boundary
        return 0


def is_blocked(scope: str, identifier: str, max_attempts: int) -> Tuple[bool, int]:
    """Return (blocked, retry_after_seconds).

    retry_after_seconds is 0 when the caller is not blocked. When Redis
    is unavailable the caller is never blocked (fail open).
    """
    client = get_client()
    if client is None:
        return (False, 0)

    key = _key(scope, identifier)
    count = _current_count(client, key)
    if count < max_attempts:
        return (False, 0)

    try:
        ttl = client.ttl(key)
    except Exception:  # noqa: BLE001 - boundary
        ttl = 0
    if ttl is None or ttl < 0:
        ttl = settings.login_rate_limit_window_seconds
    return (True, int(ttl))


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
        logger.warning("Could not record rate limit failure: %s", exc)


def reset(scope: str, identifier: str) -> None:
    """Clear the counter for the identifier (used after a successful login)."""
    client = get_client()
    if client is None:
        return
    try:
        client.delete(_key(scope, identifier))
    except Exception as exc:  # noqa: BLE001 - boundary
        logger.warning("Could not reset rate limit counter: %s", exc)


def check_login_allowed(email: str, ip_address: Optional[str]) -> Tuple[bool, int]:
    """Check both limits for a login attempt."""
    blocked, retry_after = is_blocked(
        "email", email.lower(), settings.login_rate_limit_max_attempts
    )
    if blocked:
        return (False, retry_after)

    if ip_address:
        blocked_ip, retry_after_ip = is_blocked(
            "ip", ip_address, settings.login_rate_limit_max_attempts_per_ip
        )
        if blocked_ip:
            return (False, retry_after_ip)

    return (True, 0)


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
