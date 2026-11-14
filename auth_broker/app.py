"""FastAPI application factory for the authentication Broker."""

from __future__ import annotations

from dotenv import load_dotenv
from fastapi import FastAPI, Request

from auth_broker.config import BrokerConfig, RateLimitConfig
from auth_broker.oauth import GitHubOAuthClient
from auth_broker.rate_limit import InMemoryRateLimiter
from auth_broker.routes import router
from auth_broker.transactions import TransactionStore


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

    app = FastAPI(
        title="DREAMEROL Auth Broker",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.broker_config = config
    app.state.transaction_store = transaction_store
    app.state.oauth_client = oauth_client
    app.state.rate_limit_config = rate_limit_config
    app.state.rate_limiter = rate_limiter

    @app.middleware("http")
    async def add_auth_security_headers(request: Request, call_next):
        response = await call_next(request)
        if request.url.path == "/auth" or request.url.path.startswith("/auth/"):
            response.headers["Cache-Control"] = "no-store"
            response.headers["Pragma"] = "no-cache"
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    app.include_router(router)
    return app


__all__ = ["create_app"]
