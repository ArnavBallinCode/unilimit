"""Shared helpers: real FastAPI apps driven over ASGI with HTTPX."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from unilimit import Limiter
from unilimit.backends.redis import RedisBackend
from unilimit.testing import ManualClock


@asynccontextmanager
async def client(app: Any, **kwargs: Any) -> AsyncIterator[httpx.AsyncClient]:
    kwargs.setdefault("base_url", "http://testserver")
    transport = httpx.ASGITransport(app=app, client=kwargs.pop("peer", ("203.0.113.10", 5000)))
    async with httpx.AsyncClient(transport=transport, **kwargs) as c:
        yield c


@asynccontextmanager
async def lifespan(app: Any) -> AsyncIterator[None]:
    """Drive the ASGI lifespan protocol (startup, then shutdown) like a server does."""
    to_app: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    from_app: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    task = asyncio.create_task(
        app({"type": "lifespan", "asgi": {"version": "3.0"}, "state": {}}, to_app.get, from_app.put)
    )
    await to_app.put({"type": "lifespan.startup"})
    assert (await from_app.get())["type"] == "lifespan.startup.complete"
    try:
        yield
    finally:
        await to_app.put({"type": "lifespan.shutdown"})
        assert (await from_app.get())["type"] == "lifespan.shutdown.complete"
        await task


def ratelimit_headers(response: httpx.Response) -> dict[str, str]:
    return {
        k.lower(): v
        for k, v in response.headers.items()
        if k.lower().startswith(("x-ratelimit", "retry-after", "ratelimit"))
    }


LimiterFactory = Callable[..., Limiter]


@pytest.fixture(params=["memory", "redis", "cluster"])
async def make_limiter(request: pytest.FixtureRequest) -> AsyncIterator[LimiterFactory]:
    """Builds limiters on every backend; Redis kinds use real servers."""
    created: list[Limiter] = []
    clock = ManualClock()

    def factory(**kwargs: Any) -> Limiter:
        if request.param == "memory":
            limiter = Limiter.from_url("memory://", clock=clock, **kwargs)
        elif request.param == "redis":
            limiter = Limiter(RedisBackend(request.getfixturevalue("redis_url")), **kwargs)
        else:
            url = request.getfixturevalue("cluster_url")
            limiter = Limiter(RedisBackend(url, cluster=True), **kwargs)
        created.append(limiter)
        return limiter

    yield factory
    for limiter in created:  # on the test's own event loop
        await limiter.aclose()
        limiter.close()


def new_app(**kwargs: Any) -> FastAPI:
    return FastAPI(**kwargs)
