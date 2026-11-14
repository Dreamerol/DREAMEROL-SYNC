"""Server-side Auth Broker sessions for the web dashboard."""

from __future__ import annotations

import math
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from auth_broker.client import (
    DEFAULT_BROKER_URL,
    BrokerAuthClient,
    BrokerAuthError,
)
from auth_broker.security import generate_polling_secret, hash_secret
from fork_sync.github import GitHubClient


SESSION_COOKIE = "dreamerol_session"
SESSION_TTL_SECONDS = 600.0
AUTHENTICATED = "AUTHENTICATED"
PENDING_STATES = frozenset({"PENDING", "PROCESSING", "TOKEN_READY"})
TERMINAL_STATES = frozenset({"FAILED", "CANCELLED", "EXPIRED", "ACKNOWLEDGED"})


class WebAuthError(Exception):
    """The web authentication flow failed."""


class UnknownSession(WebAuthError):
    """The browser session is missing or expired."""


@dataclass
class _Session:
    transaction_id: str
    polling_secret: str = field(repr=False)
    broker_url: str
    expires_at: float
    state: str = "PENDING"
    access_token: str | None = field(default=None, repr=False)
    account: str | None = None


class WebAuthManager:
    """Keep Broker transactions and credentials server-side per browser session."""

    def __init__(
        self,
        broker_url: str = DEFAULT_BROKER_URL,
        *,
        ttl_seconds: float = SESSION_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        broker_client_factory: Callable[[str], BrokerAuthClient] = BrokerAuthClient,
        github_client_factory: Callable[[str], GitHubClient] = GitHubClient,
    ) -> None:
        if (
            isinstance(ttl_seconds, bool)
            or not isinstance(ttl_seconds, (int, float))
            or not math.isfinite(ttl_seconds)
            or ttl_seconds <= 0
        ):
            raise ValueError("ttl_seconds must be a positive number")
        self._broker_url = broker_url
        self._ttl_seconds = float(ttl_seconds)
        self._clock = clock
        self._broker_client_factory = broker_client_factory
        self._github_client_factory = github_client_factory
        self._lock = threading.RLock()
        self._sessions: dict[str, _Session] = {}

    @property
    def ttl_seconds(self) -> float:
        return self._ttl_seconds

    def start(self) -> tuple[str, str]:
        """Create a Broker transaction and a matching server-side session."""
        polling_secret = generate_polling_secret()
        client = self._broker_client_factory(self._broker_url)
        try:
            start = client.start(hash_secret(polling_secret))
        except BrokerAuthError as exc:
            raise WebAuthError(str(exc)) from None
        finally:
            client.close()

        now = self._clock()
        with self._lock:
            self._cleanup_locked(now)
            session_id = self._new_session_id_locked()
            self._sessions[session_id] = _Session(
                transaction_id=start.transaction_id,
                polling_secret=polling_secret,
                broker_url=self._broker_url,
                expires_at=now + self._ttl_seconds,
            )
        return session_id, start.authorization_url

    def status(self, session_id: str | None) -> dict[str, str]:
        """Poll the Broker and return the current session state."""
        session = self._get_session(session_id)
        if session.access_token is not None:
            return self._authenticated_response(session)
        if session.state in TERMINAL_STATES:
            return {"state": session.state}

        client = self._broker_client_factory(session.broker_url)
        try:
            result = client.poll(session.transaction_id, session.polling_secret)
        except BrokerAuthError as exc:
            raise WebAuthError(str(exc)) from None
        finally:
            client.close()

        if result.state in PENDING_STATES:
            with self._lock:
                session.state = result.state
            return {"state": result.state}

        if result.state != "DELIVERY_AUTHORIZED":
            with self._lock:
                session.state = result.state
            return {"state": result.state}

        if not result.access_token:
            raise WebAuthError("The Broker did not return a credential.")

        try:
            account = self._resolve_account(result.access_token)
        except WebAuthError:
            with self._lock:
                session.state = "FAILED"
                session.polling_secret = ""
            raise

        with self._lock:
            session.access_token = result.access_token
            session.account = account
            session.state = AUTHENTICATED
            polling_secret = session.polling_secret
            session.polling_secret = ""

        if polling_secret:
            self._acknowledge(session.broker_url, session.transaction_id, polling_secret)

        return self._authenticated_response(session)

    def token(self, session_id: str | None) -> str | None:
        """Return the stored credential for an authenticated browser session."""
        if not session_id:
            return None
        now = self._clock()
        with self._lock:
            self._cleanup_locked(now)
            session = self._sessions.get(session_id)
            return session.access_token if session is not None else None

    def logout(self, session_id: str | None) -> None:
        """Discard a browser session and its in-memory credential."""
        if not session_id:
            return
        with self._lock:
            self._sessions.pop(session_id, None)

    def _get_session(self, session_id: str | None) -> _Session:
        if not session_id:
            raise UnknownSession("No web authentication session was found.")
        now = self._clock()
        with self._lock:
            self._cleanup_locked(now)
            session = self._sessions.get(session_id)
            if session is None:
                raise UnknownSession("The web authentication session has expired.")
            return session

    def _resolve_account(self, token: str) -> str:
        github = self._github_client_factory(token)
        try:
            user = github.get_authenticated_user()
        except Exception:
            raise WebAuthError(
                "Could not validate the GitHub credential returned by the Broker."
            ) from None
        finally:
            github.session.close()

        account = user.get("login") if isinstance(user, dict) else None
        if not isinstance(account, str) or not account.strip():
            raise WebAuthError("GitHub returned an invalid account profile.")
        return account.strip()

    def _acknowledge(
        self,
        broker_url: str,
        transaction_id: str,
        polling_secret: str,
    ) -> None:
        client = self._broker_client_factory(broker_url)
        try:
            client.ack(transaction_id, polling_secret)
        except BrokerAuthError:
            pass
        finally:
            client.close()

    def _authenticated_response(self, session: _Session) -> dict[str, str]:
        response = {"state": AUTHENTICATED}
        if session.account is not None:
            response["account"] = session.account
        return response

    def _new_session_id_locked(self) -> str:
        session_id = secrets.token_urlsafe(32)
        while session_id in self._sessions:
            session_id = secrets.token_urlsafe(32)
        return session_id

    def _cleanup_locked(self, now: float) -> None:
        expired = [
            session_id
            for session_id, session in self._sessions.items()
            if now >= session.expires_at
        ]
        for session_id in expired:
            self._sessions.pop(session_id, None)


__all__ = [
    "AUTHENTICATED",
    "SESSION_COOKIE",
    "SESSION_TTL_SECONDS",
    "TERMINAL_STATES",
    "UnknownSession",
    "WebAuthError",
    "WebAuthManager",
]
