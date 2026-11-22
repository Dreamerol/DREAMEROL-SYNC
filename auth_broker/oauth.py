"""GitHub OAuth authorization and token exchange for the Broker."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlencode

import requests

from auth_broker.config import BrokerConfig
from auth_broker.security import (
    generate_callback_state,
    generate_pkce_verifier,
    pkce_s256_challenge,
)
from auth_broker.token_policy import ValidatedToken, validate_token_response


GITHUB_AUTHORIZATION_ENDPOINT = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN_ENDPOINT = "https://github.com/login/oauth/access_token"


class TokenExchangeError(Exception):
    """The token exchange request failed before a response could be validated."""


@dataclass(frozen=True)
class AuthorizationRequest:
    """Authorization URL plus Broker-only values needed to complete the flow."""

    authorization_url: str
    state: str
    code_verifier: str = field(repr=False)


class GitHubOAuthClient:
    """Build GitHub authorization URLs and exchange authorization codes."""

    def __init__(
        self,
        config: BrokerConfig,
        *,
        post: Callable[..., Any] = requests.post,
    ) -> None:
        if not isinstance(config, BrokerConfig):
            raise TypeError("config must be a BrokerConfig")
        self._config = config
        self._post = post

    def create_authorization_request(self) -> AuthorizationRequest:
        """Create a fresh PKCE authorization request for GitHub."""
        state = generate_callback_state()
        code_verifier = generate_pkce_verifier()
        code_challenge = pkce_s256_challenge(code_verifier)
        return AuthorizationRequest(
            authorization_url=self.build_authorization_url(
                state=state,
                code_challenge=code_challenge,
            ),
            state=state,
            code_verifier=code_verifier,
        )

    def build_authorization_url(self, *, state: str, code_challenge: str) -> str:
        """Build the fixed GitHub authorization URL using PKCE S256."""
        _require_non_empty_string(state, "state")
        _require_non_empty_string(code_challenge, "code_challenge")
        params = {
            "client_id": self._config.client_id,
            "redirect_uri": self._config.callback_url,
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "prompt": "select_account",
        }
        return f"{GITHUB_AUTHORIZATION_ENDPOINT}?{urlencode(params)}"

    def exchange_code(self, *, code: str, code_verifier: str) -> ValidatedToken:
        """Exchange an authorization code and validate the token response."""
        _require_non_empty_string(code, "code")
        _require_non_empty_string(code_verifier, "code_verifier")
        payload = {
            "client_id": self._config.client_id,
            "client_secret": self._config.client_secret,
            "code": code,
            "redirect_uri": self._config.callback_url,
            "code_verifier": code_verifier,
        }
        try:
            response = self._post(
                GITHUB_TOKEN_ENDPOINT,
                data=payload,
                headers={"Accept": "application/json"},
                timeout=self._config.request_timeout,
                allow_redirects=False,
            )
        except requests.RequestException:
            raise TokenExchangeError("token exchange request failed") from None
        return validate_token_response(response)


def _require_non_empty_string(value: object, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


__all__ = [
    "AuthorizationRequest",
    "GITHUB_AUTHORIZATION_ENDPOINT",
    "GITHUB_TOKEN_ENDPOINT",
    "GitHubOAuthClient",
    "TokenExchangeError",
]
