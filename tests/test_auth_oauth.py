"""Tests for GitHub OAuth configuration, authorization, and token validation."""

from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import pytest
import requests

from auth_broker.config import BrokerConfig
from auth_broker.oauth import (
    GITHUB_AUTHORIZATION_ENDPOINT,
    GITHUB_TOKEN_ENDPOINT,
    GitHubOAuthClient,
    TokenExchangeError,
)
from auth_broker.security import pkce_s256_challenge
from auth_broker.token_policy import TokenValidationError, validate_token_response


class FakeResponse:
    def __init__(
        self,
        payload=None,
        *,
        status_code=200,
        content_type="application/json",
        headers=None,
        json_error=None,
    ):
        self.status_code = status_code
        if headers is None:
            headers = {}
            if content_type is not None:
                headers["Content-Type"] = content_type
        self.headers = headers
        self._payload = payload
        self._json_error = json_error

    def json(self):
        if self._json_error is not None:
            raise self._json_error
        return self._payload


def valid_payload():
    return {"access_token": "gho_test_secret", "token_type": "bearer"}


def test_valid_token_response_returns_validated_token():
    token = validate_token_response(FakeResponse(valid_payload()))

    assert token.access_token == "gho_test_secret"
    assert token.token_type == "bearer"
    assert "gho_test_secret" not in repr(token)


def test_accepts_json_content_type_with_parameters_and_case_insensitive_header():
    response = FakeResponse(
        valid_payload(),
        headers={"content-type": "Application/JSON; charset=utf-8"},
    )

    assert validate_token_response(response).access_token == "gho_test_secret"


def test_accepts_case_insensitive_bearer_token_type():
    payload = {"access_token": "token", "token_type": "Bearer"}

    assert validate_token_response(FakeResponse(payload)).token_type == "bearer"


def test_allows_unrelated_extra_fields():
    payload = {**valid_payload(), "scope": ""}

    assert validate_token_response(FakeResponse(payload)).access_token == "gho_test_secret"


@pytest.mark.parametrize("status_code", [199, 300, 400, 500, True, "200", None])
def test_rejects_unsuccessful_http_status(status_code):
    with pytest.raises(TokenValidationError, match="HTTP status"):
        validate_token_response(
            FakeResponse(valid_payload(), status_code=status_code)
        )


def test_rejects_missing_content_type():
    with pytest.raises(TokenValidationError, match="Content-Type"):
        validate_token_response(
            FakeResponse(valid_payload(), content_type=None)
        )


@pytest.mark.parametrize(
    "content_type",
    ["", "text/html", "application/x-www-form-urlencoded"],
)
def test_rejects_unexpected_content_type(content_type):
    with pytest.raises(TokenValidationError, match="Content-Type"):
        validate_token_response(
            FakeResponse(valid_payload(), content_type=content_type)
        )


@pytest.mark.parametrize(
    "json_error",
    [ValueError("invalid JSON"), TypeError("invalid JSON")],
)
def test_rejects_invalid_json(json_error):
    with pytest.raises(TokenValidationError, match="not valid JSON"):
        validate_token_response(FakeResponse(json_error=json_error))


@pytest.mark.parametrize("payload", [[], "token", 123, None])
def test_rejects_non_object_json(payload):
    with pytest.raises(TokenValidationError, match="not a JSON object"):
        validate_token_response(FakeResponse(payload))


@pytest.mark.parametrize("access_token", [None, "", "   ", 123, ["token"]])
def test_rejects_missing_or_invalid_access_token(access_token):
    payload = {"token_type": "bearer"}
    if access_token is not None:
        payload["access_token"] = access_token

    with pytest.raises(TokenValidationError, match="access_token"):
        validate_token_response(FakeResponse(payload))


@pytest.mark.parametrize("token_type", [None, "", "   ", "mac", 123])
def test_rejects_missing_or_invalid_token_type(token_type):
    payload = {"access_token": "token"}
    if token_type is not None:
        payload["token_type"] = token_type

    with pytest.raises(TokenValidationError, match="token_type"):
        validate_token_response(FakeResponse(payload))


@pytest.mark.parametrize(
    "field_name,value",
    [
        ("expires_in", 3600),
        ("expires_in", None),
        ("refresh_token", "refresh-value"),
        ("refresh_token_expires_in", 7200),
    ],
)
def test_rejects_forbidden_expiration_and_refresh_fields(field_name, value):
    payload = {**valid_payload(), field_name: value}

    with pytest.raises(TokenValidationError, match="expiration policy"):
        validate_token_response(FakeResponse(payload))


def test_validation_error_does_not_expose_access_token():
    secret = "gho_do_not_leak"
    payload = {"access_token": secret, "token_type": "mac"}

    with pytest.raises(TokenValidationError) as exc_info:
        validate_token_response(FakeResponse(payload))

    assert secret not in str(exc_info.value)


@pytest.fixture
def broker_config():
    return BrokerConfig(
        client_id="test-client-id",
        client_secret="test-client-secret",
        callback_url="https://broker.example.com/auth/github/callback",
        request_timeout=12.5,
    )


def test_broker_config_exposes_validated_values_without_secret_repr(broker_config):
    assert broker_config.client_id == "test-client-id"
    assert broker_config.callback_url == (
        "https://broker.example.com/auth/github/callback"
    )
    assert broker_config.request_timeout == 12.5
    assert "test-client-secret" not in repr(broker_config)


def test_broker_config_from_env_reads_required_values():
    config = BrokerConfig.from_env(
        {
            "GITHUB_APP_CLIENT_ID": "env-client-id",
            "GITHUB_APP_CLIENT_SECRET": "env-client-secret",
            "BROKER_CALLBACK_URL": "https://broker.example.com/callback",
        }
    )

    assert config.client_id == "env-client-id"
    assert config.client_secret == "env-client-secret"
    assert config.callback_url == "https://broker.example.com/callback"
    assert config.request_timeout == 30.0


def test_broker_config_from_env_uses_optional_timeout():
    config = BrokerConfig.from_env(
        {
            "GITHUB_APP_CLIENT_ID": "env-client-id",
            "GITHUB_APP_CLIENT_SECRET": "env-client-secret",
            "BROKER_CALLBACK_URL": "https://broker.example.com/callback",
            "BROKER_REQUEST_TIMEOUT": "4.5",
        }
    )

    assert config.request_timeout == 4.5


@pytest.mark.parametrize(
    "env_name",
    [
        "GITHUB_APP_CLIENT_ID",
        "GITHUB_APP_CLIENT_SECRET",
        "BROKER_CALLBACK_URL",
    ],
)
def test_broker_config_from_env_requires_variables(env_name):
    env = {
        "GITHUB_APP_CLIENT_ID": "env-client-id",
        "GITHUB_APP_CLIENT_SECRET": "env-client-secret",
        "BROKER_CALLBACK_URL": "https://broker.example.com/callback",
    }
    del env[env_name]

    with pytest.raises(ValueError, match=env_name):
        BrokerConfig.from_env(env)


def test_broker_config_from_env_rejects_invalid_timeout():
    with pytest.raises(ValueError, match="BROKER_REQUEST_TIMEOUT"):
        BrokerConfig.from_env(
            {
                "GITHUB_APP_CLIENT_ID": "env-client-id",
                "GITHUB_APP_CLIENT_SECRET": "env-client-secret",
                "BROKER_CALLBACK_URL": "https://broker.example.com/callback",
                "BROKER_REQUEST_TIMEOUT": "not-a-number",
            }
        )


@pytest.mark.parametrize(
    "callback_url",
    [
        "http://broker.example.com/callback",
        "ftp://broker.example.com/callback",
        "broker.example.com/callback",
        "https://broker.example.com/callback?dynamic=1",
        "https://broker.example.com/callback#fragment",
        "https://user:pass@broker.example.com/callback",
        "https://broker.example.com/*",
        "https:///callback",
        "https://broker.example.com/callback with space",
    ],
)
def test_broker_config_rejects_unsafe_callback_urls(callback_url):
    with pytest.raises(ValueError, match="callback_url"):
        BrokerConfig(
            client_id="client-id",
            client_secret="client-secret",
            callback_url=callback_url,
        )


@pytest.mark.parametrize(
    "client_id,client_secret",
    [
        ("", "client-secret"),
        ("   ", "client-secret"),
        ("client-id", ""),
        ("client-id", "   "),
    ],
)
def test_broker_config_rejects_empty_credentials(client_id, client_secret):
    with pytest.raises(ValueError, match="client_"):
        BrokerConfig(
            client_id=client_id,
            client_secret=client_secret,
            callback_url="https://broker.example.com/callback",
        )


@pytest.mark.parametrize(
    "timeout",
    [0, -1, float("nan"), float("inf"), float("-inf"), True, "30"],
)
def test_broker_config_rejects_invalid_timeout(timeout):
    with pytest.raises(ValueError, match="request_timeout"):
        BrokerConfig(
            client_id="client-id",
            client_secret="client-secret",
            callback_url="https://broker.example.com/callback",
            request_timeout=timeout,
        )


def test_create_authorization_request_uses_github_pkce_and_fixed_callback(
    broker_config,
):
    request = GitHubOAuthClient(broker_config).create_authorization_request()
    parsed = urlsplit(request.authorization_url)
    params = parse_qs(parsed.query)

    assert request.authorization_url.startswith(
        GITHUB_AUTHORIZATION_ENDPOINT + "?"
    )
    assert parsed.scheme == "https"
    assert parsed.netloc == "github.com"
    assert parsed.path == "/login/oauth/authorize"
    assert params["client_id"] == ["test-client-id"]
    assert params["redirect_uri"] == [broker_config.callback_url]
    assert params["state"] == [request.state]
    assert params["code_challenge"] == [
        pkce_s256_challenge(request.code_verifier)
    ]
    assert params["code_challenge_method"] == ["S256"]
    assert "scope" not in params
    assert "offline_access" not in params
    assert request.code_verifier not in repr(request)


def test_create_authorization_request_generates_fresh_values(broker_config):
    client = GitHubOAuthClient(broker_config)

    first = client.create_authorization_request()
    second = client.create_authorization_request()

    assert first.state != second.state
    assert first.code_verifier != second.code_verifier
    assert first.authorization_url != second.authorization_url


def test_authorization_url_does_not_contain_client_secret(broker_config):
    request = GitHubOAuthClient(broker_config).create_authorization_request()

    assert broker_config.client_secret not in request.authorization_url


@pytest.mark.parametrize("field_name", ["state", "code_challenge"])
def test_build_authorization_url_rejects_empty_values(broker_config, field_name):
    values = {"state": "state-value", "code_challenge": "challenge-value"}
    values[field_name] = ""

    with pytest.raises(ValueError, match=field_name):
        GitHubOAuthClient(broker_config).build_authorization_url(**values)


def test_exchange_code_posts_required_parameters_and_validates_response(
    broker_config,
):
    verifier = "test-code-verifier"
    post = Mock(return_value=FakeResponse(valid_payload()))

    token = GitHubOAuthClient(broker_config, post=post).exchange_code(
        code="auth-code",
        code_verifier=verifier,
    )

    post.assert_called_once_with(
        GITHUB_TOKEN_ENDPOINT,
        data={
            "client_id": "test-client-id",
            "client_secret": "test-client-secret",
            "code": "auth-code",
            "redirect_uri": broker_config.callback_url,
            "code_verifier": verifier,
        },
        headers={"Accept": "application/json"},
        timeout=12.5,
        allow_redirects=False,
    )
    assert token.access_token == "gho_test_secret"
    assert token.token_type == "bearer"


@pytest.mark.parametrize(
    "response_payload",
    [
        {"access_token": "token", "token_type": "bearer", "expires_in": 3600},
        {
            "access_token": "token",
            "token_type": "bearer",
            "refresh_token": "refresh-value",
        },
        {"access_token": "token", "token_type": "mac"},
    ],
)
def test_exchange_code_propagates_token_validation_errors(
    broker_config,
    response_payload,
):
    post = Mock(return_value=FakeResponse(response_payload))

    with pytest.raises(TokenValidationError):
        GitHubOAuthClient(broker_config, post=post).exchange_code(
            code="auth-code",
            code_verifier="test-code-verifier",
        )


def test_exchange_code_wraps_network_errors_without_leaking_values(broker_config):
    verifier = "test-code-verifier"
    post = Mock(side_effect=requests.ConnectionError("connection failed"))

    with pytest.raises(TokenExchangeError) as exc_info:
        GitHubOAuthClient(broker_config, post=post).exchange_code(
            code="auth-code",
            code_verifier=verifier,
        )

    message = str(exc_info.value)
    assert "connection failed" not in message
    assert broker_config.client_secret not in message
    assert verifier not in message
    assert "auth-code" not in message


@pytest.mark.parametrize(
    "code,code_verifier,expected_field",
    [
        ("", "test-code-verifier", "code"),
        ("   ", "test-code-verifier", "code"),
        ("auth-code", "", "code_verifier"),
        ("auth-code", "   ", "code_verifier"),
    ],
)
def test_exchange_code_rejects_empty_values_before_request(
    broker_config,
    code,
    code_verifier,
    expected_field,
):
    post = Mock()

    with pytest.raises(ValueError, match=expected_field):
        GitHubOAuthClient(broker_config, post=post).exchange_code(
            code=code,
            code_verifier=code_verifier,
        )

    post.assert_not_called()
