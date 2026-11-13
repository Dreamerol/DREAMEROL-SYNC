"""HTTP endpoints for the authentication Broker."""

from __future__ import annotations

import math

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from auth_broker.config import RateLimitConfig
from auth_broker.oauth import GitHubOAuthClient
from auth_broker.rate_limit import (
    InMemoryRateLimiter,
    RateLimitPolicy,
    get_client_ip,
)
from auth_broker.transactions import (
    InvalidCallbackState,
    InvalidTransaction,
    InvalidTransition,
    TransactionExpired,
    TransactionStore,
)


POLLING_SECRET_HEADER = "X-Polling-Secret"

router = APIRouter(prefix="/auth", tags=["auth"])


class StartRequest(BaseModel):
    polling_secret_hash: str


class StartResponse(BaseModel):
    transaction_id: str
    authorization_url: str


class TransactionRequest(BaseModel):
    transaction_id: str


class PollResponse(BaseModel):
    state: str
    access_token: str | None = Field(default=None, repr=False)
    token_type: str | None = None


class StateResponse(BaseModel):
    state: str


def get_transaction_store(request: Request) -> TransactionStore:
    return request.app.state.transaction_store


def get_oauth_client(request: Request) -> GitHubOAuthClient:
    return request.app.state.oauth_client


def get_rate_limit_config(request: Request) -> RateLimitConfig:
    return request.app.state.rate_limit_config


def get_rate_limiter(request: Request) -> InMemoryRateLimiter:
    return request.app.state.rate_limiter


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


def _retry_after_seconds(value: float) -> str:
    return str(max(1, math.ceil(value)))


def _rate_limit_exception(retry_after: float) -> HTTPException:
    return HTTPException(
        status_code=429,
        detail="Too many requests. Try again later.",
        headers={
            "Retry-After": _retry_after_seconds(retry_after),
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
        },
    )


def _browser_page(
    message: str,
    *,
    status_code: int,
    retry_after: float | None = None,
) -> HTMLResponse:
    body = (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        "<title>DREAMEROL authentication</title></head><body>"
        f"<p>{message}</p></body></html>"
    )
    headers = {
        "Cache-Control": "no-store",
        "Pragma": "no-cache",
        "Referrer-Policy": "no-referrer",
    }
    if retry_after is not None:
        headers["Retry-After"] = _retry_after_seconds(retry_after)
    return HTMLResponse(
        content=body,
        status_code=status_code,
        headers=headers,
    )


def _rate_limited_page(retry_after: float) -> HTMLResponse:
    return _browser_page(
        "Too many requests. Try again later.",
        status_code=429,
        retry_after=retry_after,
    )


def _record_limit(
    limiter: InMemoryRateLimiter,
    config: RateLimitConfig,
    key: str,
    limit: int,
) -> float | None:
    if not config.enabled:
        return None
    return limiter.record(
        key,
        RateLimitPolicy(limit, config.window_seconds),
    )


def _failure_retry(
    limiter: InMemoryRateLimiter,
    config: RateLimitConfig,
    key: str,
) -> float | None:
    if not config.enabled:
        return None
    return limiter.retry_after(
        key,
        RateLimitPolicy(config.failures_per_ip, config.window_seconds),
    )


def _record_failure(
    limiter: InMemoryRateLimiter,
    config: RateLimitConfig,
    key: str,
) -> None:
    if config.enabled:
        limiter.record(
            key,
            RateLimitPolicy(config.failures_per_ip, config.window_seconds),
        )


def _require_polling_secret(polling_secret: str | None) -> str:
    if not polling_secret:
        raise HTTPException(
            status_code=401,
            detail="Invalid transaction or polling secret.",
            headers={"WWW-Authenticate": POLLING_SECRET_HEADER},
        )
    return polling_secret


def _authenticate_transaction_request(
    store: TransactionStore,
    transaction_id: str,
    polling_secret: str | None,
    *,
    limiter: InMemoryRateLimiter,
    config: RateLimitConfig,
    failure_key: str,
) -> str:
    try:
        polling_secret = _require_polling_secret(polling_secret)
        store.verify_polling_secret(transaction_id, polling_secret)
    except HTTPException:
        _record_failure(limiter, config, failure_key)
        raise
    except InvalidTransaction:
        _record_failure(limiter, config, failure_key)
        raise HTTPException(
            status_code=401,
            detail="Invalid transaction or polling secret.",
            headers={"WWW-Authenticate": POLLING_SECRET_HEADER},
        ) from None
    return polling_secret


@router.post("/start", response_model=StartResponse)
def start_authentication(
    payload: StartRequest,
    request: Request,
    response: Response,
    store: TransactionStore = Depends(get_transaction_store),
    oauth_client: GitHubOAuthClient = Depends(get_oauth_client),
    rate_limit_config: RateLimitConfig = Depends(get_rate_limit_config),
    rate_limiter: InMemoryRateLimiter = Depends(get_rate_limiter),
):
    """Create a pending transaction and return a Broker-generated authorization URL."""
    _no_store(response)
    client_ip = get_client_ip(request, rate_limit_config.trusted_proxies)
    retry_after = _record_limit(
        rate_limiter,
        rate_limit_config,
        f"start:ip:{client_ip}",
        rate_limit_config.start_per_ip,
    )
    if retry_after is not None:
        raise _rate_limit_exception(retry_after)

    try:
        authorization_request = oauth_client.create_authorization_request()
        transaction_id = store.create(
            payload.polling_secret_hash,
            authorization_request.state,
            code_verifier=authorization_request.code_verifier,
        )
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="Invalid authentication request.",
        ) from None

    return StartResponse(
        transaction_id=transaction_id,
        authorization_url=authorization_request.authorization_url,
    )


@router.get("/callback", response_class=HTMLResponse)
def github_callback(
    request: Request,
    state: str | None = None,
    code: str | None = None,
    error: str | None = None,
    store: TransactionStore = Depends(get_transaction_store),
    oauth_client: GitHubOAuthClient = Depends(get_oauth_client),
    rate_limit_config: RateLimitConfig = Depends(get_rate_limit_config),
    rate_limiter: InMemoryRateLimiter = Depends(get_rate_limiter),
):
    """Validate a GitHub callback and complete or safely fail authorization."""
    client_ip = get_client_ip(request, rate_limit_config.trusted_proxies)
    failure_key = f"callback:failures:ip:{client_ip}"
    retry_after = _failure_retry(rate_limiter, rate_limit_config, failure_key)
    if retry_after is None:
        retry_after = _record_limit(
            rate_limiter,
            rate_limit_config,
            f"callback:ip:{client_ip}",
            rate_limit_config.callback_per_ip,
        )
    if retry_after is not None:
        return _rate_limited_page(retry_after)

    if not state:
        _record_failure(rate_limiter, rate_limit_config, failure_key)
        return _browser_page(
            "Authentication could not be completed.",
            status_code=400,
        )

    if error is not None:
        try:
            store.fail_callback(state, cancelled=error == "access_denied")
        except (InvalidCallbackState, InvalidTransition, TransactionExpired):
            _record_failure(rate_limiter, rate_limit_config, failure_key)
            return _browser_page(
                "Authentication could not be completed.",
                status_code=400,
            )
        if error == "access_denied":
            return _browser_page(
                "Authorization was cancelled. You can return to the CLI.",
                status_code=400,
            )
        _record_failure(rate_limiter, rate_limit_config, failure_key)
        return _browser_page(
            "Authentication failed. You can return to the CLI.",
            status_code=400,
        )

    if not code:
        try:
            store.fail_callback(state)
        except (InvalidCallbackState, InvalidTransition, TransactionExpired):
            pass
        _record_failure(rate_limiter, rate_limit_config, failure_key)
        return _browser_page(
            "Authentication failed. You can return to the CLI.",
            status_code=400,
        )

    try:
        transaction_id = store.claim_callback(state)
    except (InvalidCallbackState, InvalidTransition, TransactionExpired):
        _record_failure(rate_limiter, rate_limit_config, failure_key)
        return _browser_page(
            "Authentication could not be completed.",
            status_code=400,
        )

    try:
        code_verifier = store.get_code_verifier(transaction_id)
        token = oauth_client.exchange_code(
            code=code,
            code_verifier=code_verifier,
        )
        store.make_token_ready(transaction_id, token.access_token)
    except Exception:
        _record_failure(rate_limiter, rate_limit_config, failure_key)
        try:
            store.fail_processing(transaction_id)
        except Exception:
            pass
        return _browser_page(
            "Authentication could not be completed.",
            status_code=502,
        )

    return _browser_page(
        "GitHub authorization is complete. You can return to the CLI.",
        status_code=200,
    )


@router.post(
    "/poll",
    response_model=PollResponse,
    response_model_exclude_none=True,
)
def poll_authentication(
    payload: TransactionRequest,
    request: Request,
    response: Response,
    polling_secret: str | None = Header(
        default=None,
        alias=POLLING_SECRET_HEADER,
    ),
    store: TransactionStore = Depends(get_transaction_store),
    rate_limit_config: RateLimitConfig = Depends(get_rate_limit_config),
    rate_limiter: InMemoryRateLimiter = Depends(get_rate_limiter),
):
    """Authenticate a CLI poll and deliver a credential when authorized."""
    _no_store(response)
    client_ip = get_client_ip(request, rate_limit_config.trusted_proxies)
    failure_key = f"poll:failures:ip:{client_ip}"
    retry_after = _failure_retry(rate_limiter, rate_limit_config, failure_key)
    if retry_after is None:
        retry_after = _record_limit(
            rate_limiter,
            rate_limit_config,
            f"poll:ip:{client_ip}",
            rate_limit_config.poll_per_ip,
        )
    if retry_after is not None:
        raise _rate_limit_exception(retry_after)

    polling_secret = _authenticate_transaction_request(
        store,
        payload.transaction_id,
        polling_secret,
        limiter=rate_limiter,
        config=rate_limit_config,
        failure_key=failure_key,
    )

    retry_after = _record_limit(
        rate_limiter,
        rate_limit_config,
        f"poll:transaction:{payload.transaction_id}",
        rate_limit_config.poll_per_transaction,
    )
    if retry_after is not None:
        raise _rate_limit_exception(retry_after)

    try:
        result = store.poll(payload.transaction_id, polling_secret)
    except InvalidTransaction:
        _record_failure(rate_limiter, rate_limit_config, failure_key)
        raise HTTPException(
            status_code=401,
            detail="Invalid transaction or polling secret.",
            headers={"WWW-Authenticate": POLLING_SECRET_HEADER},
        ) from None
    except TransactionExpired:
        raise HTTPException(
            status_code=410,
            detail="Transaction has expired.",
        ) from None
    except InvalidTransition:
        raise HTTPException(
            status_code=409,
            detail="Transaction is not ready for this operation.",
        ) from None

    if result.credential is not None:
        return PollResponse(
            state=result.state.value,
            access_token=result.credential,
            token_type="bearer",
        )
    return PollResponse(state=result.state.value)


@router.post("/ack", response_model=StateResponse)
def acknowledge_authentication(
    payload: TransactionRequest,
    request: Request,
    response: Response,
    polling_secret: str | None = Header(
        default=None,
        alias=POLLING_SECRET_HEADER,
    ),
    store: TransactionStore = Depends(get_transaction_store),
    rate_limit_config: RateLimitConfig = Depends(get_rate_limit_config),
    rate_limiter: InMemoryRateLimiter = Depends(get_rate_limiter),
):
    """Acknowledge successful local credential storage."""
    _no_store(response)
    client_ip = get_client_ip(request, rate_limit_config.trusted_proxies)
    failure_key = f"ack:failures:ip:{client_ip}"
    retry_after = _failure_retry(rate_limiter, rate_limit_config, failure_key)
    if retry_after is None:
        retry_after = _record_limit(
            rate_limiter,
            rate_limit_config,
            f"ack:ip:{client_ip}",
            rate_limit_config.ack_per_ip,
        )
    if retry_after is not None:
        raise _rate_limit_exception(retry_after)

    polling_secret = _authenticate_transaction_request(
        store,
        payload.transaction_id,
        polling_secret,
        limiter=rate_limiter,
        config=rate_limit_config,
        failure_key=failure_key,
    )

    retry_after = _record_limit(
        rate_limiter,
        rate_limit_config,
        f"ack:transaction:{payload.transaction_id}",
        rate_limit_config.ack_per_transaction,
    )
    if retry_after is not None:
        raise _rate_limit_exception(retry_after)

    try:
        state = store.acknowledge(payload.transaction_id, polling_secret)
    except InvalidTransaction:
        _record_failure(rate_limiter, rate_limit_config, failure_key)
        raise HTTPException(
            status_code=401,
            detail="Invalid transaction or polling secret.",
            headers={"WWW-Authenticate": POLLING_SECRET_HEADER},
        ) from None
    except TransactionExpired:
        raise HTTPException(
            status_code=410,
            detail="Transaction has expired.",
        ) from None
    except InvalidTransition:
        raise HTTPException(
            status_code=409,
            detail="ACK is allowed only after delivery is authorized.",
        ) from None

    return StateResponse(state=state.value)


__all__ = [
    "POLLING_SECRET_HEADER",
    "get_oauth_client",
    "get_rate_limit_config",
    "get_rate_limiter",
    "get_transaction_store",
    "router",
]
