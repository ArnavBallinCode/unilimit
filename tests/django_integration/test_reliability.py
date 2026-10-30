"""Redis outages, recovery, concurrency, lifecycle and several application instances."""

from __future__ import annotations

import asyncio
import threading
from collections import Counter
from collections.abc import Iterator
from typing import Any

import pytest

from tests._servers import RedisProcess, server_binary
from unilimit import Identity, Limiter, Rule
from unilimit.backends.redis import RedisBackend
from unilimit.integrations.django import conf, get_limiter

from .conftest import LimiterFactory, asgi, ratelimit_headers, wsgi

IP = Identity("ip", "203.0.113.10")


@pytest.fixture
def own_redis() -> Iterator[RedisProcess]:
    server = RedisProcess(server_binary()).start()
    yield server
    server.destroy()


def _limiter(server: RedisProcess, settings: Any, **kwargs: Any) -> Limiter:
    limiter = Limiter(
        RedisBackend(server.url, cooldown=0, socket_timeout=0.5, socket_connect_timeout=0.5),
        **kwargs,
    )
    settings.UNILIMIT = {"LIMITER": limiter}
    return limiter


# -- outage and recovery -------------------------------------------------------------------


def test_outage_and_recovery_under_wsgi(own_redis: RedisProcess, settings: Any) -> None:
    limiter = _limiter(own_redis, settings)
    c = wsgi()
    try:
        assert c.get("/sync").status_code == 200
        own_redis.stop()
        down = [c.get("/sync") for _ in range(3)]
        strict = c.get("/api/strict")  # its throttle fails closed
        lenient = c.get("/api/scoped")  # fails open
        own_redis.start()
        recovered = [c.get("/sync").status_code for _ in range(3)]
    finally:
        limiter.close()
    # Fail open: admitted, and no made-up numbers in the headers.
    assert [r.status_code for r in down] == [200] * 3
    assert ratelimit_headers(down[0]) == {}
    assert strict.status_code == 503
    assert strict.json() == {"detail": "Service temporarily unavailable, try again later."}
    assert lenient.status_code == 200
    # A fresh server lost the counters: two more allowed, then limited again.
    assert recovered == [200, 200, 429]


async def test_outage_and_recovery_under_asgi(own_redis: RedisProcess, settings: Any) -> None:
    limiter = _limiter(
        own_redis, settings, global_rules=[Rule("site-closed", "100/minute", fail_mode="closed")]
    )
    try:
        async with asgi() as c:
            assert (await c.get("/async")).status_code == 200
            own_redis.stop()
            down = await c.get("/async")
            own_redis.start()
            recovered = [(await c.get("/async")).status_code for _ in range(3)]
    finally:
        await limiter.aclose()
        limiter.close()
    assert down.status_code == 503  # the global rule fails closed
    assert down.json() == {"detail": "Service Unavailable"}
    assert recovered == [200, 200, 429]


# -- concurrency ---------------------------------------------------------------------------


async def test_concurrent_asgi_requests_never_over_admit(make_limiter: LimiterFactory) -> None:
    limiter = make_limiter(global_rules=[Rule("burst", "10/minute", algorithm="fixed_window")])
    async with asgi() as c:
        responses = await asyncio.gather(*(c.get("/aok") for _ in range(40)))
    assert Counter(r.status_code for r in responses) == {200: 10, 429: 30}
    assert limiter.peek(Rule("burst", "10/minute", algorithm="fixed_window"), IP).remaining == 0


def test_concurrent_wsgi_threads_never_over_admit(make_limiter: LimiterFactory) -> None:
    make_limiter()
    statuses: list[int] = []
    lock = threading.Lock()
    start = threading.Barrier(16, timeout=30)

    def worker() -> None:
        c = wsgi()
        start.wait()
        for _ in range(3):
            code = c.post("/api/rules").status_code
            with lock:
                statuses.append(code)

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert Counter(statuses) == {200: 2, 429: 46}


async def test_two_application_instances_share_exact_limits(redis_url: str, settings: Any) -> None:
    """Two ASGI applications, each with its own Limiter and Redis clients (as in two
    processes), share counters exactly."""
    first = Limiter(RedisBackend(redis_url))
    second = Limiter(RedisBackend(redis_url))
    try:
        settings.UNILIMIT = {"LIMITER": first}
        async with asgi() as one:
            statuses = [(await one.get("/async")).status_code]
        settings.UNILIMIT = {"LIMITER": second}
        async with asgi() as two:
            statuses += [(await two.get("/async")).status_code for _ in range(2)]
    finally:
        for limiter in (first, second):
            await limiter.aclose()
            limiter.close()
    assert statuses == [200, 200, 429]


# -- lifecycle -----------------------------------------------------------------------------


async def test_url_configured_limiter_lifecycle(redis_url: str, settings: Any) -> None:
    settings.UNILIMIT = {"URL": redis_url, "OPTIONS": {"namespace": "lifecycle"}}
    limiter = get_limiter()
    assert get_limiter() is limiter  # cached
    assert limiter.namespace == "lifecycle"
    async with asgi() as c:
        assert (await c.get("/async")).status_code == 200
    assert wsgi().get("/sync").status_code == 200  # sync client too
    await limiter.aclose()  # async clients belong to their event loop
    settings.UNILIMIT = {"URL": redis_url}  # setting_changed: the old one is closed
    replacement = get_limiter()
    assert replacement is not limiter
    assert wsgi().get("/sync").status_code == 200
    conf.reset()


def test_wsgi_async_views_use_the_sync_client(make_limiter: LimiterFactory) -> None:
    """Under WSGI an async view runs in a throwaway event loop; limiting it must not
    create an async Redis client bound to that loop (a new connection per request)."""
    limiter = make_limiter()
    backend = limiter.backend
    c = wsgi()
    assert [c.get("/async").status_code for _ in range(3)] == [200, 200, 429]
    if isinstance(backend, RedisBackend):
        assert backend._async == {}


# -- streaming -----------------------------------------------------------------------------


async def test_streaming_responses(make_limiter: LimiterFactory) -> None:
    make_limiter()
    async with asgi() as c:  # an async iterator: what Django expects under ASGI
        first = await c.get("/astream")
        second = await c.get("/astream")
        third = await c.get("/astream")
    assert first.content == b"ab"
    assert ratelimit_headers(first)["x-ratelimit-remaining"] == "1"
    assert ratelimit_headers(second)["x-ratelimit-remaining"] == "0"
    assert third.status_code == 429


def test_streaming_response_under_wsgi(memory_limiter: LimiterFactory) -> None:
    memory_limiter()
    response: Any = wsgi().get("/stream")
    assert b"".join(response.streaming_content) == b"ab"
    assert response["X-RateLimit-Remaining"] == "1"
    response.close()


async def test_with_lifespan_closes_the_clients_on_shutdown(redis_url: str, settings: Any) -> None:
    from django.core.handlers.asgi import ASGIHandler

    from unilimit.integrations.django.asgi import with_lifespan

    from .conftest import drive_lifespan

    settings.UNILIMIT = {"URL": redis_url}
    limiter = get_limiter()
    backend = limiter.backend
    assert isinstance(backend, RedisBackend)
    app = with_lifespan(ASGIHandler())
    async with drive_lifespan(app):
        async with asgi(app=app) as c:
            assert (await c.get("/async")).status_code == 200
        assert wsgi().get("/sync").status_code == 200
        assert backend._async
        assert backend._client is not None
    assert backend._async == {}  # closed on the server's loop
    assert backend._client is None
