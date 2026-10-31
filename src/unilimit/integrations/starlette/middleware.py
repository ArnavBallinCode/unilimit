"""Pure ASGI middleware: application-wide limits, response headers and lifecycle.

Not built on ``BaseHTTPMiddleware``. Pure ASGI keeps streaming responses, background
tasks and context variables intact, and lets headers be added at the ``send`` level, so
they also reach responses that endpoints construct themselves.
"""

from __future__ import annotations

from starlette.requests import HTTPConnection
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from unilimit._logging import logger
from unilimit.core.limiter import Limiter
from unilimit.http import ResponseConfig, ResponseSpec, rate_limit_headers, rejection_response
from unilimit.integrations.starlette._state import (
    UserResolver,
    build_context,
    request_state,
    scope_user,
)


async def send_response(spec: ResponseSpec, send: Send) -> None:
    headers = [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in spec.headers]
    headers += [
        (b"content-type", spec.media_type.encode("latin-1")),
        (b"content-length", str(len(spec.body)).encode("latin-1")),
    ]
    await send({"type": "http.response.start", "status": spec.status_code, "headers": headers})
    await send({"type": "http.response.body", "body": spec.body})


class UniLimitMiddleware:
    """Enforces the limiter's exemptions, access lists and global rules on every HTTP
    request **before routing**, and adds rate-limit headers to every response.

    Global rules see the transport peer (resolved through trusted proxies), request
    headers and, if Starlette's ``AuthenticationMiddleware`` runs before this one,
    ``scope["user"]``. User identities that come from FastAPI dependencies are only
    known after routing: use :class:`~unilimit.integrations.fastapi.RateLimit` for those.

    On lifespan shutdown the limiter's clients are closed, unless
    ``close_on_shutdown=False`` (for a limiter shared by several applications).
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        limiter: Limiter,
        response: ResponseConfig | None = None,
        close_on_shutdown: bool = True,
        user: UserResolver | None = None,
    ) -> None:
        self.app = app
        self.limiter = limiter
        self.response = response or ResponseConfig()
        self.close_on_shutdown = close_on_shutdown
        self.user = user or scope_user

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            await self._http(scope, receive, send)
        elif scope["type"] == "lifespan" and self.close_on_shutdown:
            await self.app(scope, receive, self._lifespan_send(send))
        else:  # websockets are passed through unchanged
            await self.app(scope, receive, send)

    def _lifespan_send(self, send: Send) -> Send:
        async def wrapped(message: Message) -> None:
            if message["type"] == "lifespan.shutdown.complete":
                try:
                    await self.limiter.aclose()
                    self.limiter.close()
                except Exception:
                    logger.exception("closing the rate limiter on shutdown failed")
            await send(message)

        return wrapped

    async def _http(self, scope: Scope, receive: Receive, send: Send) -> None:
        state = request_state(scope)
        state.limiter, state.response, state.middleware = self.limiter, self.response, True

        conn = HTTPConnection(scope)
        decision = await self.limiter.aevaluate(build_context(conn, self.user(conn)))
        state.record(decision)
        if not decision.allowed:
            await send_response(rejection_response(decision, self.response), send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start" and state.results:
                existing = {k.lower() for k, _ in message.get("headers", ())}
                extra = [
                    (k.lower().encode("latin-1"), v.encode("latin-1"))
                    for k, v in rate_limit_headers(state.results, self.response)
                    if k.lower().encode("latin-1") not in existing
                ]
                if extra:
                    message = {**message, "headers": [*message.get("headers", ()), *extra]}
            await send(message)

        await self.app(scope, receive, send_with_headers)
