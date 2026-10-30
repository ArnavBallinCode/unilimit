"""Starlette integration for UniLimit.

Provides pure ASGI middleware and WebSocket limits for Starlette applications.

Usage::

    from starlette.applications import Starlette
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

from unilimit.integrations.fastapi.dependency import (
    APP_STATE_LIMITER,
    APP_STATE_RESPONSE,
    RateLimitExceeded,
)
from unilimit.integrations.fastapi.middleware import UniLimitMiddleware
from unilimit.integrations.fastapi.websocket import (
    WebSocketConnectionLimit,
    WebSocketConnectionLimitExceeded,
    WebSocketLease,
    WebSocketRateLimit,
    WebSocketRateLimitExceeded,
)

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
