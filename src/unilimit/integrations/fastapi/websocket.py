"""Rate limits and distributed concurrency limits for WebSocket connections and messages.

- **Connection concurrency:** :class:`WebSocketConnectionLimit` bounds the number of
  active concurrent connections across all workers and nodes for an identity (user, IP,
  or custom key). Leases feature automated background heartbeats, configurable TTLs,
  and crash recovery.
- **Connection rate limits:** :class:`WebSocketRateLimit` used as a dependency limits
  the rate of connection handshakes.
- **Message rate limits & weighted budgets:** :class:`WebSocketRateLimit.consume`
  evaluates per-message limits with support for message weights (`cost`). Configurable
  rejection policies allow dropping, replying with an error frame, closing the socket,
  or raising an exception.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any, Literal

from starlette.exceptions import WebSocketException
from starlette.status import WS_1008_POLICY_VIOLATION
from starlette.websockets import WebSocket

from unilimit._logging import logger
from unilimit.core.concurrency import ConcurrencyLimit, LeaseDecision
from unilimit.core.errors import ConfigurationError
from unilimit.core.errors import RateLimitExceeded as CoreRateLimitExceeded
from unilimit.core.identity import Identity
from unilimit.core.limiter import Limiter
from unilimit.core.result import Decision
from unilimit.integrations.fastapi._state import RequestState, build_context, scope_user
from unilimit.integrations.fastapi.dependency import APP_STATE_LIMITER, Policies


class WebSocketRateLimitExceeded(WebSocketException, CoreRateLimitExceeded):
    """Closes a WebSocket handshake with code 1008 (policy violation) when rate-limited."""

    def __init__(self, decision: Decision) -> None:
        WebSocketException.__init__(self, code=WS_1008_POLICY_VIOLATION, reason="rate limited")
        CoreRateLimitExceeded.__init__(self, decision)


class WebSocketConnectionLimitExceeded(WebSocketException):
    """Closes a WebSocket connection when its concurrency cap is reached."""

    def __init__(
        self,
        decision: LeaseDecision,
        code: int = WS_1008_POLICY_VIOLATION,
        reason: str = "concurrency limit exceeded",
    ) -> None:
        super().__init__(code=code, reason=reason)
        self.decision = decision


class WebSocketLease:
    """An active distributed concurrency lease for an open WebSocket connection."""

    def __init__(
        self,
        limiter: Limiter,
        decision: LeaseDecision,
        lease_ttl: float,
        heartbeat_interval: float,
    ) -> None:
        self.limiter = limiter
        self.decision = decision
        self.key = decision.key
        self.lease_id = decision.lease_id
        self.lease_ttl = lease_ttl
        self.heartbeat_interval = heartbeat_interval
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._released = False

    @property
    def allowed(self) -> bool:
        return self.decision.allowed

    @property
    def current(self) -> int:
        return self.decision.current

    @property
    def max_concurrent(self) -> int:
        return self.decision.max_concurrent

    @property
    def remaining(self) -> int:
        return self.decision.remaining

    async def renew(self) -> bool:
        """Extend this lease by ``lease_ttl`` seconds."""
        if self._released:
            return False
        return await self.limiter.arenew_lease(self.key, self.lease_id, self.lease_ttl)

    async def release(self) -> None:
        """Release this lease, freeing a concurrency slot immediately."""
        if self._released:
            return
        self._released = True
        self.stop_heartbeat()
        await self.limiter.arelease_lease(self.key, self.lease_id)

    def start_heartbeat(self) -> None:
        """Start background task that renews the lease periodically."""
        if self._heartbeat_task is None and not self._released:
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    def stop_heartbeat(self) -> None:
        """Stop the background renewal task."""
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            self._heartbeat_task = None

    async def _heartbeat_loop(self) -> None:
        try:
            while not self._released:
                await asyncio.sleep(self.heartbeat_interval)
                success = await self.renew()
                if not success:
                    logger.warning("WebSocket lease %s expired or failed renewal", self.lease_id)
                    break
        except asyncio.CancelledError:
            pass


class WebSocketConnectionLimit:
    """Enforces distributed concurrent connection caps on WebSocket endpoints.

    Can be used:
    1. As a dependency: ``dependencies=[Depends(WebSocketConnectionLimit("ws", max_concurrent=5))]``
    2. Directly as an async context manager:
       ``async with limit.manage(websocket) as lease: ...``
    """

    def __init__(
        self,
        name: str,
        *,
        max_concurrent: int,
        key: str | Callable[..., Any] = "ip",
        lease_ttl: float = 30.0,
        heartbeat_interval: float | None = None,
        close_code: int = WS_1008_POLICY_VIOLATION,
        close_reason: str = "concurrency limit exceeded",
        identity: Callable[..., Any] | None = None,
        limiter: Limiter | None = None,
    ) -> None:
        self.limit = ConcurrencyLimit(
            name=name,
            max_concurrent=max_concurrent,
            key=key,
            lease_ttl=lease_ttl,
            heartbeat_interval=heartbeat_interval,
        )
        self.close_code = close_code
        self.close_reason = close_reason
        self._limiter = limiter

        from fastapi import Depends

        params = [
            inspect.Parameter("websocket", inspect.Parameter.KEYWORD_ONLY, annotation=WebSocket)
        ]
        if identity is not None:
            params.append(
                inspect.Parameter(
                    "identity", inspect.Parameter.KEYWORD_ONLY, default=Depends(identity)
                )
            )
        self.__signature__ = inspect.Signature(params, return_annotation=WebSocketLease)

    def _limiter_for(self, websocket: WebSocket) -> Limiter:
        limiter = self._limiter or getattr(websocket.app.state, APP_STATE_LIMITER, None)
        if not isinstance(limiter, Limiter):
            raise ConfigurationError(
                "no Limiter found: call unilimit.integrations.fastapi.setup(app, limiter) "
                f"or pass {type(self).__name__}(..., limiter=limiter)"
            )
        return limiter

    async def acquire(self, websocket: WebSocket, *, user_id: Any = None) -> WebSocketLease:
        """Acquire a lease without entering a context manager."""
        limiter = self._limiter_for(websocket)
        uid = user_id if user_id is not None else scope_user(websocket)
        ctx = build_context(websocket, uid)

        ident: Identity | None = None
        if user_id is not None:
            ident = Identity("user", str(user_id))
        elif isinstance(self.limit.key, (str, tuple, list)) or callable(self.limit.key):
            ident = limiter.resolve_identity(ctx, self.limit.key)

        if ident is None:
            ident = Identity("custom", str(uid or ctx.client_ip or "anonymous"))

        decision = await limiter.aacquire_lease(self.limit, ident)
        return WebSocketLease(
            limiter,
            decision,
            self.limit.lease_ttl,
            self.limit.effective_heartbeat_interval,
        )

    @asynccontextmanager
    async def manage(
        self, websocket: WebSocket, *, user_id: Any = None
    ) -> AsyncIterator[WebSocketLease]:
        """Async context manager that acquires a lease, runs heartbeats, and releases on exit."""
        lease = await self.acquire(websocket, user_id=user_id)
        if not lease.allowed:
            raise WebSocketConnectionLimitExceeded(
                lease.decision, code=self.close_code, reason=self.close_reason
            )
        lease.start_heartbeat()
        try:
            yield lease
        finally:
            await lease.release()

    async def __call__(
        self, *, websocket: WebSocket, identity: Any = None
    ) -> AsyncIterator[WebSocketLease]:
        async with self.manage(websocket, user_id=identity) as lease:
            yield lease


class WebSocketRateLimit(Policies):
    """Rate limits for WebSocket routes, with support for weighted message budgets.

    - **Connections:** used as a dependency of a WebSocket route, it limits connection
      attempts. A rejected handshake is closed with code 1008 before it is accepted.
    - **Messages:** inside the handler, ``await limit.consume(websocket, cost=...)``
      evaluates message quota.
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

    async def consume(
        self, websocket: WebSocket, *, user_id: Any = None, cost: int = 1
    ) -> Decision:
        """Evaluate and consume this policy for one connection attempt or message.

        ``cost`` allows weighted message budgets (e.g. scaling by payload size).
        """
        limiter = self._limiter_for(websocket, RequestState())
        uid = user_id if user_id is not None else scope_user(websocket)
        return await limiter.aevaluate(
            build_context(websocket, uid),
            self._rules_for(limiter, websocket),
            include_global=False,
            cost=cost,
        )

    async def gate(
        self,
        websocket: WebSocket,
        *,
        user_id: Any = None,
        cost: int = 1,
        on_exceeded: Literal["raise", "drop", "reply", "close"] = "raise",
        reply_payload: Any = None,
        close_code: int = WS_1008_POLICY_VIOLATION,
        close_reason: str = "rate limit exceeded",
    ) -> Decision:
        """Consume message quota and apply the configured rejection policy if exceeded."""
        decision = await self.consume(websocket, user_id=user_id, cost=cost)
        if not decision.allowed:
            if on_exceeded == "raise":
                raise WebSocketRateLimitExceeded(decision)
            if on_exceeded == "reply":
                payload = reply_payload or {
                    "error": "rate_limited",
                    "retry_after": decision.retry_after,
                }
                if isinstance(payload, str):
                    await websocket.send_text(payload)
                else:
                    await websocket.send_json(payload)
            elif on_exceeded == "close":
                await websocket.close(code=close_code, reason=close_reason)
        return decision

    async def consume_or_drop(
        self, websocket: WebSocket, *, user_id: Any = None, cost: int = 1
    ) -> bool:
        """Consume quota and return True if admitted, False if dropped."""
        decision = await self.consume(websocket, user_id=user_id, cost=cost)
        return decision.allowed

    async def consume_or_reply(
        self,
        websocket: WebSocket,
        payload: Any = None,
        *,
        user_id: Any = None,
        cost: int = 1,
    ) -> bool:
        """Consume quota. If rejected, sends ``payload`` to the client and returns False."""
        decision = await self.gate(
            websocket,
            user_id=user_id,
            cost=cost,
            on_exceeded="reply",
            reply_payload=payload,
        )
        return decision.allowed

    async def consume_or_close(
        self,
        websocket: WebSocket,
        *,
        code: int = WS_1008_POLICY_VIOLATION,
        reason: str = "rate limit exceeded",
        user_id: Any = None,
        cost: int = 1,
    ) -> bool:
        """Consume quota. If rejected, closes the socket with ``code`` and returns False."""
        decision = await self.gate(
            websocket,
            user_id=user_id,
            cost=cost,
            on_exceeded="close",
            close_code=code,
            close_reason=reason,
        )
        return decision.allowed


__all__ = [
    "WebSocketConnectionLimit",
    "WebSocketConnectionLimitExceeded",
    "WebSocketLease",
    "WebSocketRateLimit",
    "WebSocketRateLimitExceeded",
]
