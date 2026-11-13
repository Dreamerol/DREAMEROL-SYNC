"""Configuration for the authentication Broker's GitHub OAuth client."""

from __future__ import annotations

import ipaddress
import math
import os
from dataclasses import dataclass, field
from typing import Mapping
from urllib.parse import urlsplit


@dataclass(frozen=True)
class BrokerConfig:
    """Validated configuration for the Broker's GitHub OAuth client."""

    client_id: str
    client_secret: str = field(repr=False)
    callback_url: str
    request_timeout: float = 30.0

    def __post_init__(self) -> None:
        _require_non_empty_string(self.client_id, "client_id")
        _require_non_empty_string(self.client_secret, "client_secret")
        _validate_callback_url(self.callback_url)
        _validate_timeout(self.request_timeout)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "BrokerConfig":
        """Build configuration from required Broker environment variables."""
        env = os.environ if environ is None else environ
        required = {
            "client_id": "GITHUB_APP_CLIENT_ID",
            "client_secret": "GITHUB_APP_CLIENT_SECRET",
            "callback_url": "BROKER_CALLBACK_URL",
        }
        values: dict[str, str] = {}
        for field_name, env_name in required.items():
            try:
                values[field_name] = env[env_name]
            except KeyError:
                raise ValueError(
                    f"missing required environment variable: {env_name}"
                ) from None

        timeout_text = env.get("BROKER_REQUEST_TIMEOUT", "30")
        try:
            timeout = float(timeout_text)
        except (TypeError, ValueError):
            raise ValueError(
                "BROKER_REQUEST_TIMEOUT must be a positive finite number"
            ) from None

        return cls(
            client_id=values["client_id"],
            client_secret=values["client_secret"],
            callback_url=values["callback_url"],
            request_timeout=timeout,
        )


@dataclass(frozen=True)
class RateLimitConfig:
    """Configurable deployment limits for Broker abuse protection."""

    enabled: bool = True
    window_seconds: float = 60.0
    start_per_ip: int = 60
    callback_per_ip: int = 120
    poll_per_ip: int = 240
    poll_per_transaction: int = 60
    ack_per_ip: int = 60
    ack_per_transaction: int = 10
    failures_per_ip: int = 30
    trusted_proxies: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise ValueError("enabled must be a boolean")
        _validate_positive_finite(self.window_seconds, "window_seconds")
        for name in (
            "start_per_ip",
            "callback_per_ip",
            "poll_per_ip",
            "poll_per_transaction",
            "ack_per_ip",
            "ack_per_transaction",
            "failures_per_ip",
        ):
            _validate_positive_int(getattr(self, name), name)
        _validate_trusted_proxies(self.trusted_proxies)

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> "RateLimitConfig":
        """Build rate-limit settings from optional deployment environment variables."""
        env = os.environ if environ is None else environ
        return cls(
            enabled=_parse_bool(
                env.get("BROKER_RATE_LIMIT_ENABLED", "true"),
                "BROKER_RATE_LIMIT_ENABLED",
            ),
            window_seconds=_parse_positive_float(
                env.get("BROKER_RATE_LIMIT_WINDOW_SECONDS", "60"),
                "BROKER_RATE_LIMIT_WINDOW_SECONDS",
            ),
            start_per_ip=_parse_positive_int(
                env.get("BROKER_RATE_LIMIT_START_PER_IP", "60"),
                "BROKER_RATE_LIMIT_START_PER_IP",
            ),
            callback_per_ip=_parse_positive_int(
                env.get("BROKER_RATE_LIMIT_CALLBACK_PER_IP", "120"),
                "BROKER_RATE_LIMIT_CALLBACK_PER_IP",
            ),
            poll_per_ip=_parse_positive_int(
                env.get("BROKER_RATE_LIMIT_POLL_PER_IP", "240"),
                "BROKER_RATE_LIMIT_POLL_PER_IP",
            ),
            poll_per_transaction=_parse_positive_int(
                env.get("BROKER_RATE_LIMIT_POLL_PER_TRANSACTION", "60"),
                "BROKER_RATE_LIMIT_POLL_PER_TRANSACTION",
            ),
            ack_per_ip=_parse_positive_int(
                env.get("BROKER_RATE_LIMIT_ACK_PER_IP", "60"),
                "BROKER_RATE_LIMIT_ACK_PER_IP",
            ),
            ack_per_transaction=_parse_positive_int(
                env.get("BROKER_RATE_LIMIT_ACK_PER_TRANSACTION", "10"),
                "BROKER_RATE_LIMIT_ACK_PER_TRANSACTION",
            ),
            failures_per_ip=_parse_positive_int(
                env.get("BROKER_RATE_LIMIT_FAILURES_PER_IP", "30"),
                "BROKER_RATE_LIMIT_FAILURES_PER_IP",
            ),
            trusted_proxies=_parse_proxy_list(
                env.get("BROKER_TRUSTED_PROXIES", ""),
            ),
        )


def _require_non_empty_string(value: object, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


def _validate_callback_url(value: object) -> None:
    if not isinstance(value, str) or not value or any(ch.isspace() for ch in value):
        raise ValueError("callback_url must be a fixed HTTPS URL")
    if "*" in value:
        raise ValueError("callback_url must not contain wildcards")

    try:
        parsed = urlsplit(value)
        _ = parsed.port
        hostname = parsed.hostname
    except ValueError:
        raise ValueError("callback_url must be a fixed HTTPS URL") from None

    if (
        parsed.scheme.lower() != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("callback_url must be a fixed HTTPS URL")


def _validate_timeout(value: object) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError("request_timeout must be a positive finite number")


def _validate_positive_finite(value: object, name: str) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{name} must be a positive finite number")


def _validate_positive_int(value: object, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


def _validate_trusted_proxies(value: object) -> None:
    if not isinstance(value, tuple):
        raise ValueError("trusted_proxies must be a tuple of IP networks")
    for entry in value:
        if not isinstance(entry, str) or not entry.strip():
            raise ValueError("trusted_proxies must contain valid IP networks")
        try:
            ipaddress.ip_network(entry, strict=False)
        except ValueError:
            raise ValueError(
                "trusted_proxies must contain valid IP networks"
            ) from None


def _parse_bool(value: object, name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    raise ValueError(f"{name} must be a boolean")


def _parse_positive_int(value: object, name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a positive integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a positive integer") from None
    if parsed <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return parsed


def _parse_positive_float(value: object, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a positive finite number")
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a positive finite number") from None
    if not math.isfinite(parsed) or parsed <= 0:
        raise ValueError(f"{name} must be a positive finite number")
    return parsed


def _parse_proxy_list(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        items = value.split(",")
    else:
        try:
            items = list(value)
        except TypeError:
            raise ValueError(
                "BROKER_TRUSTED_PROXIES must be a comma-separated list"
            ) from None
    return tuple(str(item).strip() for item in items if str(item).strip())


__all__ = ["BrokerConfig", "RateLimitConfig"]
