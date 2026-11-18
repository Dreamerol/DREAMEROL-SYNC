"""In-memory rate limiting and client-IP helpers for the authentication Broker."""

from __future__ import annotations

import ipaddress
import math
import threading
import time
from collections import deque
from dataclasses import dataclass
from functools import lru_cache
from typing import Callable, Iterable

from starlette.requests import Request


@dataclass(frozen=True)
class RateLimitPolicy:
    """A sliding-window request limit."""

    limit: int
    window_seconds: float

    def __post_init__(self) -> None:
        if (
            isinstance(self.limit, bool)
            or not isinstance(self.limit, int)
            or self.limit <= 0
        ):
            raise ValueError("limit must be a positive integer")
        if (
            isinstance(self.window_seconds, bool)
            or not isinstance(self.window_seconds, (int, float))
            or not math.isfinite(self.window_seconds)
            or self.window_seconds <= 0
        ):
            raise ValueError("window_seconds must be a positive finite number")


class InMemoryRateLimiter:
    """Thread-safe sliding-window limiter for one Broker process."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        cleanup_threshold: int = 10_000,
    ) -> None:
        if (
            isinstance(cleanup_threshold, bool)
            or not isinstance(cleanup_threshold, int)
            or cleanup_threshold < 1
        ):
            raise ValueError("cleanup_threshold must be a positive integer")
        self._clock = clock
        self._events: dict[str, deque[float]] = {}
        self._windows: dict[str, float] = {}
        self._lock = threading.Lock()
        self._cleanup_threshold = cleanup_threshold
        self._operations = 0

    def retry_after(self, key: str, policy: RateLimitPolicy) -> float | None:
        """Return seconds until the key may proceed, without recording a request."""
        _require_key(key)
        now = self._clock()
        with self._lock:
            events = self._prune_locked(key, now, policy.window_seconds)
            if len(events) >= policy.limit:
                return max(0.0, events[0] + policy.window_seconds - now)
            return None

    def record(self, key: str, policy: RateLimitPolicy) -> float | None:
        """Record a request or return seconds until the key may proceed."""
        _require_key(key)
        now = self._clock()
        with self._lock:
            events = self._prune_locked(key, now, policy.window_seconds)
            if len(events) >= policy.limit:
                return max(0.0, events[0] + policy.window_seconds - now)
            events.append(now)
            self._operations += 1
            if (
                self._operations % 1000 == 0
                and len(self._events) > self._cleanup_threshold
            ):
                self._cleanup_locked(now)
            return None

    def cleanup(self) -> int:
        """Remove expired keys and return how many were removed."""
        now = self._clock()
        with self._lock:
            return self._cleanup_locked(now)

    def clear(self) -> None:
        """Remove all limiter state, primarily for tests."""
        with self._lock:
            self._events.clear()
            self._windows.clear()
            self._operations = 0

    def _prune_locked(
        self,
        key: str,
        now: float,
        window_seconds: float,
    ) -> deque[float]:
        events = self._events.get(key)
        if events is None:
            events = deque()
            self._events[key] = events
        self._windows[key] = window_seconds
        cutoff = now - window_seconds
        while events and events[0] <= cutoff:
            events.popleft()
        return events

    def _cleanup_locked(self, now: float) -> int:
        removed = 0
        for key, events in list(self._events.items()):
            window_seconds = self._windows.get(key, 60.0)
            cutoff = now - window_seconds
            while events and events[0] <= cutoff:
                events.popleft()
            if not events:
                del self._events[key]
                self._windows.pop(key, None)
                removed += 1
        return removed


def get_client_ip(request: Request, trusted_proxies: Iterable[str] = ()) -> str:
    """Resolve the client IP, trusting forwarded headers only from configured proxies."""
    peer = request.client.host if request.client else "unknown"
    networks = _trusted_networks(tuple(trusted_proxies))
    if not _is_trusted(peer, networks):
        return peer

    forwarded = request.headers.get("x-forwarded-for")
    if not forwarded:
        return peer

    candidates = [
        parsed
        for part in forwarded.split(",")
        if (parsed := _parse_ip(part.strip())) is not None
    ]
    for candidate in reversed(candidates):
        if not _is_trusted(str(candidate), networks):
            return str(candidate)
    if candidates:
        return str(candidates[0])
    return peer


@lru_cache(maxsize=32)
def _trusted_networks(
    values: tuple[str, ...],
) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    return tuple(ipaddress.ip_network(value, strict=False) for value in values)


def _parse_ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        parsed = ipaddress.ip_address(value)
    except ValueError:
        return None
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped:
        return parsed.ipv4_mapped
    return parsed


def _is_trusted(
    value: str,
    networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> bool:
    parsed = _parse_ip(value)
    if parsed is None:
        return False
    return any(parsed in network for network in networks)


def _require_key(key: str) -> None:
    if not isinstance(key, str) or not key:
        raise ValueError("key must be a non-empty string")


__all__ = [
    "InMemoryRateLimiter",
    "RateLimitPolicy",
    "get_client_ip",
]
