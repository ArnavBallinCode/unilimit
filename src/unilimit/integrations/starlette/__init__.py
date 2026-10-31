"""Starlette integration for UniLimit.

Provides pure ASGI middleware and WebSocket limits for Starlette applications. Needs only
the ``starlette`` extra (``pip install "unilimit[starlette]"``), not FastAPI.

Usage::

    from starlette.applications import Starlette
    from starlette.middleware import Middleware
    from starlette.routing import Route, WebSocketRoute
    from unilimit import Limiter
    from unilimit.integrations.starlette import UniLimitMiddleware, WebSocketConnectionLimit

    limiter = Limiter(backend=...)
    conn_limit = WebSocketConnectionLimit("ws-conn", max_concurrent=5, limiter=limiter)

    async def ws_endpoint(websocket):
        async with conn_limit.manage(websocket):
            await websocket.accept()
            ...

    app = Starlette(
        routes=[WebSocketRoute("/ws", ws_endpoint)],
        middleware=[Middleware(UniLimitMiddleware, limiter=limiter)],
    )
"""

from __future__ import annotations

from typing import Any

from unilimit.integrations.starlette._policies import APP_STATE_LIMITER, APP_STATE_RESPONSE
from unilimit.integrations.starlette.middleware import UniLimitMiddleware
from unilimit.integrations.starlette.websocket import (
    WebSocketConnectionLimit,
    WebSocketConnectionLimitExceeded,
    WebSocketLease,
    WebSocketRateLimit,
    WebSocketRateLimitExceeded,
)


def __getattr__(name: str) -> Any:
    # Raised only by the FastAPI dependency; importing it here eagerly would require FastAPI.
    if name == "RateLimitExceeded":
        from unilimit.integrations.fastapi.dependency import RateLimitExceeded

        return RateLimitExceeded
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "APP_STATE_LIMITER",
    "APP_STATE_RESPONSE",
    "RateLimitExceeded",
    "UniLimitMiddleware",
    "WebSocketConnectionLimit",
    "WebSocketConnectionLimitExceeded",
    "WebSocketLease",
    "WebSocketRateLimit",
    "WebSocketRateLimitExceeded",
]
