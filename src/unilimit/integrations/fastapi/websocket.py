"""WebSocket limits; defined in :mod:`unilimit.integrations.starlette.websocket`."""

from __future__ import annotations

from unilimit.integrations.starlette.websocket import (
    WebSocketConnectionLimit,
    WebSocketConnectionLimitExceeded,
    WebSocketLease,
    WebSocketRateLimit,
    WebSocketRateLimitExceeded,
)

__all__ = [
    "WebSocketConnectionLimit",
    "WebSocketConnectionLimitExceeded",
    "WebSocketLease",
    "WebSocketRateLimit",
    "WebSocketRateLimitExceeded",
]
