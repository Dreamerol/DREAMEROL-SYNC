"""End-to-end Broker tests against a mock GitHub OAuth provider."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
import requests
from fastapi.testclient import TestClient

import auth_broker.oauth as oauth_module
from auth_broker.app import create_app
from auth_broker.config import BrokerConfig, RateLimitConfig
from auth_broker.security import generate_polling_secret, hash_secret


CALLBACK_URL = "http://127.0.0.1:8000/auth/callback"
CLIENT_ID = "mock-client-id"
CLIENT_SECRET = "mock-client-secret"
ACCESS_TOKEN = "gho_mock_oauth_e2e_secret"


def _s256(value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


class MockGitHubOAuthProvider:
    """Minimal GitHub OAuth provider that validates PKCE and client credentials."""

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.access_token = ACCESS_TOKEN
        self.deny_next = False
        self.token_error_next = False
        self.authorization_requests: list[dict] = []
        self.token_requests: list[dict] = []
        self._codes: dict[str, dict[str, str]] = {}
        self._lock = threading.Lock()
        self._next_code = 1
        provider = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                provider._handle_get(self)

            def do_POST(self) -> None:
                provider._handle_post(self)

            def log_message(self, format: str, *args) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            daemon=True,
        )
        self._thread.start()

    @property
    def netloc(self) -> str:
        return f"127.0.0.1:{self._server.server_port}"

    @property
    def base_url(self) -> str:
        return f"http://{self.netloc}"

    @property
    def authorization_endpoint(self) -> str:
        return self.base_url + "/login/oauth/authorize"

    @property
    def token_endpoint(self) -> str:
        return self.base_url + "/login/oauth/access_token"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def _handle_get(self, handler: BaseHTTPRequestHandler) -> None:
        parsed = urlsplit(handler.path)
        if parsed.path != "/login/oauth/authorize":
            self._send_json(handler, 404, {"error": "not_found"})
            return

        params = parse_qs(parsed.query)
        client_id_ok = params.get("client_id") == [self.client_id]
        redirect_uri_ok = params.get("redirect_uri") == [self.redirect_uri]
        pkce_method = (params.get("code_challenge_method") or [None])[0]
        challenge = (params.get("code_challenge") or [""])[0]
        state = (params.get("state") or [""])[0]
        pkce_ok = pkce_method == "S256" and len(challenge) == 43
        if not (client_id_ok and redirect_uri_ok and pkce_ok and state):
            self._send_json(handler, 400, {"error": "invalid_request"})
            return

        denied = self.deny_next
        if denied:
            self.deny_next = False
        self.authorization_requests.append(
            {
                "client_id_ok": client_id_ok,
                "redirect_uri_ok": redirect_uri_ok,
                "pkce_method": pkce_method,
                "challenge": challenge,
                "state": state,
                "denied": denied,
            }
        )

        query = {"state": state}
        if denied:
            query["error"] = "access_denied"
        else:
            with self._lock:
                code = f"mock-code-{self._next_code}"
                self._next_code += 1
                self._codes[code] = {
                    "challenge": challenge,
                    "redirect_uri": self.redirect_uri,
                }
            query["code"] = code
        location = f"{self.redirect_uri}?{urlencode(query)}"
        self._redirect(handler, location)

    def _handle_post(self, handler: BaseHTTPRequestHandler) -> None:
        parsed = urlsplit(handler.path)
        if parsed.path != "/login/oauth/access_token":
            self._send_json(handler, 404, {"error": "not_found"})
            return

        try:
            length = int(handler.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        raw = handler.rfile.read(length).decode("utf-8")
        form = parse_qs(raw)
        client_id_ok = form.get("client_id") == [self.client_id]
        client_secret_ok = form.get("client_secret") == [self.client_secret]
        redirect_uri_ok = form.get("redirect_uri") == [self.redirect_uri]
        code = (form.get("code") or [""])[0]
        verifier = (form.get("code_verifier") or [""])[0]
        with self._lock:
            record = self._codes.pop(code, None)
        verifier_matches = bool(record) and hmac.compare_digest(
            record["challenge"],
            _s256(verifier),
        )
        self.token_requests.append(
            {
                "client_id_ok": client_id_ok,
                "client_secret_ok": client_secret_ok,
                "redirect_uri_ok": redirect_uri_ok,
                "code_present": bool(code),
                "code_verifier_matches_challenge": verifier_matches,
            }
        )

        if self.token_error_next:
            self.token_error_next = False
            self._send_json(handler, 400, {"error": "bad_verification_code"})
            return
        if not (
            client_id_ok
            and client_secret_ok
            and redirect_uri_ok
            and verifier_matches
        ):
            self._send_json(handler, 400, {"error": "invalid_grant"})
            return

        self._send_json(
            handler,
            200,
            {
                "access_token": self.access_token,
                "token_type": "bearer",
            },
        )

    @staticmethod
    def _send_json(handler: BaseHTTPRequestHandler, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Cache-Control", "no-store")
        handler.end_headers()
        handler.wfile.write(body)

    @staticmethod
    def _redirect(handler: BaseHTTPRequestHandler, location: str) -> None:
        handler.send_response(302)
        handler.send_header("Location", location)
        handler.send_header("Content-Length", "0")
        handler.send_header("Cache-Control", "no-store")
        handler.end_headers()


@pytest.fixture
def mock_provider():
    provider = MockGitHubOAuthProvider(
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        redirect_uri=CALLBACK_URL,
    )
    try:
        yield provider
    finally:
        provider.close()


@pytest.fixture
def broker_client(monkeypatch, mock_provider):
    config = BrokerConfig(
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        callback_url=CALLBACK_URL,
        request_timeout=5.0,
    )
    monkeypatch.setattr(
        oauth_module,
        "GITHUB_AUTHORIZATION_ENDPOINT",
        mock_provider.authorization_endpoint,
    )
    monkeypatch.setattr(
        oauth_module,
        "GITHUB_TOKEN_ENDPOINT",
        mock_provider.token_endpoint,
    )
    app = create_app(
        config,
        rate_limit_config=RateLimitConfig(enabled=False),
    )
    with TestClient(app) as client:
        yield client, mock_provider


def _start_flow(client):
    secret = generate_polling_secret()
    response = client.post(
        "/auth/start",
        json={"polling_secret_hash": hash_secret(secret)},
    )
    assert response.status_code == 200
    return secret, response.json()


def _authorize(auth_url: str) -> tuple[str, dict]:
    response = requests.get(auth_url, allow_redirects=False, timeout=5)
    assert response.status_code == 302
    location = urlsplit(response.headers["Location"])
    return response.headers["Location"], parse_qs(location.query)


def test_mock_github_oauth_full_flow(broker_client):
    client, provider = broker_client
    secret, start = _start_flow(client)
    auth_url = start["authorization_url"]
    parsed = urlsplit(auth_url)
    params = parse_qs(parsed.query)

    assert parsed.scheme == "http"
    assert parsed.netloc == provider.netloc
    assert parsed.path == "/login/oauth/authorize"
    assert params["client_id"] == [CLIENT_ID]
    assert params["redirect_uri"] == [CALLBACK_URL]
    assert params["code_challenge_method"] == ["S256"]
    assert len(params["code_challenge"][0]) == 43
    assert params["state"][0]
    assert CLIENT_SECRET not in auth_url

    location, callback_params = _authorize(auth_url)
    parsed_location = urlsplit(location)
    configured_callback = urlsplit(CALLBACK_URL)
    assert parsed_location.scheme == configured_callback.scheme
    assert parsed_location.netloc == configured_callback.netloc
    assert parsed_location.path == configured_callback.path
    assert callback_params["state"] == params["state"]
    code = callback_params["code"][0]

    assert len(provider.authorization_requests) == 1
    authorization = provider.authorization_requests[0]
    assert authorization["client_id_ok"] is True
    assert authorization["redirect_uri_ok"] is True
    assert authorization["pkce_method"] == "S256"
    assert authorization["challenge"] == params["code_challenge"][0]
    assert authorization["state"] == params["state"][0]
    assert authorization["denied"] is False

    callback = client.get(
        "/auth/callback",
        params={"state": params["state"][0], "code": code},
    )
    assert callback.status_code == 200
    assert "complete" in callback.text.lower()
    assert provider.access_token not in callback.text
    assert CLIENT_SECRET not in callback.text

    assert len(provider.token_requests) == 1
    token_request = provider.token_requests[0]
    assert token_request["client_id_ok"] is True
    assert token_request["client_secret_ok"] is True
    assert token_request["redirect_uri_ok"] is True
    assert token_request["code_present"] is True
    assert token_request["code_verifier_matches_challenge"] is True

    poll = client.post(
        "/auth/poll",
        json={"transaction_id": start["transaction_id"]},
        headers={"X-Polling-Secret": secret},
    )
    assert poll.status_code == 200
    assert poll.json() == {
        "state": "DELIVERY_AUTHORIZED",
        "access_token": provider.access_token,
        "token_type": "bearer",
    }

    ack = client.post(
        "/auth/ack",
        json={"transaction_id": start["transaction_id"]},
        headers={"X-Polling-Secret": secret},
    )
    assert ack.status_code == 200
    assert ack.json() == {"state": "ACKNOWLEDGED"}
    assert provider.access_token not in ack.text


def test_mock_github_oauth_access_denied_cancels_transaction(broker_client):
    client, provider = broker_client
    provider.deny_next = True
    secret, start = _start_flow(client)
    auth_url = start["authorization_url"]
    params = parse_qs(urlsplit(auth_url).query)

    _, callback_params = _authorize(auth_url)

    assert callback_params["error"] == ["access_denied"]
    assert callback_params["state"] == params["state"]
    assert provider.authorization_requests[0]["denied"] is True

    callback = client.get(
        "/auth/callback",
        params={
            "state": params["state"][0],
            "error": "access_denied",
        },
    )
    assert callback.status_code == 400
    assert "cancelled" in callback.text.lower()

    poll = client.post(
        "/auth/poll",
        json={"transaction_id": start["transaction_id"]},
        headers={"X-Polling-Secret": secret},
    )
    assert poll.status_code == 200
    assert poll.json() == {"state": "CANCELLED"}
    assert provider.token_requests == []


def test_mock_github_oauth_token_exchange_failure_fails_transaction(broker_client):
    client, provider = broker_client
    provider.token_error_next = True
    secret, start = _start_flow(client)
    auth_url = start["authorization_url"]
    params = parse_qs(urlsplit(auth_url).query)
    _, callback_params = _authorize(auth_url)

    callback = client.get(
        "/auth/callback",
        params={
            "state": params["state"][0],
            "code": callback_params["code"][0],
        },
    )
    assert callback.status_code == 502
    assert provider.access_token not in callback.text
    assert "bad_verification_code" not in callback.text
    assert provider.token_requests[0]["code_verifier_matches_challenge"] is True

    poll = client.post(
        "/auth/poll",
        json={"transaction_id": start["transaction_id"]},
        headers={"X-Polling-Secret": secret},
    )
    assert poll.status_code == 200
    assert poll.json() == {"state": "FAILED"}
