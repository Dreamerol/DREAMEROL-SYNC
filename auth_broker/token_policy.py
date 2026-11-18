"""Pure validation for GitHub OAuth token endpoint responses.

This module has no network dependencies. The Broker's OAuth client supplies a
response-like object after the token request, and this module decides whether
the response satisfies the approved MVP token policy before any credential is
made available to the CLI.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol


JSON_CONTENT_TYPE = "application/json"
FORBIDDEN_TOKEN_FIELDS = (
    "expires_in",
    "refresh_token",
    "refresh_token_expires_in",
)


class TokenValidationError(ValueError):
    """The GitHub token response does not satisfy the approved policy."""


@dataclass(frozen=True)
class ValidatedToken:
    """Validated credential data; the access token is hidden from repr output."""

    access_token: str = field(repr=False)
    token_type: str


class HttpResponse(Protocol):
    """Minimal response interface required for token validation."""

    status_code: int
    headers: Mapping[str, str]

    def json(self) -> Any:
        """Return the decoded JSON response body."""


def validate_token_response(response: HttpResponse) -> ValidatedToken:
    """Validate an HTTP token response without logging or exposing secrets."""
    _validate_status(response)
    _validate_content_type(response)
    payload = _parse_json(response)
    return _validate_payload(payload)


def _validate_status(response: HttpResponse) -> None:
    status_code = getattr(response, "status_code", None)
    if (
        isinstance(status_code, bool)
        or not isinstance(status_code, int)
        or not 200 <= status_code < 300
    ):
        raise TokenValidationError(
            "token endpoint returned an unsuccessful HTTP status"
        )


def _validate_content_type(response: HttpResponse) -> None:
    content_type = _header_value(getattr(response, "headers", None), "Content-Type")
    if not isinstance(content_type, str):
        raise TokenValidationError(
            "token endpoint response is missing Content-Type"
        )
    media_type = content_type.partition(";")[0].strip().lower()
    if media_type != JSON_CONTENT_TYPE:
        raise TokenValidationError(
            "token endpoint response has an unexpected Content-Type"
        )


def _parse_json(response: HttpResponse) -> dict[str, Any]:
    try:
        payload = response.json()
    except (TypeError, ValueError):
        raise TokenValidationError(
            "token endpoint response is not valid JSON"
        ) from None
    if not isinstance(payload, dict):
        raise TokenValidationError(
            "token endpoint response is not a JSON object"
        )
    return payload


def _validate_payload(payload: dict[str, Any]) -> ValidatedToken:
    access_token = payload.get("access_token")
    if not isinstance(access_token, str) or not access_token.strip():
        raise TokenValidationError(
            "token response has an invalid access_token"
        )

    token_type = payload.get("token_type")
    if not isinstance(token_type, str) or token_type.strip().lower() != "bearer":
        raise TokenValidationError(
            "token response has an unexpected token_type"
        )

    for field_name in FORBIDDEN_TOKEN_FIELDS:
        if field_name in payload:
            raise TokenValidationError(
                "token response does not satisfy the approved expiration policy"
            )

    return ValidatedToken(access_token=access_token, token_type="bearer")


def _header_value(headers: Mapping[str, str] | None, name: str) -> str | None:
    if headers is None:
        return None
    for key, value in headers.items():
        if isinstance(key, str) and key.lower() == name.lower():
            return value if isinstance(value, str) else None
    return None


__all__ = [
    "FORBIDDEN_TOKEN_FIELDS",
    "JSON_CONTENT_TYPE",
    "TokenValidationError",
    "ValidatedToken",
    "validate_token_response",
]
