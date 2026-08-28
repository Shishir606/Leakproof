from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, field

from redis import Redis
from redis.exceptions import RedisError


class RateLimitUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    retry_after_seconds: int = 0


class RedisRateLimiter:
    """Atomic rolling-window limiter backed by a Redis sorted set."""

    _SCRIPT = """
    local cutoff = tonumber(ARGV[1]) - tonumber(ARGV[2])
    redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', cutoff)
    if redis.call('ZSCORE', KEYS[1], ARGV[4]) then
      redis.call('EXPIRE', KEYS[1], math.ceil(tonumber(ARGV[2]) / 1000))
      return {1, 0}
    end
    local count = redis.call('ZCARD', KEYS[1])
    if count >= tonumber(ARGV[3]) then
      local oldest = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
      local retry = 1
      if #oldest == 2 then
        local reset_at = tonumber(oldest[2]) + tonumber(ARGV[2])
        retry = math.max(1, math.ceil((reset_at - tonumber(ARGV[1])) / 1000))
      end
      return {0, retry}
    end
    redis.call('ZADD', KEYS[1], tonumber(ARGV[1]), ARGV[4])
    redis.call('EXPIRE', KEYS[1], math.ceil(tonumber(ARGV[2]) / 1000))
    return {1, 0}
    """

    def __init__(self, redis_url: str) -> None:
        self._client = Redis.from_url(redis_url, decode_responses=True)

    def allow(
        self,
        scope: str,
        subject: str,
        *,
        limit: int,
        window_seconds: int,
        member: str | None = None,
        now: float | None = None,
    ) -> RateLimitDecision:
        now_ms = round((now if now is not None else time.time()) * 1000)
        member = member or secrets.token_urlsafe(12)
        try:
            allowed, retry_after = self._client.eval(
                self._SCRIPT,
                1,
                f"leakproof:limit:{scope}:{subject}",
                now_ms,
                window_seconds * 1000,
                limit,
                member,
            )
        except RedisError as exc:
            raise RateLimitUnavailable("rate-limit store is unavailable") from exc
        return RateLimitDecision(bool(allowed), int(retry_after))


@dataclass
class InMemoryRateLimiter:
    """Process-local implementation used only in simulation and unit tests."""

    entries: dict[tuple[str, str], dict[str, float]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def allow(
        self,
        scope: str,
        subject: str,
        *,
        limit: int,
        window_seconds: int,
        member: str | None = None,
        now: float | None = None,
    ) -> RateLimitDecision:
        current = now if now is not None else time.time()
        member = member or secrets.token_urlsafe(12)
        key = (scope, subject)
        with self._lock:
            window = self.entries.setdefault(key, {})
            cutoff = current - window_seconds
            for item, timestamp in list(window.items()):
                if timestamp <= cutoff:
                    del window[item]
            if member in window:
                return RateLimitDecision(True)
            if len(window) >= limit:
                retry_after = max(1, round(min(window.values()) + window_seconds - current))
                return RateLimitDecision(False, retry_after)
            window[member] = current
            return RateLimitDecision(True)
