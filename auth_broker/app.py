"""FastAPI application factory for the authentication Broker."""

from __future__ import annotations

import asyncio
import logging
import re
from contextlib import asynccontextmanager, suppress

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from auth_broker.config import BrokerConfig, RateLimitConfig
from auth_broker.oauth import GitHubOAuthClient
from auth_broker.rate_limit import InMemoryRateLimiter
from auth_broker.routes import router
from auth_broker.transactions import TransactionStore

logger = logging.getLogger(__name__)
_QUERY_SECRET_RE = re.compile(r"([?&](?:code|state)=)[^&\s]+", re.IGNORECASE)


class _CallbackAccessRedactor(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = _redact_query_secrets(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(_redact_query_secrets(arg) for arg in record.args)
        elif isinstance(record.args, dict):
            record.args = {
                key: _redact_query_secrets(value) for key, value in record.args.items()
            }
        elif isinstance(record.args, str):
            record.args = _redact_query_secrets(record.args)
        return True


def _redact_query_secrets(value):
    if isinstance(value, str):
        return _QUERY_SECRET_RE.sub(r"\1[REDACTED]", value)
    return value


def _install_access_log_redactor() -> None:
    access_logger = logging.getLogger("uvicorn.access")
    if not any(
        isinstance(log_filter, _CallbackAccessRedactor)
        for log_filter in access_logger.filters
    ):
        access_logger.addFilter(_CallbackAccessRedactor())


def _apply_security_headers(response, request: Request) -> None:
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data: https:; "
        "style-src 'self' 'unsafe-inline'; script-src 'self'; "
        "connect-src 'self'; object-src 'none'; base-uri 'self'; "
        "frame-ancestors 'none'",
    )
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    forwarded = request.headers.get("x-forwarded-proto", "")
    is_https = forwarded.split(",")[0].strip().lower() == "https"
    if is_https or request.url.scheme == "https":
        response.headers.setdefault(
            "Strict-Transport-Security",
            "max-age=31536000; includeSubDomains",
        )


def create_app(
    config: BrokerConfig | None = None,
    *,
    transaction_store: TransactionStore | None = None,
    oauth_client: GitHubOAuthClient | None = None,
    rate_limit_config: RateLimitConfig | None = None,
    rate_limiter: InMemoryRateLimiter | None = None,
) -> FastAPI:
    """Create the Broker app with explicit or environment-derived configuration."""
    if config is None:
        load_dotenv()
        config = BrokerConfig.from_env()
    if transaction_store is None:
        transaction_store = TransactionStore()
    if oauth_client is None:
        oauth_client = GitHubOAuthClient(config)
    if rate_limit_config is None:
        rate_limit_config = RateLimitConfig.from_env()
    if rate_limiter is None:
        rate_limiter = InMemoryRateLimiter()

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        async def _cleanup_loop():
            while True:
                try:
                    transaction_store.cleanup_expired()
                except Exception:
                    logger.exception("Transaction cleanup failed")
                await asyncio.sleep(60)

        task = asyncio.create_task(_cleanup_loop())
        application.state.transaction_cleanup_task = task
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    app = FastAPI(
        title="DREAMEROL Auth Broker",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.broker_config = config
    app.state.transaction_store = transaction_store
    app.state.oauth_client = oauth_client
    app.state.rate_limit_config = rate_limit_config
    app.state.rate_limiter = rate_limiter
    _install_access_log_redactor()

    @app.middleware("http")
    async def add_auth_security_headers(request: Request, call_next):
        response = await call_next(request)
        _apply_security_headers(response, request)
        if request.url.path == "/auth" or request.url.path.startswith("/auth/"):
            response.headers["Cache-Control"] = "no-store"
            response.headers["Pragma"] = "no-cache"
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        return JSONResponse(
            {"status": "ok"},
            headers={
                "Cache-Control": "no-store",
                "Pragma": "no-cache",
            },
        )

    app.include_router(router)
    return app


__all__ = ["create_app"]
