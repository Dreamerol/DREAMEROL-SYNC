"""End-to-end tests for the authentication Broker HTTP endpoints."""

import logging
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from fastapi.testclient import TestClient

from auth_broker.app import _CallbackAccessRedactor, create_app
from auth_broker.config import BrokerConfig, RateLimitConfig
from auth_broker.oauth import GITHUB_TOKEN_ENDPOINT, GitHubOAuthClient
from auth_broker.rate_limit import InMemoryRateLimiter
from auth_broker.security import (
    generate_polling_secret,
    hash_secret,
    pkce_s256_challenge,
)
from auth_broker.transactions import TransactionStore


VALID_ACCESS_TOKEN = "gho_broker_secret"
VALID_TOKEN_TYPE = "bearer"


class FakeResponse:
    def __init__(
        self,
        payload=None,
        *,
        status_code=200,
        content_type="application/json",
        json_error=None,
    ):
        self.status_code = status_code
        self.headers = {"Content-Type": content_type}
        self._payload = payload
        self._json_error = json_error

    def json(self):
        if self._json_error is not None:
            raise self._json_error
        return self._payload


def valid_payload():
    return {
        "access_token": VALID_ACCESS_TOKEN,
        "token_type": VALID_TOKEN_TYPE,
    }


@pytest.fixture
def config():
    return BrokerConfig(
        client_id="test-client-id",
        client_secret="test-client-secret",
        callback_url="https://broker.example.com/auth/callback",
        request_timeout=5.0,
    )


@pytest.fixture
def store():
    return TransactionStore(ttl_seconds=600)


@pytest.fixture
def post():
    return Mock(return_value=FakeResponse(valid_payload()))


@pytest.fixture
def client(config, store, post):
    app = create_app(
        config,
        transaction_store=store,
        oauth_client=GitHubOAuthClient(config, post=post),
    )
    with TestClient(app) as test_client:
        yield test_client


def start_auth(client, secret):
    response = client.post(
        "/auth/start",
        json={"polling_secret_hash": hash_secret(secret)},
    )
    assert response.status_code == 200
    payload = response.json()
    state = parse_qs(urlsplit(payload["authorization_url"]).query)["state"][0]
    return response, payload["transaction_id"], state


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


def callback(client, state, *, code=None, error=None):
    params = {"state": state}
    if code is not None:
        params["code"] = code
    if error is not None:
        params["error"] = error
    return client.get("/auth/callback", params=params)


def test_start_creates_pending_transaction_and_authorization_url(client, config):
    secret = generate_polling_secret()
    response, transaction_id, state = start_auth(client, secret)

    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"

    payload = response.json()
    parsed = urlsplit(payload["authorization_url"])
    params = parse_qs(parsed.query)
    assert parsed.scheme == "https"
    assert parsed.netloc == "github.com"
    assert parsed.path == "/login/oauth/authorize"
    assert params["client_id"] == [config.client_id]
    assert params["redirect_uri"] == [config.callback_url]
    assert params["state"] == [state]
    assert params["code_challenge_method"] == ["S256"]
    assert "scope" not in params
    assert "offline_access" not in params
    assert secret not in payload["authorization_url"]
    assert hash_secret(secret) not in payload["authorization_url"]
    assert config.client_secret not in payload["authorization_url"]

    pending = poll(client, transaction_id, secret)
    assert pending.status_code == 200
    assert pending.json() == {"state": "PENDING"}
    assert pending.headers["cache-control"] == "no-store"


def test_poll_and_ack_require_matching_polling_secret(client):
    secret = generate_polling_secret()
    _, transaction_id, _ = start_auth(client, secret)

    missing_poll = client.post(
        "/auth/poll",
        json={"transaction_id": transaction_id},
    )
    wrong_poll = poll(client, transaction_id, "wrong-secret")
    missing_ack = client.post(
        "/auth/ack",
        json={"transaction_id": transaction_id},
    )
    wrong_ack = ack(client, transaction_id, "wrong-secret")

    assert missing_poll.status_code == 401
    assert wrong_poll.status_code == 401
    assert missing_ack.status_code == 401
    assert wrong_ack.status_code == 401
    for response in (missing_poll, wrong_poll, missing_ack, wrong_ack):
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["pragma"] == "no-cache"
    assert "PENDING" not in wrong_poll.text
    assert "PENDING" not in wrong_ack.text


def test_start_rejects_invalid_polling_secret_hash(client):
    response = client.post(
        "/auth/start",
        json={"polling_secret_hash": "not-a-sha256-digest"},
    )

    assert response.status_code == 400
    assert response.json() == {"detail": "Invalid authentication request."}
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"


def test_healthz_reports_ok_without_exposing_config(client, config):
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    assert config.client_secret not in response.text


def test_unknown_auth_path_has_no_store_headers(client):
    response = client.get("/auth/unknown")

    assert response.status_code == 404
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_callback_success_delivers_token_and_ack_clears_it(client, post, config):
    secret = generate_polling_secret()
    start_response, transaction_id, state = start_auth(client, secret)
    authorization_url = start_response.json()["authorization_url"]

    callback_response = callback(client, state, code="auth-code")
    assert callback_response.status_code == 200
    assert "complete" in callback_response.text.lower()
    assert VALID_ACCESS_TOKEN not in callback_response.text
    assert state not in callback_response.text
    assert config.client_secret not in callback_response.text
    assert callback_response.headers["cache-control"] == "no-store"
    assert callback_response.headers["pragma"] == "no-cache"
    assert callback_response.headers["referrer-policy"] == "no-referrer"
    assert callback_response.headers["x-content-type-options"] == "nosniff"
    post.assert_called_once()

    call = post.call_args
    assert call.args[0] == GITHUB_TOKEN_ENDPOINT
    assert call.kwargs["headers"] == {"Accept": "application/json"}
    assert call.kwargs["timeout"] == 5.0
    assert call.kwargs["allow_redirects"] is False
    data = call.kwargs["data"]
    assert set(data) == {
        "client_id",
        "client_secret",
        "code",
        "redirect_uri",
        "code_verifier",
    }
    assert data["client_id"] == config.client_id
    assert data["client_secret"] == config.client_secret
    assert data["code"] == "auth-code"
    assert data["redirect_uri"] == config.callback_url
    challenge = parse_qs(urlsplit(authorization_url).query)["code_challenge"][0]
    assert pkce_s256_challenge(data["code_verifier"]) == challenge

    tokenless_poll = client.post(
        "/auth/poll",
        json={"transaction_id": transaction_id},
    )
    wrong_secret_poll = poll(client, transaction_id, "wrong-secret")
    assert tokenless_poll.status_code == 401
    assert wrong_secret_poll.status_code == 401

    first = poll(client, transaction_id, secret)
    assert first.status_code == 200
    assert first.json() == {
        "state": "DELIVERY_AUTHORIZED",
        "access_token": VALID_ACCESS_TOKEN,
        "token_type": VALID_TOKEN_TYPE,
    }
    assert config.client_secret not in first.text
    assert data["code_verifier"] not in first.text
    assert first.headers["cache-control"] == "no-store"

    second = poll(client, transaction_id, secret)
    assert second.status_code == 200
    assert second.json() == first.json()

    ack_response = ack(client, transaction_id, secret)
    assert ack_response.status_code == 200
    assert ack_response.json() == {"state": "ACKNOWLEDGED"}
    assert VALID_ACCESS_TOKEN not in ack_response.text
    assert ack_response.headers["cache-control"] == "no-store"

    after_ack = poll(client, transaction_id, secret)
    assert after_ack.status_code == 200
    assert after_ack.json() == {"state": "ACKNOWLEDGED"}
    assert VALID_ACCESS_TOKEN not in after_ack.text

    repeated_ack = ack(client, transaction_id, secret)
    assert repeated_ack.status_code == 200
    assert repeated_ack.json() == {"state": "ACKNOWLEDGED"}


def test_ack_before_delivery_is_rejected_without_changing_state(client):
    secret = generate_polling_secret()
    _, transaction_id, state = start_auth(client, secret)
    assert callback(client, state, code="auth-code").status_code == 200

    response = ack(client, transaction_id, secret)

    assert response.status_code == 409
    assert response.json() == {
        "detail": "ACK is allowed only after delivery is authorized."
    }
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    delivered = poll(client, transaction_id, secret)
    assert delivered.status_code == 200
    assert delivered.json()["access_token"] == VALID_ACCESS_TOKEN


def test_callback_access_denied_cancels_only_matching_transaction(client):
    secret_a = generate_polling_secret()
    secret_b = generate_polling_secret()
    _, transaction_a, state_a = start_auth(client, secret_a)
    _, transaction_b, _ = start_auth(client, secret_b)

    response = callback(client, state_a, error="access_denied")

    assert response.status_code == 400
    assert "cancelled" in response.text.lower()
    assert poll(client, transaction_a, secret_a).json() == {"state": "CANCELLED"}
    assert poll(client, transaction_b, secret_b).json() == {"state": "PENDING"}


def test_callback_other_error_fails_only_matching_transaction(client):
    secret_a = generate_polling_secret()
    secret_b = generate_polling_secret()
    _, transaction_a, state_a = start_auth(client, secret_a)
    _, transaction_b, _ = start_auth(client, secret_b)

    response = callback(client, state_a, error="server_error")

    assert response.status_code == 400
    assert poll(client, transaction_a, secret_a).json() == {"state": "FAILED"}
    assert poll(client, transaction_b, secret_b).json() == {"state": "PENDING"}


def test_callback_missing_code_fails_transaction(client, post):
    secret = generate_polling_secret()
    _, transaction_id, state = start_auth(client, secret)

    response = callback(client, state)

    assert response.status_code == 400
    assert poll(client, transaction_id, secret).json() == {"state": "FAILED"}
    post.assert_not_called()


def test_invalid_callback_state_does_not_modify_transaction(client, post):
    secret = generate_polling_secret()
    _, transaction_id, _ = start_auth(client, secret)

    response = callback(client, "invalid-state", code="auth-code")

    assert response.status_code == 400
    assert poll(client, transaction_id, secret).json() == {"state": "PENDING"}
    post.assert_not_called()


def test_duplicate_callback_does_not_exchange_twice(client, post):
    secret = generate_polling_secret()
    _, transaction_id, state = start_auth(client, secret)

    first = callback(client, state, code="auth-code")
    second = callback(client, state, code="auth-code")

    assert first.status_code == 200
    assert second.status_code == 400
    assert post.call_count == 1
    delivered = poll(client, transaction_id, secret)
    assert delivered.status_code == 200
    assert delivered.json()["access_token"] == VALID_ACCESS_TOKEN


def test_callback_token_exchange_failure_fails_transaction(client, post):
    post.side_effect = requests.ConnectionError("network failure detail")
    secret = generate_polling_secret()
    _, transaction_id, state = start_auth(client, secret)

    response = callback(client, state, code="auth-code")

    assert response.status_code == 502
    assert "network failure detail" not in response.text
    assert poll(client, transaction_id, secret).json() == {"state": "FAILED"}


def test_callback_token_exchange_failure_records_failure_limit(config, store, post):
    post.side_effect = requests.ConnectionError("network failure detail")
    limiter = InMemoryRateLimiter(clock=lambda: 1000.0)
    app = create_app(
        config,
        transaction_store=store,
        oauth_client=GitHubOAuthClient(config, post=post),
        rate_limit_config=RateLimitConfig(
            callback_per_ip=100,
            failures_per_ip=1,
            window_seconds=60,
        ),
        rate_limiter=limiter,
    )

    with TestClient(app) as client:
        secret = generate_polling_secret()
        _, _, state = start_auth(client, secret)

        first = callback(client, state, code="auth-code")
        second = callback(client, state, code="auth-code")

    assert first.status_code == 502
    assert second.status_code == 429
    assert "Too many requests" in second.text


def test_callback_invalid_token_response_fails_transaction(client, post):
    post.return_value = FakeResponse(
        {
            "access_token": "unexpected-token",
            "token_type": "bearer",
            "expires_in": 3600,
        }
    )
    secret = generate_polling_secret()
    _, transaction_id, state = start_auth(client, secret)

    response = callback(client, state, code="auth-code")

    assert response.status_code == 502
    assert "unexpected-token" not in response.text
    assert poll(client, transaction_id, secret).json() == {"state": "FAILED"}


def test_expired_transaction_cannot_be_polled_or_acked(config):
    now = [1000.0]
    store = TransactionStore(ttl_seconds=10, clock=lambda: now[0])
    post = Mock(return_value=FakeResponse(valid_payload()))
    app = create_app(
        config,
        transaction_store=store,
        oauth_client=GitHubOAuthClient(config, post=post),
    )

    with TestClient(app) as client:
        secret = generate_polling_secret()
        _, transaction_id, _ = start_auth(client, secret)
        now[0] += 11

        expired_poll = poll(client, transaction_id, secret)
        expired_ack = ack(client, transaction_id, secret)

    assert expired_poll.status_code == 200
    assert expired_poll.json() == {"state": "EXPIRED"}
    assert expired_ack.status_code == 410
    assert expired_ack.json() == {"detail": "Transaction has expired."}
    assert expired_ack.headers["cache-control"] == "no-store"
    assert expired_ack.headers["pragma"] == "no-cache"


def test_create_app_loads_dotenv_when_config_is_omitted(monkeypatch):
    for name in (
        "GITHUB_APP_CLIENT_ID",
        "GITHUB_APP_CLIENT_SECRET",
        "BROKER_CALLBACK_URL",
    ):
        monkeypatch.delenv(name, raising=False)

    def fake_load_dotenv():
        monkeypatch.setenv("GITHUB_APP_CLIENT_ID", "dotenv-client-id")
        monkeypatch.setenv("GITHUB_APP_CLIENT_SECRET", "dotenv-client-secret")
        monkeypatch.setenv(
            "BROKER_CALLBACK_URL",
            "http://127.0.0.1:8000/auth/callback",
        )

    monkeypatch.setattr("auth_broker.app.load_dotenv", fake_load_dotenv)

    app = create_app()

    assert app.state.broker_config.client_id == "dotenv-client-id"
    assert app.state.broker_config.client_secret == "dotenv-client-secret"
    assert (
        app.state.broker_config.callback_url
        == "http://127.0.0.1:8000/auth/callback"
    )


def test_create_app_skips_dotenv_when_config_is_explicit(monkeypatch, config):
    calls = []
    monkeypatch.setattr(
        "auth_broker.app.load_dotenv",
        lambda: calls.append(True),
    )

    app = create_app(config)

    assert calls == []
    assert app.state.broker_config is config


def test_auth_broker_sets_browser_security_headers(client):
    response = client.get("/healthz")

    assert response.headers["content-security-policy"].startswith("default-src 'self'")
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"


def test_access_log_redactor_hides_callback_secrets():
    record = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=(
            "127.0.0.1:1234",
            "GET",
            "/auth/callback?code=abc&state=xyz",
            "1.1",
            200,
        ),
        exc_info=None,
    )

    _CallbackAccessRedactor().filter(record)

    assert "abc" not in record.args[2]
    assert "xyz" not in record.args[2]
    assert "[REDACTED]" in record.args[2]
