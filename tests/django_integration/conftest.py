"""Helpers for the Django and DRF tests: real WSGI and ASGI request cycles."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from asgiref.sync import sync_to_async
from django.core.handlers.asgi import ASGIHandler
from django.db import connections
from django.test import Client

from unilimit import Limiter
from unilimit.backends.redis import RedisBackend
from unilimit.integrations.django import conf
from unilimit.testing import ManualClock

PEER = "203.0.113.10"

LimiterFactory = Callable[..., Limiter]


@pytest.fixture(autouse=True)
def _fresh_config() -> Iterator[None]:
    conf.reset()
    yield
    conf.reset()


@pytest.fixture(autouse=True)
async def _close_thread_connections() -> AsyncIterator[None]:
    """Django opens a database connection per thread. Sync code reached from async code
    (ASGI handler, ``sync_to_async``) runs in asgiref's shared thread; close its
    connections so the per-test leak check stays meaningful."""
    yield
    await sync_to_async(connections.close_all)()


def _factory(
    kind: str, request: pytest.FixtureRequest, settings: Any, created: list[Limiter]
) -> LimiterFactory:
    clock = ManualClock()

    def factory(**kwargs: Any) -> Limiter:
        if kind == "memory":
            limiter = Limiter.from_url("memory://", clock=clock, **kwargs)
        elif kind == "redis":
            limiter = Limiter(RedisBackend(request.getfixturevalue("redis_url")), **kwargs)
        else:
            url = request.getfixturevalue("cluster_url")
            limiter = Limiter(RedisBackend(url, cluster=True), **kwargs)
        created.append(limiter)
        settings.UNILIMIT = {"LIMITER": limiter}
        return limiter

    return factory


@pytest.fixture(params=["memory", "redis", "cluster"])
async def make_limiter(
    request: pytest.FixtureRequest, settings: Any
) -> AsyncIterator[LimiterFactory]:
    """Builds a limiter on every backend and installs it as ``UNILIMIT["LIMITER"]``."""
    created: list[Limiter] = []
    yield _factory(request.param, request, settings, created)
    for limiter in created:  # on the test's own event loop
        await limiter.aclose()
        limiter.close()


@pytest.fixture
def memory_limiter(request: pytest.FixtureRequest, settings: Any) -> LimiterFactory:
    """Memory backend only, for behaviour that does not depend on the backend."""
    return _factory("memory", request, settings, [])


def wsgi(peer: str = PEER, **kwargs: Any) -> Client:
    """Django's test client: the real WSGI handler and a sync middleware chain."""
    return Client(REMOTE_ADDR=peer, **kwargs)


@asynccontextmanager
async def asgi(
    peer: str = PEER, app: Any = None, **kwargs: Any
) -> AsyncIterator[httpx.AsyncClient]:
    """Django's real ASGI handler (async middleware chain), driven over HTTPX."""
    handler: Any = app or ASGIHandler()
    transport = httpx.ASGITransport(app=handler, client=(peer, 5000))
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver", **kwargs
    ) as client:
        yield client


def ratelimit_headers(response: Any) -> dict[str, str]:
    return {
        k.lower(): v
        for k, v in response.headers.items()
        if k.lower().startswith(("x-ratelimit", "retry-after", "ratelimit"))
    }


@asynccontextmanager
async def drive_lifespan(app: Any) -> AsyncIterator[None]:
    """Drive the ASGI lifespan protocol (startup, then shutdown) like a server does."""
    to_app: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    from_app: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    task = asyncio.create_task(
        app({"type": "lifespan", "asgi": {"version": "3.0"}}, to_app.get, from_app.put)
    )
    await to_app.put({"type": "lifespan.startup"})
    assert (await from_app.get())["type"] == "lifespan.startup.complete"
    try:
        yield
    finally:
        await to_app.put({"type": "lifespan.shutdown"})
        assert (await from_app.get())["type"] == "lifespan.shutdown.complete"
        await task
