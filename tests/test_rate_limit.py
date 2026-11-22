import math
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from auth_broker.app import create_app
from auth_broker.config import BrokerConfig, RateLimitConfig
from auth_broker.oauth import GitHubOAuthClient
from auth_broker.rate_limit import (
    InMemoryRateLimiter,
    RateLimitPolicy,
    get_client_ip,
)
from auth_broker.security import generate_polling_secret, hash_secret
from auth_broker.transactions import TransactionStore


@pytest.fixture
def broker_config():
    return BrokerConfig(
        client_id="test-client-id",
        client_secret="test-client-secret",
        callback_url="https://broker.example.com/auth/callback",
        request_timeout=5.0,
    )


def make_app(broker_config, rate_limit_config, limiter):
    return create_app(
        broker_config,
        transaction_store=TransactionStore(ttl_seconds=600),
        oauth_client=GitHubOAuthClient(broker_config, post=Mock()),
        rate_limit_config=rate_limit_config,
        rate_limiter=limiter,
    )


def start_auth(client, secret=None):
    secret = secret or generate_polling_secret()
    response = client.post(
        "/auth/start",
        json={"polling_secret_hash": hash_secret(secret)},
    )
    return response, secret


def poll(client, transaction_id, secret):
    return client.post(
        "/auth/poll",
        json={"transaction_id": transaction_id},
        headers={"X-Polling-Secret": secret},
    )


def ack(client, transaction_id, secret):
    return client.post(
        "/auth/ack",
        json={"transaction_id": transaction_id},
        headers={"X-Polling-Secret": secret},
    )


def make_request(peer="203.0.113.10", forwarded=None):
    headers = []
    if forwarded is not None:
        headers.append((b"x-forwarded-for", forwarded.encode("ascii")))
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": headers,
            "client": (peer, 12345),
        }
    )


@pytest.mark.parametrize(
    "limit,window",
    [(0, 60), (-1, 60), (True, 60), (1, 0), (1, -1), (1, math.inf), (1, True)],
)
def test_rate_limit_policy_rejects_invalid_values(limit, window):
    with pytest.raises(ValueError):
        RateLimitPolicy(limit, window)


def test_limiter_allows_limit_then_blocks_and_resets():
    now = [1000.0]
    limiter = InMemoryRateLimiter(clock=lambda: now[0])
    policy = RateLimitPolicy(2, 10)

    assert limiter.record("key", policy) is None
    assert limiter.record("key", policy) is None
    assert limiter.record("key", policy) == pytest.approx(10)

    now[0] += 10.1
    assert limiter.record("key", policy) is None


def test_retry_after_does_not_record_requests():
    now = [1000.0]
    limiter = InMemoryRateLimiter(clock=lambda: now[0])
    policy = RateLimitPolicy(1, 10)

    assert limiter.record("key", policy) is None
    assert limiter.retry_after("key", policy) == pytest.approx(10)
    assert limiter.retry_after("key", policy) == pytest.approx(10)

    now[0] += 10.1
    assert limiter.retry_after("key", policy) is None
    assert limiter.record("key", policy) is None


def test_limiter_keys_are_independent():
    limiter = InMemoryRateLimiter(clock=lambda: 1000.0)
    policy = RateLimitPolicy(1, 60)

    assert limiter.record("first", policy) is None
    assert limiter.record("second", policy) is None
    assert limiter.record("first", policy) is not None


def test_limiter_cleanup_removes_expired_keys():
    now = [1000.0]
    limiter = InMemoryRateLimiter(clock=lambda: now[0])
    policy = RateLimitPolicy(1, 10)

    assert limiter.record("key", policy) is None
    now[0] += 11
    assert limiter.cleanup() == 1
    assert limiter.cleanup() == 0


def test_limiter_rejects_empty_keys():
    limiter = InMemoryRateLimiter()
    policy = RateLimitPolicy(1, 60)

    with pytest.raises(ValueError, match="key"):
        limiter.record("", policy)
    with pytest.raises(ValueError, match="key"):
        limiter.retry_after("", policy)


def test_rate_limit_config_defaults():
    config = RateLimitConfig()

    assert config.enabled is True
    assert config.window_seconds == 60.0
    assert config.start_per_ip == 60
    assert config.callback_per_ip == 120
    assert config.poll_per_ip == 240
    assert config.poll_per_transaction == 60
    assert config.ack_per_ip == 60
    assert config.ack_per_transaction == 10
    assert config.failures_per_ip == 30
    assert config.trusted_proxies == ()


def test_rate_limit_config_from_env_reads_all_values():
    config = RateLimitConfig.from_env(
        {
            "BROKER_RATE_LIMIT_ENABLED": "false",
            "BROKER_RATE_LIMIT_WINDOW_SECONDS": "30",
            "BROKER_RATE_LIMIT_START_PER_IP": "10",
            "BROKER_RATE_LIMIT_CALLBACK_PER_IP": "20",
            "BROKER_RATE_LIMIT_POLL_PER_IP": "30",
            "BROKER_RATE_LIMIT_POLL_PER_TRANSACTION": "40",
            "BROKER_RATE_LIMIT_ACK_PER_IP": "50",
            "BROKER_RATE_LIMIT_ACK_PER_TRANSACTION": "60",
            "BROKER_RATE_LIMIT_FAILURES_PER_IP": "70",
            "BROKER_TRUSTED_PROXIES": "127.0.0.1, 10.0.0.0/8",
        }
    )

    assert config.enabled is False
    assert config.window_seconds == 30.0
    assert config.start_per_ip == 10
    assert config.callback_per_ip == 20
    assert config.poll_per_ip == 30
    assert config.poll_per_transaction == 40
    assert config.ack_per_ip == 50
    assert config.ack_per_transaction == 60
    assert config.failures_per_ip == 70
    assert config.trusted_proxies == ("127.0.0.1", "10.0.0.0/8")


@pytest.mark.parametrize(
    "environ",
    [
        {"BROKER_RATE_LIMIT_ENABLED": "sometimes"},
        {"BROKER_RATE_LIMIT_WINDOW_SECONDS": "0"},
        {"BROKER_RATE_LIMIT_WINDOW_SECONDS": "nan"},
        {"BROKER_RATE_LIMIT_START_PER_IP": "0"},
        {"BROKER_RATE_LIMIT_POLL_PER_TRANSACTION": "not-an-int"},
        {"BROKER_TRUSTED_PROXIES": "not-an-ip"},
    ],
)
def test_rate_limit_config_rejects_invalid_environment_values(environ):
    with pytest.raises(ValueError):
        RateLimitConfig.from_env(environ)


def test_client_ip_ignores_forwarded_header_without_trusted_proxies():
    request = make_request(peer="203.0.113.10", forwarded="198.51.100.20")

    assert get_client_ip(request) == "203.0.113.10"


def test_client_ip_uses_forwarded_header_from_trusted_proxy():
    request = make_request(
        peer="10.0.0.1",
        forwarded="198.51.100.20, 10.0.0.2",
    )

    assert get_client_ip(request, ("10.0.0.0/8",)) == "198.51.100.20"


def test_client_ip_skips_invalid_forwarded_entries():
    request = make_request(
        peer="10.0.0.1",
        forwarded="not-an-ip, 198.51.100.20",
    )

    assert get_client_ip(request, ("10.0.0.0/8",)) == "198.51.100.20"


def test_client_ip_returns_first_forwarded_value_when_all_are_trusted():
    request = make_request(
        peer="10.0.0.1",
        forwarded="10.0.0.3, 10.0.0.2",
    )

    assert get_client_ip(request, ("10.0.0.0/8",)) == "10.0.0.3"


def test_start_rate_limit_returns_429_with_retry_after(broker_config):
    now = [1000.0]
    limiter = InMemoryRateLimiter(clock=lambda: now[0])
    rate_config = RateLimitConfig(start_per_ip=2, window_seconds=60)
    app = make_app(broker_config, rate_config, limiter)

    with TestClient(app) as client:
        assert start_auth(client)[0].status_code == 200
        assert start_auth(client)[0].status_code == 200
        response, _ = start_auth(client)

    assert response.status_code == 429
    assert response.json() == {"detail": "Too many requests. Try again later."}
    assert response.headers["retry-after"] == "60"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"


def test_callback_rate_limit_returns_html_429(broker_config):
    limiter = InMemoryRateLimiter(clock=lambda: 1000.0)
    rate_config = RateLimitConfig(callback_per_ip=1, window_seconds=60)
    app = make_app(broker_config, rate_config, limiter)

    with TestClient(app) as client:
        first = client.get("/auth/callback", params={"state": "invalid"})
        second = client.get("/auth/callback", params={"state": "invalid"})

    assert first.status_code == 400
    assert second.status_code == 429
    assert "Too many requests" in second.text
    assert second.headers["retry-after"] == "60"
    assert second.headers["cache-control"] == "no-store"


def test_poll_per_transaction_limit_does_not_block_other_transactions(broker_config):
    limiter = InMemoryRateLimiter(clock=lambda: 1000.0)
    rate_config = RateLimitConfig(
        poll_per_ip=100,
        poll_per_transaction=1,
        failures_per_ip=100,
        window_seconds=60,
    )
    app = make_app(broker_config, rate_config, limiter)

    with TestClient(app) as client:
        first_start, first_secret = start_auth(client)
        second_start, second_secret = start_auth(client)
        first_id = first_start.json()["transaction_id"]
        second_id = second_start.json()["transaction_id"]

        assert poll(client, first_id, first_secret).status_code == 200
        blocked = poll(client, first_id, first_secret)
        assert poll(client, second_id, second_secret).status_code == 200

    assert blocked.status_code == 429
    assert blocked.headers["retry-after"] == "60"


def test_poll_per_ip_limit_blocks_different_transactions(broker_config):
    limiter = InMemoryRateLimiter(clock=lambda: 1000.0)
    rate_config = RateLimitConfig(
        poll_per_ip=1,
        poll_per_transaction=100,
        failures_per_ip=100,
        window_seconds=60,
    )
    app = make_app(broker_config, rate_config, limiter)

    with TestClient(app) as client:
        first_start, first_secret = start_auth(client)
        second_start, second_secret = start_auth(client)
        first_id = first_start.json()["transaction_id"]
        second_id = second_start.json()["transaction_id"]

        assert poll(client, first_id, first_secret).status_code == 200
        blocked = poll(client, second_id, second_secret)

    assert blocked.status_code == 429


def test_repeated_invalid_poll_secrets_trigger_failure_limit(broker_config):
    limiter = InMemoryRateLimiter(clock=lambda: 1000.0)
    rate_config = RateLimitConfig(
        poll_per_ip=100,
        poll_per_transaction=100,
        failures_per_ip=1,
        window_seconds=60,
    )
    app = make_app(broker_config, rate_config, limiter)

    with TestClient(app) as client:
        start_response, secret = start_auth(client)
        transaction_id = start_response.json()["transaction_id"]

        invalid = poll(client, transaction_id, "wrong-secret")
        blocked = poll(client, transaction_id, secret)

    assert invalid.status_code == 401
    assert blocked.status_code == 429


def test_invalid_poll_secret_does_not_consume_per_transaction_limit(broker_config):
    limiter = InMemoryRateLimiter(clock=lambda: 1000.0)
    rate_config = RateLimitConfig(
        poll_per_ip=100,
        poll_per_transaction=1,
        failures_per_ip=100,
        window_seconds=60,
    )
    app = make_app(broker_config, rate_config, limiter)

    with TestClient(app) as client:
        start_response, secret = start_auth(client)
        transaction_id = start_response.json()["transaction_id"]

        invalid = poll(client, transaction_id, "wrong-secret")
        valid = poll(client, transaction_id, secret)

    assert invalid.status_code == 401
    assert valid.status_code == 200


def test_invalid_ack_secret_does_not_consume_per_transaction_limit(broker_config):
    limiter = InMemoryRateLimiter(clock=lambda: 1000.0)
    rate_config = RateLimitConfig(
        ack_per_ip=100,
        ack_per_transaction=1,
        failures_per_ip=100,
        window_seconds=60,
    )
    app = make_app(broker_config, rate_config, limiter)

    with TestClient(app) as client:
        start_response, secret = start_auth(client)
        transaction_id = start_response.json()["transaction_id"]

        invalid = ack(client, transaction_id, "wrong-secret")
        valid = ack(client, transaction_id, secret)

    assert invalid.status_code == 401
    assert valid.status_code == 409


def test_ack_per_transaction_limit_returns_429(broker_config):
    limiter = InMemoryRateLimiter(clock=lambda: 1000.0)
    rate_config = RateLimitConfig(
        ack_per_ip=100,
        ack_per_transaction=1,
        failures_per_ip=100,
        window_seconds=60,
    )
    app = make_app(broker_config, rate_config, limiter)

    with TestClient(app) as client:
        start_response, secret = start_auth(client)
        transaction_id = start_response.json()["transaction_id"]

        first = ack(client, transaction_id, secret)
        blocked = ack(client, transaction_id, secret)

    assert first.status_code == 409
    assert blocked.status_code == 429


def test_disabled_rate_limits_do_not_block_requests(broker_config):
    limiter = InMemoryRateLimiter(clock=lambda: 1000.0)
    rate_config = RateLimitConfig(enabled=False, start_per_ip=1)
    app = make_app(broker_config, rate_config, limiter)

    with TestClient(app) as client:
        assert start_auth(client)[0].status_code == 200
        assert start_auth(client)[0].status_code == 200


def test_trusted_proxy_limit_keys_by_forwarded_ip(broker_config):
    limiter = InMemoryRateLimiter(clock=lambda: 1000.0)
    rate_config = RateLimitConfig(
        start_per_ip=1,
        trusted_proxies=("10.0.0.0/8",),
        window_seconds=60,
    )
    app = make_app(broker_config, rate_config, limiter)

    with TestClient(app, client=("10.0.0.1", 12345)) as client:
        first = client.post(
            "/auth/start",
            json={"polling_secret_hash": hash_secret(generate_polling_secret())},
            headers={"X-Forwarded-For": "198.51.100.10"},
        )
        second = client.post(
            "/auth/start",
            json={"polling_secret_hash": hash_secret(generate_polling_secret())},
            headers={"X-Forwarded-For": "198.51.100.11"},
        )
        third = client.post(
            "/auth/start",
            json={"polling_secret_hash": hash_secret(generate_polling_secret())},
            headers={"X-Forwarded-For": "198.51.100.10"},
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert third.status_code == 429
