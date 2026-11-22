import pytest
import requests

from auth_broker.client import (
    DEFAULT_BROKER_URL,
    POLLING_SECRET_HEADER,
    BrokerAuthClient,
    BrokerAuthError,
)


class FakeResponse:
    def __init__(self, payload=None, *, status_code=200, json_error=None):
        self.status_code = status_code
        self._payload = payload
        self._json_error = json_error

    def json(self):
        if self._json_error is not None:
            raise self._json_error
        return self._payload


class FakeSession:
    def __init__(self, response=None, *, error=None):
        self.response = response
        self.error = error
        self.calls = []
        self.closed = False

    def post(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        if self.error is not None:
            raise self.error
        return self.response

    def close(self):
        self.closed = True


def make_client(payload=None, *, status_code=200, json_error=None, **kwargs):
    session = FakeSession(
        FakeResponse(
            payload,
            status_code=status_code,
            json_error=json_error,
        )
    )
    return BrokerAuthClient(
        kwargs.pop("base_url", DEFAULT_BROKER_URL),
        session=session,
        **kwargs,
    ), session


@pytest.mark.parametrize(
    "url",
    [
        "https://broker.example.com",
        "https://broker.example.com/",
        "http://127.0.0.1:8000",
        "http://localhost:8000",
        "http://[::1]:8000",
    ],
)
def test_broker_url_accepts_https_and_localhost_http(url):
    client = BrokerAuthClient(url)
    client.close()


@pytest.mark.parametrize(
    "url",
    [
        "",
        " http://127.0.0.1:8000",
        "http://broker.example.com",
        "ftp://broker.example.com",
        "https://user@broker.example.com",
        "https://user:password@broker.example.com",
        "https://broker.example.com?debug=1",
        "https://broker.example.com#fragment",
        "https://broker.example.com:bad",
        "https:///missing-host",
    ],
)
def test_broker_url_rejects_unsafe_values(url):
    with pytest.raises(BrokerAuthError, match="Broker URL"):
        BrokerAuthClient(url)


@pytest.mark.parametrize(
    "timeout",
    [0, -1, float("nan"), float("inf"), True, "10"],
)
def test_broker_timeout_must_be_positive_and_finite(timeout):
    with pytest.raises(BrokerAuthError, match="timeout"):
        BrokerAuthClient(DEFAULT_BROKER_URL, timeout=timeout)


def test_injected_session_is_not_closed_by_client():
    client, session = make_client({})

    client.close()

    assert session.closed is False


def test_start_posts_only_the_polling_secret_hash():
    payload = {
        "transaction_id": "transaction-1",
        "authorization_url": (
            "https://github.com/login/oauth/authorize?client_id=abc&state=xyz"
        ),
    }
    client, session = make_client(payload)

    result = client.start("a" * 64)

    assert result.transaction_id == "transaction-1"
    assert result.authorization_url == payload["authorization_url"]
    assert session.calls == [
        {
            "url": DEFAULT_BROKER_URL + "/auth/start",
            "json": {"polling_secret_hash": "a" * 64},
            "headers": {"Accept": "application/json"},
            "timeout": 10.0,
            "allow_redirects": False,
        }
    ]


@pytest.mark.parametrize(
    "authorization_url",
    [
        "http://github.com/login/oauth/authorize",
        "https://github.com.evil.example/login/oauth/authorize",
        "https://github.com:8443/login/oauth/authorize",
        "https://user@github.com/login/oauth/authorize",
        "https://github.com/login/oauth/authorize#fragment",
        "not-a-url",
    ],
)
def test_start_rejects_untrusted_authorization_urls(authorization_url):
    client, _ = make_client(
        {
            "transaction_id": "transaction-1",
            "authorization_url": authorization_url,
        }
    )

    with pytest.raises(BrokerAuthError):
        client.start("a" * 64)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"transaction_id": "", "authorization_url": "https://github.com/x"},
        {"transaction_id": "transaction-1"},
        {"authorization_url": "https://github.com/x"},
    ],
)
def test_start_rejects_invalid_response_fields(payload):
    client, _ = make_client(payload)

    with pytest.raises(BrokerAuthError):
        client.start("a" * 64)


def test_start_rejects_empty_polling_secret_hash_before_request():
    client, session = make_client({})

    with pytest.raises(ValueError, match="polling_secret_hash"):
        client.start("")

    assert session.calls == []


def test_poll_returns_delivery_credential_and_hides_it_from_repr():
    client, session = make_client(
        {
            "state": "DELIVERY_AUTHORIZED",
            "access_token": "gho_secret",
            "token_type": "Bearer",
        }
    )

    result = client.poll("transaction-1", "polling-secret")

    assert result.state == "DELIVERY_AUTHORIZED"
    assert result.access_token == "gho_secret"
    assert result.token_type == "bearer"
    assert "gho_secret" not in repr(result)
    assert session.calls[0]["headers"] == {
        "Accept": "application/json",
        POLLING_SECRET_HEADER: "polling-secret",
    }
    assert session.calls[0]["json"] == {"transaction_id": "transaction-1"}


def test_poll_pending_state_has_no_credential():
    client, _ = make_client({"state": "PENDING"})

    result = client.poll("transaction-1", "polling-secret")

    assert result.state == "PENDING"
    assert result.access_token is None
    assert result.token_type is None


@pytest.mark.parametrize(
    "payload",
    [
        {"state": "PENDING", "access_token": "gho_secret", "token_type": "bearer"},
        {"state": "DELIVERY_AUTHORIZED"},
        {"state": "DELIVERY_AUTHORIZED", "access_token": "", "token_type": "bearer"},
        {"state": "DELIVERY_AUTHORIZED", "access_token": "gho_secret", "token_type": "mac"},
        {},
    ],
)
def test_poll_rejects_invalid_credential_states(payload):
    client, _ = make_client(payload)

    with pytest.raises(BrokerAuthError):
        client.poll("transaction-1", "polling-secret")


def test_ack_returns_acknowledged_state():
    client, session = make_client({"state": "ACKNOWLEDGED"})

    assert client.ack("transaction-1", "polling-secret") == "ACKNOWLEDGED"
    assert session.calls[0]["headers"][POLLING_SECRET_HEADER] == "polling-secret"


def test_ack_rejects_unexpected_state():
    client, _ = make_client({"state": "PENDING"})

    with pytest.raises(BrokerAuthError, match="acknowledge"):
        client.ack("transaction-1", "polling-secret")


def test_http_401_reports_rejected_polling_secret():
    client, _ = make_client({}, status_code=401)

    with pytest.raises(BrokerAuthError, match="polling secret"):
        client.poll("transaction-1", "wrong-secret")


def test_http_error_reports_status_code():
    client, _ = make_client({}, status_code=500)

    with pytest.raises(BrokerAuthError, match="HTTP 500"):
        client.ack("transaction-1", "polling-secret")


def test_invalid_json_is_reported():
    client, _ = make_client(json_error=ValueError("not json"))

    with pytest.raises(BrokerAuthError, match="invalid JSON"):
        client.start("a" * 64)


def test_non_object_json_is_reported():
    client, _ = make_client([])

    with pytest.raises(BrokerAuthError, match="invalid response"):
        client.start("a" * 64)


def test_network_failure_is_reported_without_original_error():
    session = FakeSession(error=requests.ConnectionError("connection refused"))
    client = BrokerAuthClient(DEFAULT_BROKER_URL, session=session)

    with pytest.raises(BrokerAuthError, match="Could not reach"):
        client.start("a" * 64)
