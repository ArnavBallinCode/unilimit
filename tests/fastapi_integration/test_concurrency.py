"""Concurrency and Redis failure behaviour through real ASGI requests."""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Iterator

import pytest
from fastapi import Depends, FastAPI, Header

from tests._servers import RedisProcess, server_binary
from unilimit import Identity, Limiter, Rule
from unilimit.backends.redis import RedisBackend
from unilimit.integrations.fastapi import RateLimit, setup

from .conftest import LimiterFactory, client, lifespan, ratelimit_headers


def user_from_header(x_user: str | None = Header(None)) -> str | None:
    return x_user


async def test_concurrent_requests_never_over_admit(make_limiter: LimiterFactory) -> None:
    app = FastAPI()
    setup(app, make_limiter())
    limit = RateLimit("hot", rate="50/day", algorithm="token_bucket")

    @app.get("/async", dependencies=[Depends(limit)])
    async def a() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/sync", dependencies=[Depends(limit)])
    def s() -> dict[str, bool]:
        return {"ok": True}

    async with client(app) as c:
        responses = await asyncio.gather(
            *(c.get("/async" if i % 2 else "/sync") for i in range(300))
        )
    statuses = Counter(r.status_code for r in responses)
    assert statuses == {200: 50, 429: 250}
    remaining = sorted(int(r.headers["x-ratelimit-remaining"]) for r in responses if r.is_success)
    assert remaining == list(range(50))  # every admission saw a distinct quota slot


async def test_concurrent_requests_through_middleware(make_limiter: LimiterFactory) -> None:
    app = FastAPI()
    setup(app, make_limiter(global_rules=[Rule("site", "40/day", algorithm="fixed_window")]))

    @app.get("/x")
    async def x() -> dict[str, bool]:
        return {"ok": True}

    async with client(app) as c:
        responses = await asyncio.gather(*(c.get("/x") for _ in range(200)))
    assert Counter(r.status_code for r in responses) == {200: 40, 429: 160}


async def test_concurrent_users_have_independent_exact_limits(
    make_limiter: LimiterFactory,
) -> None:
    app = FastAPI()
    setup(app, make_limiter())
    dep = RateLimit("per-user", rate="10/day", identity=user_from_header)

    @app.get("/me", dependencies=[Depends(dep)])
    async def me() -> dict[str, bool]:
        return {"ok": True}

    async with client(app) as c:
        responses = await asyncio.gather(
            *(c.get("/me", headers={"x-user": f"u{i % 8}"}) for i in range(240))
        )
    admitted = Counter(r.request.headers["x-user"] for r in responses if r.status_code == 200)
    assert admitted == {f"u{i}": 10 for i in range(8)}


async def test_cross_identity_rules_under_concurrency(make_limiter: LimiterFactory) -> None:
    """Per-user and per-IP rules in one dependency (different hash slots on Cluster):
    the IP limit is never exceeded, whatever the backend."""
    limiter = make_limiter()
    app = FastAPI()
    setup(app, limiter)
    per_user = Rule("per-user", "100/day", key="user")
    per_ip = Rule("per-ip", "60/day", key="ip", algorithm="fixed_window")

    @app.get("/x", dependencies=[Depends(RateLimit(per_user, per_ip, identity=user_from_header))])
    async def x() -> dict[str, bool]:
        return {"ok": True}

    async with client(app) as c:
        responses = await asyncio.gather(
            *(c.get("/x", headers={"x-user": f"u{i % 6}"}) for i in range(240))
        )
    assert sum(r.status_code == 200 for r in responses) == 60
    assert limiter.peek(per_ip, Identity("ip", "203.0.113.10")).remaining == 0


# -- real Redis outages -----------------------------------------------------------------


@pytest.fixture
def server() -> Iterator[RedisProcess]:
    s = RedisProcess(server_binary()).start()
    yield s
    s.destroy()


@pytest.mark.parametrize("fail_mode", ["open", "closed"])
async def test_redis_outage_and_recovery(server: RedisProcess, fail_mode: str) -> None:
    limiter = Limiter(
        RedisBackend(server.url, cooldown=0.2),
        global_rules=[Rule("site", "100/minute")],
        fail_mode=fail_mode,  # type: ignore[arg-type]
    )
    app = FastAPI()
    setup(app, limiter)

    @app.get("/x", dependencies=[Depends(RateLimit("x", rate="100/minute"))])
    async def x() -> dict[str, bool]:
        return {"ok": True}

    async with lifespan(app), client(app) as c:
        healthy = await c.get("/x")
        assert healthy.status_code == 200
        assert ratelimit_headers(healthy)["x-ratelimit-remaining"] == "99"

        server.stop()
        down = await c.get("/x")
        if fail_mode == "open":
            assert down.status_code == 200
            assert ratelimit_headers(down) == {}  # no invented numbers while degraded
        else:
            assert down.status_code == 503
            assert down.json() == {"detail": "Service Unavailable"}  # no internals leaked

        server.start()
        await asyncio.sleep(0.25)  # breaker cooldown
        for _ in range(50):
            recovered = await c.get("/x")
            if ratelimit_headers(recovered):
                break
            await asyncio.sleep(0.05)
        assert recovered.status_code == 200
        assert ratelimit_headers(recovered)["x-ratelimit-remaining"] == "99"  # fresh server


async def test_dependency_fail_closed_rule_on_fail_open_limiter(server: RedisProcess) -> None:
    limiter = Limiter(RedisBackend(server.url, cooldown=0), fail_mode="open")
    app = FastAPI()
    setup(app, limiter)

    @app.post(
        "/login", dependencies=[Depends(RateLimit("login", rate="5/minute", fail_mode="closed"))]
    )
    async def login() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/feed", dependencies=[Depends(RateLimit("feed", rate="5/minute"))])
    async def feed() -> dict[str, bool]:
        return {"ok": True}

    async with lifespan(app), client(app) as c:
        server.stop()
        assert (await c.post("/login")).status_code == 503  # security-sensitive: closed
        assert (await c.get("/feed")).status_code == 200  # everything else: open
