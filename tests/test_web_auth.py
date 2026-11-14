from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from api.app import app
from api.auth import (
    SESSION_COOKIE,
    UnknownSession,
    WebAuthError,
    WebAuthManager,
)
from auth_broker.client import AuthPoll, AuthStart, BrokerAuthError
from auth_broker.security import hash_secret


class FakeBroker:
    def __init__(
        self,
        *,
        poll_results=(),
        start_result=None,
        start_error=None,
        ack_error=None,
    ):
        self.poll_results = list(poll_results)
        self.start_result = start_result or AuthStart(
            "transaction-1",
            "https://github.com/login/oauth/authorize?client_id=test",
        )
        self.start_error = start_error
        self.ack_error = ack_error
        self.start_hashes = []
        self.polls = []
        self.acks = []
        self.close_count = 0

    def start(self, polling_secret_hash):
        self.start_hashes.append(polling_secret_hash)
        if self.start_error is not None:
            raise self.start_error
        return self.start_result

    def poll(self, transaction_id, polling_secret):
        self.polls.append((transaction_id, polling_secret))
        if self.poll_results:
            return self.poll_results.pop(0)
        return AuthPoll("PENDING")

    def ack(self, transaction_id, polling_secret):
        self.acks.append((transaction_id, polling_secret))
        if self.ack_error is not None:
            raise self.ack_error
        return "ACKNOWLEDGED"

    def close(self):
        self.close_count += 1


class FakeGitHub:
    def __init__(self, *, user=None, error=None):
        self.session = Mock()
        self.user = user if user is not None else {"login": "demo-user"}
        self.error = error

    def get_authenticated_user(self):
        if self.error is not None:
            raise self.error
        return self.user


def build_manager(broker, github, clock):
    tokens = []

    def github_factory(token):
        tokens.append(token)
        return github

    manager = WebAuthManager(
        "http://127.0.0.1:8000",
        ttl_seconds=600,
        clock=clock,
        broker_client_factory=lambda url: broker,
        github_client_factory=github_factory,
    )
    return manager, tokens


def test_start_keeps_polling_secret_server_side_and_hashes_it():
    broker = FakeBroker()
    manager, _ = build_manager(broker, FakeGitHub(), lambda: 1000.0)

    session_id, authorization_url = manager.start()

    assert session_id
    assert authorization_url.startswith("https://github.com/")
    assert manager.token(session_id) is None
    assert len(broker.start_hashes[0]) == 64


def test_status_polls_validates_stores_and_acknowledges_credential():
    broker = FakeBroker(
        poll_results=[
            AuthPoll("PENDING"),
            AuthPoll(
                "DELIVERY_AUTHORIZED",
                access_token="github-token",
                token_type="bearer",
            ),
        ]
    )
    manager, tokens = build_manager(broker, FakeGitHub(), lambda: 1000.0)

    session_id, _ = manager.start()
    assert manager.status(session_id) == {"state": "PENDING"}
    assert manager.status(session_id) == {
        "state": "AUTHENTICATED",
        "account": "demo-user",
    }

    polling_secret = broker.polls[0][1]
    assert broker.start_hashes[0] == hash_secret(polling_secret)
    assert broker.acks == [("transaction-1", polling_secret)]
    assert manager.token(session_id) == "github-token"
    assert tokens == ["github-token"]

    assert manager.status(session_id) == {
        "state": "AUTHENTICATED",
        "account": "demo-user",
    }
    assert len(broker.polls) == 2


def test_invalid_github_credential_marks_session_failed_without_ack():
    broker = FakeBroker(
        poll_results=[
            AuthPoll(
                "DELIVERY_AUTHORIZED",
                access_token="invalid-token",
                token_type="bearer",
            )
        ]
    )
    github = FakeGitHub(error=RuntimeError("GitHub rejected the token"))
    manager, _ = build_manager(broker, github, lambda: 1000.0)

    session_id, _ = manager.start()
    with pytest.raises(WebAuthError):
        manager.status(session_id)

    assert broker.acks == []
    assert manager.token(session_id) is None
    assert manager.status(session_id) == {"state": "FAILED"}
    assert len(broker.polls) == 1
    github.session.close.assert_called_once_with()


def test_ack_failure_does_not_discard_valid_credential():
    broker = FakeBroker(
        poll_results=[
            AuthPoll(
                "DELIVERY_AUTHORIZED",
                access_token="github-token",
                token_type="bearer",
            )
        ],
        ack_error=BrokerAuthError("Broker unavailable"),
    )
    manager, _ = build_manager(broker, FakeGitHub(), lambda: 1000.0)

    session_id, _ = manager.start()

    assert manager.status(session_id)["state"] == "AUTHENTICATED"
    assert manager.token(session_id) == "github-token"


def test_terminal_broker_state_is_returned_without_repolling():
    broker = FakeBroker(poll_results=[AuthPoll("CANCELLED")])
    manager, _ = build_manager(broker, FakeGitHub(), lambda: 1000.0)

    session_id, _ = manager.start()

    assert manager.status(session_id) == {"state": "CANCELLED"}
    assert manager.status(session_id) == {"state": "CANCELLED"}
    assert len(broker.polls) == 1


def test_expired_session_is_discarded():
    now = [1000.0]
    broker = FakeBroker()
    manager, _ = build_manager(broker, FakeGitHub(), lambda: now[0])

    session_id, _ = manager.start()
    now[0] += manager.ttl_seconds

    with pytest.raises(UnknownSession):
        manager.status(session_id)
    assert manager.token(session_id) is None


def test_logout_discards_session():
    broker = FakeBroker()
    manager, _ = build_manager(broker, FakeGitHub(), lambda: 1000.0)
    session_id, _ = manager.start()

    manager.logout(session_id)
    manager.logout(None)

    with pytest.raises(UnknownSession):
        manager.status(session_id)


def test_broker_start_failure_is_reported():
    broker = FakeBroker(start_error=BrokerAuthError("Broker unavailable"))
    manager, _ = build_manager(broker, FakeGitHub(), lambda: 1000.0)

    with pytest.raises(WebAuthError, match="Broker unavailable"):
        manager.start()

    assert broker.close_count == 1


@pytest.mark.parametrize("ttl_seconds", [0, -1, True, float("nan"), float("inf"), "600"])
def test_invalid_session_ttl_is_rejected(ttl_seconds):
    with pytest.raises(ValueError):
        WebAuthManager(ttl_seconds=ttl_seconds)


def test_start_generates_distinct_session_ids():
    broker = FakeBroker()
    manager, _ = build_manager(broker, FakeGitHub(), lambda: 1000.0)

    first_session_id, _ = manager.start()
    second_session_id, _ = manager.start()

    assert first_session_id != second_session_id
    assert manager.token(first_session_id) is None
    assert manager.token(second_session_id) is None


class FakeManager:
    ttl_seconds = 600

    def __init__(self):
        self.start_result = (
            "browser-session",
            "https://github.com/login/oauth/authorize?client_id=test",
        )
        self.start_error = None
        self.status_result = {"state": "PENDING"}
        self.status_error = None
        self.token_value = "session-token"
        self.logout_sessions = []

    def start(self):
        if self.start_error is not None:
            raise self.start_error
        return self.start_result

    def status(self, session_id):
        if self.status_error is not None:
            raise self.status_error
        return dict(self.status_result)

    def token(self, session_id):
        if session_id == "browser-session":
            return self.token_value
        return None

    def logout(self, session_id):
        self.logout_sessions.append(session_id)


@pytest.fixture
def web_client():
    original_manager = app.state.auth_manager
    manager = FakeManager()
    app.state.auth_manager = manager
    try:
        with TestClient(app) as client:
            yield client, manager
    finally:
        app.state.auth_manager = original_manager


def test_auth_start_sets_httponly_same_site_cookie(web_client):
    client, manager = web_client

    response = client.post("/api/auth/start")

    assert response.status_code == 200
    assert response.json() == {
        "authorization_url": manager.start_result[1],
    }
    cookie = response.headers["set-cookie"]
    assert f"{SESSION_COOKIE}=browser-session" in cookie
    assert "HttpOnly" in cookie
    assert "Path=/" in cookie
    assert "SameSite=lax" in cookie
    assert "Max-Age=600" in cookie
    assert response.headers["cache-control"] == "no-store"


def test_auth_status_requires_a_session_and_clears_stale_cookie(web_client):
    client, manager = web_client
    manager.status_error = UnknownSession("missing")

    response = client.get("/api/auth/status")

    assert response.status_code == 401
    assert "Max-Age=0" in response.headers["set-cookie"]
    assert response.headers["cache-control"] == "no-store"


def test_auth_status_returns_authenticated_account(web_client):
    client, manager = web_client
    client.post("/api/auth/start")
    manager.status_result = {"state": "AUTHENTICATED", "account": "demo-user"}

    response = client.get("/api/auth/status")

    assert response.status_code == 200
    assert response.json() == {
        "state": "AUTHENTICATED",
        "account": "demo-user",
    }
    assert response.headers["cache-control"] == "no-store"


def test_auth_status_clears_cookie_after_terminal_state(web_client):
    client, manager = web_client
    client.post("/api/auth/start")
    manager.status_result = {"state": "EXPIRED"}

    response = client.get("/api/auth/status")

    assert response.status_code == 200
    assert response.json() == {"state": "EXPIRED", "account": None}
    assert "Max-Age=0" in response.headers["set-cookie"]
    assert response.headers["cache-control"] == "no-store"


def test_auth_start_failure_returns_bad_gateway_without_cookie(web_client):
    client, manager = web_client
    manager.start_error = WebAuthError("Broker unavailable")

    response = client.post("/api/auth/start")

    assert response.status_code == 502
    assert response.json()["detail"] == "Broker unavailable"
    assert "set-cookie" not in response.headers
    assert response.headers["cache-control"] == "no-store"


def test_auth_status_failure_returns_bad_gateway(web_client):
    client, manager = web_client
    client.post("/api/auth/start")
    manager.status_error = WebAuthError("Broker unavailable")

    response = client.get("/api/auth/status")

    assert response.status_code == 502
    assert response.json()["detail"] == "Broker unavailable"
    assert response.headers["cache-control"] == "no-store"


def test_logout_discards_session_and_cookie(web_client):
    client, manager = web_client
    client.post("/api/auth/start")

    response = client.post("/api/auth/logout")

    assert response.status_code == 200
    assert response.json() == {"state": "SIGNED_OUT"}
    assert manager.logout_sessions == ["browser-session"]
    assert "Max-Age=0" in response.headers["set-cookie"]
    assert response.headers["cache-control"] == "no-store"


def test_sync_uses_server_side_session_token(web_client, monkeypatch):
    client, _ = web_client
    client.post("/api/auth/start")

    tokens = []
    github = Mock()
    github.session = Mock()
    github.get_authenticated_user.return_value = {"login": "demo-user"}

    def github_factory(token):
        tokens.append(token)
        return github

    synchronizer = Mock()
    synchronizer.preview_all.return_value = [
        {"repository": "demo/fork", "status": "behind", "behind_by": 2}
    ]
    monkeypatch.setattr("api.routes.GitHubClient", github_factory)
    monkeypatch.setattr("api.routes.ForkSynchronizer", lambda client: synchronizer)

    response = client.post("/api/sync", json={"dry_run": True})

    assert response.status_code == 200
    assert response.json() == {
        "account": "demo-user",
        "total": 1,
        "results": [
            {"repository": "demo/fork", "status": "behind", "message": None, "behind_by": 2}
        ],
    }
    assert tokens == ["session-token"]
    github.session.close.assert_called_once_with()
    assert response.headers["cache-control"] == "no-store"


def test_sync_requires_authentication(web_client):
    client, _ = web_client

    response = client.post("/api/sync", json={"dry_run": True})

    assert response.status_code == 401
    assert response.headers["cache-control"] == "no-store"


def test_bearer_token_takes_precedence_over_session_cookie(web_client, monkeypatch):
    client, _ = web_client
    client.post("/api/auth/start")

    tokens = []
    github = Mock()
    github.session = Mock()
    github.get_authenticated_user.return_value = {"login": "demo-user"}

    def github_factory(token):
        tokens.append(token)
        return github

    synchronizer = Mock()
    synchronizer.preview_all.return_value = []
    monkeypatch.setattr("api.routes.GitHubClient", github_factory)
    monkeypatch.setattr("api.routes.ForkSynchronizer", lambda client: synchronizer)

    response = client.post(
        "/api/sync",
        json={"dry_run": True},
        headers={"Authorization": "Bearer explicit-token"},
    )

    assert response.status_code == 200
    assert tokens == ["explicit-token"]


def test_invalid_bearer_does_not_fall_back_to_session_cookie(web_client, monkeypatch):
    client, _ = web_client
    client.post("/api/auth/start")

    factory = Mock(side_effect=AssertionError("GitHub client should not be created"))
    monkeypatch.setattr("api.routes.GitHubClient", factory)

    response = client.post(
        "/api/sync",
        json={"dry_run": True},
        headers={"Authorization": "Basic abc"},
    )

    assert response.status_code == 401
    factory.assert_not_called()


@pytest.mark.parametrize(
    "path",
    ["/api/auth/start", "/api/auth/logout", "/api/sync"],
)
def test_state_changing_endpoints_reject_get(web_client, path):
    client, _ = web_client

    response = client.get(path)

    assert response.status_code == 405
    assert response.headers["cache-control"] == "no-store"


def test_auth_start_sets_secure_cookie_over_https():
    original_manager = app.state.auth_manager
    app.state.auth_manager = FakeManager()
    try:
        with TestClient(app, base_url="https://testserver") as client:
            response = client.post("/api/auth/start")
    finally:
        app.state.auth_manager = original_manager

    assert response.status_code == 200
    assert "Secure" in response.headers["set-cookie"]
    assert response.headers["cache-control"] == "no-store"
