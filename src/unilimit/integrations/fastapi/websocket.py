"""Rate limits for WebSocket connections and messages.

Adapted from the idea of fastapi-limiter's ``WebSocketRateLimiter`` (Apache-2.0); no
code was copied. Unlike it, this limits both the handshake and individual messages,
and uses the same rules, identities and atomic batches as HTTP limits.
"""

from __future__ import annotations

import inspect
from typing import Any

from starlette.exceptions import WebSocketException
from starlette.status import WS_1008_POLICY_VIOLATION
from starlette.websockets import WebSocket

from unilimit.core.errors import RateLimitExceeded as CoreRateLimitExceeded
from unilimit.core.result import Decision
from unilimit.integrations.fastapi._state import RequestState, build_context, scope_user
from unilimit.integrations.fastapi.dependency import Policies


class WebSocketRateLimitExceeded(WebSocketException, CoreRateLimitExceeded):
    """Closes a WebSocket handshake with code 1008 (policy violation)."""

    def __init__(self, decision: Decision) -> None:
        WebSocketException.__init__(self, code=WS_1008_POLICY_VIOLATION, reason="rate limited")
        CoreRateLimitExceeded.__init__(self, decision)


class WebSocketRateLimit(Policies):
    """Rate limits for WebSocket routes, with the same options as ``RateLimit``.

    - **Connections:** used as a dependency of a WebSocket route, it limits connection
      attempts. A rejected handshake is closed with code 1008 before it is accepted.
    - **Messages:** inside the handler, ``await limit.consume(websocket)`` limits one
      message and returns the :class:`~unilimit.Decision`. Your handler decides what to
      do with a rejected message: drop it, answer with an error frame, or close.

    Each message is a separate, atomic evaluation. WebSockets do not pass through the
    HTTP middleware, so global rules do not apply to them.
    """

    def _connection_parameters(self) -> list[inspect.Parameter]:
        return [
            inspect.Parameter("websocket", inspect.Parameter.KEYWORD_ONLY, annotation=WebSocket)
        ]

    async def __call__(self, *, websocket: WebSocket, identity: Any = None) -> Decision:
        decision = await self.consume(websocket, user_id=identity)
        if not decision.allowed:
            raise WebSocketRateLimitExceeded(decision)
        return decision

    async def consume(self, websocket: WebSocket, *, user_id: Any = None) -> Decision:
        """Evaluate and consume this policy for one connection attempt or message.

        ``user_id`` identifies the user for ``"user"`` keys (for example the id your
        handler authenticated at connect time). Without it, Starlette's
        ``scope["user"]`` is used if present.
        """
        limiter = self._limiter_for(websocket, RequestState())
        uid = user_id if user_id is not None else scope_user(websocket)
        return await limiter.aevaluate(
            build_context(websocket, uid),
            self._rules_for(limiter, websocket),
            include_global=False,
        )


__all__ = ["WebSocketRateLimit", "WebSocketRateLimitExceeded"]
