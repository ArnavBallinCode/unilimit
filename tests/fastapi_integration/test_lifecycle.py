"""Redis client lifecycle under FastAPI: lifespan, ownership, event loops, sync isolation."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
import redis
import redis.asyncio
from fastapi import Depends, FastAPI

from tests._servers import RedisProcess
from unilimit import ConfigurationError, Limiter, Rule
from unilimit.backends.redis import RedisBackend
from unilimit.integrations.fastapi import RateLimit, setup

from .conftest import client, lifespan, ratelimit_headers

SITE = Rule("site", "100/minute")


def build_app(limiter: Limiter, **setup_kwargs: object) -> FastAPI:
    app = FastAPI()
    setup(app, limiter, **setup_kwargs)  # type: ignore[arg-type]

    @app.get("/async", dependencies=[Depends(RateLimit("a", rate="100/minute"))])
    async def a() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/sync", dependencies=[Depends(RateLimit("s", rate="100/minute"))])
    def s() -> dict[str, bool]:
        return {"ok": True}

    return app


async def test_shutdown_closes_the_limiters_async_client(redis_url: str) -> None:
    backend = RedisBackend(redis_url)
    app = build_app(Limiter(backend, global_rules=[SITE]))
    async with lifespan(app), client(app) as c:
        assert (await c.get("/async")).status_code == 200
        assert len(backend._async) == 1  # exactly one client for this event loop
        assert (await c.get("/sync")).status_code == 200
        assert len(backend._async) == 1  # reused, not duplicated
    assert backend._async == {}  # closed on lifespan shutdown


async def test_async_request_paths_never_create_a_sync_client(redis_url: str) -> None:
    backend = RedisBackend(redis_url)
    app = build_app(Limiter(backend, global_rules=[SITE]))
    async with lifespan(app), client(app) as c:
        for path in ("/async", "/sync"):  # sync endpoints too: limiting runs on the loop
            assert ratelimit_headers(await c.get(path))
    assert backend._client is None


async def test_user_lifespan_still_runs(redis_url: str) -> None:
    events: list[str] = []

    @asynccontextmanager
    async def app_lifespan(app: FastAPI) -> AsyncIterator[None]:
        events.append("startup")
        yield
        events.append("shutdown")

    backend = RedisBackend(redis_url)
    app = FastAPI(lifespan=app_lifespan)
    setup(app, Limiter(backend, global_rules=[SITE]))

    @app.get("/x")
    async def x() -> dict[str, bool]:
        return {"ok": True}

    async with lifespan(app), client(app) as c:
        assert (await c.get("/x")).status_code == 200
    assert events == ["startup", "shutdown"]
    assert backend._async == {}


async def test_supplied_clients_are_never_closed(redis_server: RedisProcess) -> None:
    sync_client = redis.Redis(port=redis_server.port)
    async_client = redis.asyncio.Redis(port=redis_server.port)
    backend = RedisBackend(client=sync_client, async_client=async_client)
    app = build_app(Limiter(backend, global_rules=[SITE]))
    async with lifespan(app), client(app) as c:
        assert (await c.get("/async")).status_code == 200
    assert await async_client.ping()  # still usable: the application owns it
    assert sync_client.ping()
    await async_client.aclose()
    sync_client.close()


async def test_one_limiter_shared_by_two_apps(redis_url: str) -> None:
    backend = RedisBackend(redis_url)
    limiter = Limiter(backend, global_rules=[Rule("site", "3/minute")])
    first = build_app(limiter, close_on_shutdown=False)
    second = build_app(limiter, close_on_shutdown=False)
    async with lifespan(first), lifespan(second), client(first) as a, client(second) as b:
        statuses = [
            (await a.get("/async")).status_code,
            (await b.get("/async")).status_code,
            (await a.get("/sync")).status_code,
            (await b.get("/async")).status_code,
        ]
        assert len(backend._async) == 1  # one pool for both apps on this loop
    assert statuses == [200, 200, 200, 429]  # one shared counter
    assert len(backend._async) == 1  # neither app closed the shared client
    await limiter.aclose()


def test_separate_event_loops_get_separate_clients(redis_url: str) -> None:
    backend = RedisBackend(redis_url)
    app = build_app(Limiter(backend, global_rules=[Rule("site", "10/minute")]))

    async def serve_once() -> str:
        async with lifespan(app), client(app) as c:
            return ratelimit_headers(await c.get("/async"))["x-ratelimit-remaining"]

    # Each asyncio.run() is a new loop, like a server restart in the same process.
    assert [asyncio.run(serve_once()) for _ in range(3)] == ["9", "8", "7"]
    assert backend._async == {}


async def test_requests_work_after_shutdown_and_restart(redis_url: str) -> None:
    backend = RedisBackend(redis_url)
    app = build_app(Limiter(backend, global_rules=[Rule("site", "10/minute")]))
    async with lifespan(app), client(app) as c:
        assert (await c.get("/async")).status_code == 200
    async with lifespan(app), client(app) as c:  # a client is created again on demand
        response = await c.get("/async")
    assert ratelimit_headers(response)["x-ratelimit-remaining"] == "8"


def test_setup_twice_is_an_error() -> None:
    app = FastAPI()
    limiter = Limiter.from_url("memory://")
    setup(app, limiter)
    with pytest.raises(ConfigurationError, match="already set up"):
        setup(app, limiter)
