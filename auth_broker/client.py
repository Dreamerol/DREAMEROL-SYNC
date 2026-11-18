"""HTTP client used by the CLI to talk to the authentication Broker."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import requests


DEFAULT_BROKER_URL = "http://127.0.0.1:8000"
POLLING_SECRET_HEADER = "X-Polling-Secret"


class BrokerAuthError(Exception):
    """The Broker request failed or returned an unusable response."""


@dataclass(frozen=True)
class AuthStart:
    transaction_id: str
    authorization_url: str


@dataclass(frozen=True)
class AuthPoll:
    state: str
    access_token: str | None = field(default=None, repr=False)
    token_type: str | None = None


class BrokerAuthClient:
    """Small JSON client for the Broker's four authentication endpoints."""

    def __init__(
        self,
        base_url: str,
        *,
        session: requests.Session | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._base_url = _validate_base_url(base_url)
        self._owns_session = session is None
        self._session = session if session is not None else requests.Session()
        self._timeout = _validate_timeout(timeout)

    def close(self) -> None:
        if self._owns_session:
            self._session.close()

    def start(self, polling_secret_hash: str) -> AuthStart:
        if not isinstance(polling_secret_hash, str) or not polling_secret_hash:
            raise ValueError("polling_secret_hash must be a non-empty string")
        response = self._post(
            "/auth/start",
            json={"polling_secret_hash": polling_secret_hash},
            headers={"Accept": "application/json"},
        )
        payload = _json_object(response)
        transaction_id = payload.get("transaction_id")
        authorization_url = payload.get("authorization_url")
        if not isinstance(transaction_id, str) or not transaction_id.strip():
            raise BrokerAuthError(
                "Broker returned an invalid authentication transaction."
            )
        if not isinstance(authorization_url, str) or not authorization_url.strip():
            raise BrokerAuthError("Broker returned an invalid authorization URL.")
        _validate_authorization_url(authorization_url)
        return AuthStart(
            transaction_id=transaction_id,
            authorization_url=authorization_url,
        )

    def poll(self, transaction_id: str, polling_secret: str) -> AuthPoll:
        if not isinstance(transaction_id, str) or not transaction_id.strip():
            raise ValueError("transaction_id must be a non-empty string")
        if not isinstance(polling_secret, str) or not polling_secret:
            raise ValueError("polling_secret must be a non-empty string")
        response = self._post(
            "/auth/poll",
            json={"transaction_id": transaction_id},
            headers={
                "Accept": "application/json",
                POLLING_SECRET_HEADER: polling_secret,
            },
        )
        payload = _json_object(response)
        state = payload.get("state")
        if not isinstance(state, str) or not state:
            raise BrokerAuthError(
                "Broker returned an invalid authentication state."
            )

        access_token = payload.get("access_token")
        token_type = payload.get("token_type")
        if access_token is not None:
            if state != "DELIVERY_AUTHORIZED":
                raise BrokerAuthError(
                    "Broker returned a credential in an invalid state."
                )
            if not isinstance(access_token, str) or not access_token.strip():
                raise BrokerAuthError("Broker returned an invalid credential.")
            if (
                not isinstance(token_type, str)
                or token_type.strip().lower() != "bearer"
            ):
                raise BrokerAuthError("Broker returned an invalid credential type.")
            token_type = "bearer"
        elif state == "DELIVERY_AUTHORIZED":
            raise BrokerAuthError(
                "Broker did not return the authorized credential."
            )

        return AuthPoll(
            state=state,
            access_token=access_token,
            token_type=token_type,
        )

    def ack(self, transaction_id: str, polling_secret: str) -> str:
        if not isinstance(transaction_id, str) or not transaction_id.strip():
            raise ValueError("transaction_id must be a non-empty string")
        if not isinstance(polling_secret, str) or not polling_secret:
            raise ValueError("polling_secret must be a non-empty string")
        response = self._post(
            "/auth/ack",
            json={"transaction_id": transaction_id},
            headers={
                "Accept": "application/json",
                POLLING_SECRET_HEADER: polling_secret,
            },
        )
        payload = _json_object(response)
        state = payload.get("state")
        if state != "ACKNOWLEDGED":
            raise BrokerAuthError("Broker did not acknowledge the credential.")
        return state

    def _post(self, path: str, *, json: dict[str, str], headers: dict[str, str]):
        try:
            response = self._session.post(
                self._base_url + path,
                json=json,
                headers=headers,
                timeout=self._timeout,
                allow_redirects=False,
            )
        except requests.RequestException:
            raise BrokerAuthError(
                "Could not reach the authentication Broker."
            ) from None

        if response.status_code == 401:
            raise BrokerAuthError(
                "The authentication Broker rejected the polling secret."
            )
        if not 200 <= response.status_code < 300:
            raise BrokerAuthError(
                f"The authentication Broker returned HTTP {response.status_code}."
            )
        return response


def _json_object(response) -> dict:
    try:
        payload = response.json()
    except (TypeError, ValueError):
        raise BrokerAuthError("Broker returned invalid JSON.") from None
    if not isinstance(payload, dict):
        raise BrokerAuthError("Broker returned an invalid response.")
    return payload


def _validate_base_url(value: object) -> str:
    if not isinstance(value, str) or not value or any(ch.isspace() for ch in value):
        raise BrokerAuthError("Broker URL must be a valid HTTPS URL.")
    try:
        parsed = urlsplit(value)
        _ = parsed.port
        hostname = parsed.hostname
    except ValueError:
        raise BrokerAuthError("Broker URL must be a valid HTTPS URL.") from None

    if (
        not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise BrokerAuthError("Broker URL must be a valid HTTPS URL.")

    scheme = parsed.scheme.lower()
    if scheme == "https":
        return value.rstrip("/")
    if scheme == "http" and hostname.lower() in {"127.0.0.1", "localhost", "::1"}:
        return value.rstrip("/")
    raise BrokerAuthError(
        "Broker URL must use HTTPS, except for localhost development."
    )


def _validate_authorization_url(value: str) -> None:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise BrokerAuthError(
            "Broker returned an invalid authorization URL."
        ) from None

    if (
        parsed.scheme.lower() != "https"
        or parsed.hostname != "github.com"
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.fragment
    ):
        raise BrokerAuthError(
            "Broker returned an untrusted authorization URL."
        )


def _validate_timeout(value: object) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise BrokerAuthError("Broker timeout must be a positive finite number.")
    return float(value)


__all__ = [
    "AuthPoll",
    "AuthStart",
    "BrokerAuthClient",
    "BrokerAuthError",
    "DEFAULT_BROKER_URL",
    "POLLING_SECRET_HEADER",
]
