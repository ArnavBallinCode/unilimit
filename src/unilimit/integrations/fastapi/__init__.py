"""FastAPI integration for UniLimit.

Requires the ``fastapi`` extra: ``pip install "unilimit[fastapi]"``.

Usage::

    app = FastAPI()
    limiter = Limiter.from_url("redis://localhost:6379/0", global_rules=[...])
    setup(app, limiter)

    @app.get("/profile", dependencies=[Depends(RateLimit("profile", rate="30/minute"))])
    async def profile(): ...

Order of enforcement in a request (each step is one atomic batch, and a counter
already consumed in an earlier step is never consumed again):

1. ``UniLimitMiddleware`` (before routing): exemptions, deny/allow lists, global rules.
2. ``RateLimit`` dependencies, in FastAPI's dependency order: app-level, then
   router-level (outer to inner), then route ``dependencies=[...]``, then endpoint
   parameters.

The first step that rejects ends the request; quota consumed by earlier steps stays
consumed. Response headers describe the most restrictive counter across all steps.
"""

from __future__ import annotations

try:
    import fastapi as _fastapi  # noqa: F401
except ImportError as exc:  # pragma: no cover - exercised only without the extra
    raise ImportError(
        'The FastAPI integration requires FastAPI. Install it with: pip install "unilimit[fastapi]"'
    ) from exc

from fastapi import FastAPI
from fastapi.responses import Response
from starlette.requests import Request

from unilimit.core.errors import ConfigurationError
from unilimit.core.limiter import Limiter
from unilimit.http import ResponseConfig
from unilimit.integrations.fastapi._state import RequestState, UserResolver
from unilimit.integrations.fastapi.dependency import (
    APP_STATE_LIMITER,
    APP_STATE_RESPONSE,
    RateLimit,
    RateLimitExceeded,
)
from unilimit.integrations.fastapi.middleware import UniLimitMiddleware


async def rate_limit_exceeded_handler(request: Request, exc: Exception) -> Response:
    """Render a :class:`RateLimitExceeded` with its configured body and headers."""
    assert isinstance(exc, RateLimitExceeded)
    spec = exc.spec
    return Response(
        content=spec.body,
        status_code=spec.status_code,
        headers=dict(spec.headers),
        media_type=spec.media_type,
    )


def setup(
    app: FastAPI,
    limiter: Limiter,
    *,
    response: ResponseConfig | None = None,
    close_on_shutdown: bool = True,
    user: UserResolver | None = None,
) -> None:
    """Install UniLimit on ``app``: the middleware, the 429 handler and the limiter.

    - ``response`` customises status codes, bodies and headers.
    - ``close_on_shutdown`` closes the limiter's Redis clients when the app shuts down;
      set it to False when one limiter serves several applications.
    - ``user`` resolves the user id for *global* rules from the connection. The default
      reads ``scope["user"]`` set by Starlette's ``AuthenticationMiddleware``.

    Call it once, at import time, before the application starts.
    """
    if getattr(app.state, APP_STATE_LIMITER, None) is not None:
        raise ConfigurationError("UniLimit is already set up on this application")
    setattr(app.state, APP_STATE_LIMITER, limiter)
    setattr(app.state, APP_STATE_RESPONSE, response or ResponseConfig())
    app.add_middleware(
        UniLimitMiddleware,
        limiter=limiter,
        response=response,
        close_on_shutdown=close_on_shutdown,
        user=user,
    )
    app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)


__all__ = [
    "RateLimit",
    "RateLimitExceeded",
    "RequestState",
    "ResponseConfig",
    "UniLimitMiddleware",
    "rate_limit_exceeded_handler",
    "setup",
]
