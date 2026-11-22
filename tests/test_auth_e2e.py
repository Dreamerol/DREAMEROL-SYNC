import socket
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
import uvicorn

import main
from auth_broker.app import create_app
from auth_broker.client import BrokerAuthClient
from auth_broker.config import BrokerConfig
from auth_broker.oauth import GitHubOAuthClient
from auth_broker.security import hash_secret


ACCESS_TOKEN = "gho_e2e_secret"


class FakeTokenResponse:
    status_code = 200
    headers = {"Content-Type": "application/json"}

    def json(self):
        return {"access_token": ACCESS_TOKEN, "token_type": "bearer"}


class RecordingBrokerAuthClient(BrokerAuthClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.starts = []
        self.polls = []
        self.acks = []

    def start(self, polling_secret_hash):
        result = super().start(polling_secret_hash)
        self.starts.append((polling_secret_hash, result))
        return result

    def poll(self, transaction_id, polling_secret):
        result = super().poll(transaction_id, polling_secret)
        self.polls.append((transaction_id, polling_secret, result))
        return result

    def ack(self, transaction_id, polling_secret):
        state = super().ack(transaction_id, polling_secret)
        self.acks.append((transaction_id, polling_secret, state))
        return state


class FakeAccounts:
    def __init__(self):
        self.tokens = []

    def add_account(self, token):
        self.tokens.append(token)
        return "e2e-user"


@pytest.fixture
def broker_server():
    config = BrokerConfig(
        client_id="e2e-client-id",
        client_secret="e2e-client-secret",
        callback_url="https://broker.example.com/auth/callback",
        request_timeout=5.0,
    )
    post = Mock(return_value=FakeTokenResponse())
    app = create_app(
        config,
        oauth_client=GitHubOAuthClient(config, post=post),
    )

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="error",
            access_log=False,
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        if not thread.is_alive():
            raise RuntimeError("Broker server failed to start")
        time.sleep(0.01)

    if not server.started:
        server.should_exit = True
        thread.join(timeout=5)
        raise RuntimeError("Broker server did not start in time")

    try:
        yield f"http://127.0.0.1:{port}", post
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        if thread.is_alive():
            server.force_exit = True
            thread.join(timeout=5)


def test_cli_login_end_to_end_with_real_broker(broker_server, monkeypatch):
    base_url, post = broker_server
    clients = []

    def client_factory(url):
        client = RecordingBrokerAuthClient(url)
        clients.append(client)
        return client

    monkeypatch.setattr(main, "BrokerAuthClient", client_factory)
    accounts = FakeAccounts()
    output = []
    errors = []
    callbacks = []

    def complete_authorization(_delay):
        authorization_url = next(
            line
            for line in output
            if line.startswith("https://github.com/login/oauth/authorize")
        )
        state = parse_qs(urlsplit(authorization_url).query)["state"][0]
        response = requests.get(
            base_url + "/auth/callback",
            params={"state": state, "code": "e2e-code"},
            timeout=5,
        )
        callbacks.append(response)

    result = main._run_broker_login(
        SimpleNamespace(broker_url=base_url),
        accounts,
        output=output.append,
        error=errors.append,
        sleep=complete_authorization,
        clock=time.monotonic,
    )

    assert result == 0
    assert errors == []
    assert len(callbacks) == 1
    assert callbacks[0].status_code == 200
    assert ACCESS_TOKEN not in callbacks[0].text
    assert accounts.tokens == [ACCESS_TOKEN]
    assert post.call_count == 1
    assert post.call_args.kwargs["data"]["code"] == "e2e-code"
    assert post.call_args.kwargs["data"]["code_verifier"]

    client = clients[0]
    polling_hash, start = client.starts[0]
    polling_secret = client.polls[0][1]
    assert polling_hash == hash_secret(polling_secret)
    assert client.acks == [(start.transaction_id, polling_secret, "ACKNOWLEDGED")]

    combined_output = "\n".join(output + errors)
    assert start.authorization_url in output
    assert "Account 'e2e-user' added" in combined_output
    assert ACCESS_TOKEN not in combined_output
    assert polling_secret not in combined_output

    verifier = BrokerAuthClient(base_url)
    try:
        after_ack = verifier.poll(start.transaction_id, polling_secret)
    finally:
        verifier.close()

    assert after_ack.state == "ACKNOWLEDGED"
    assert after_ack.access_token is None
