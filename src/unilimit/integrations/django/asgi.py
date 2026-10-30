"""ASGI lifespan support: close the limiter's Redis clients when the server stops.

Django's ASGI handler does not implement the lifespan protocol, so nothing tells a
Django application that the server is shutting down. Wrap the application to close the
limiter's clients on shutdown, on the server's own event loop::

    # asgi.py
    from django.core.asgi import get_asgi_application
    from unilimit.integrations.django.asgi import with_lifespan

    application = with_lifespan(get_asgi_application())

Without it, the clients are released when the process exits. WSGI servers have no
shutdown protocol for applications at all.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

from unilimit._logging import logger
from unilimit.core.limiter import Limiter
from unilimit.integrations.django.conf import get_limiter

Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]
ASGIApp = Callable[..., Awaitable[None]]


class with_lifespan:  # noqa: N801  (used like a function)
    """Wraps a Django ASGI application with a lifespan handler that closes ``limiter``
    (default: the one from ``settings.UNILIMIT``) when the server shuts down."""

    def __init__(self, app: ASGIApp, *, limiter: Limiter | None = None) -> None:
        self.app = app
        self.limiter = limiter

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "lifespan":
            await self.app(scope, receive, send)
            return
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await self._close()
                await send({"type": "lifespan.shutdown.complete"})
                return

    async def _close(self) -> None:
        try:
            limiter = self.limiter or get_limiter()
            await limiter.aclose()
            limiter.close()
        except Exception:
            logger.exception("closing the rate limiter on shutdown failed")


__all__ = ["with_lifespan"]
