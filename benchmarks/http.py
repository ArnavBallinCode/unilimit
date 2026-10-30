"""HTTP-level benchmark: a FastAPI endpoint guarded by each library, driven through the
ASGI interface directly (no sockets, no HTTP parsing), so the figures isolate the cost of
the application plus the limiter.

Limits are far above the request count, so this measures the *allowed* path. SlowAPI
calls blocking ``limits`` storage from the event loop; that is how it works and is
measured as such.
"""

from __future__ import annotations

import asyncio
import gc
import time
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import Request  # module level: FastAPI resolves 'Request' from these globals

from benchmarks.common import RedisServer, RoundTrips, Timing

HIGH = "1000000000/hour"
Scope = dict[str, Any]


def scope(path: str) -> Scope:
    return {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET",
        "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"",
        "root_path": "", "headers": [(b"host", b"bench")], "client": ("203.0.113.10", 50000),
        "server": ("bench", 80),
    }  # fmt: skip


async def call(app: Any, path: str) -> int:
    status = 0

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        nonlocal status
        if message["type"] == "http.response.start":
            status = message["status"]

    await app(scope(path), receive, send)
    return status


def build_baseline() -> tuple[Any, Callable[[], Awaitable[None]]]:
    from fastapi import FastAPI

    app = FastAPI()

    @app.get("/x")
    async def x() -> dict[str, bool]:
        return {"ok": True}

    async def close() -> None:
        pass

    return app, close


def build_unilimit(url: str | None, headers: bool) -> tuple[Any, Callable[[], Awaitable[None]]]:
    from fastapi import Depends, FastAPI

    from unilimit import Limiter
    from unilimit.integrations.fastapi import RateLimit, setup

    limiter = Limiter.from_url(url or "memory://")
    app = FastAPI()
    if headers:
        setup(app, limiter)  # middleware: rate-limit headers on every response
    dep = (
        RateLimit("bench", rate=HIGH, key="ip", limiter=limiter)
        if not headers
        else RateLimit("bench", rate=HIGH, key="ip")
    )

    @app.get("/x", dependencies=[Depends(dep)])
    async def x() -> dict[str, bool]:
        return {"ok": True}

    async def close() -> None:
        await limiter.aclose()
        limiter.close()

    return app, close


def build_slowapi(url: str | None) -> tuple[Any, Callable[[], Awaitable[None]]]:
    from fastapi import FastAPI
    from slowapi import Limiter, _rate_limit_exceeded_handler
    from slowapi.errors import RateLimitExceeded
    from slowapi.util import get_remote_address

    limiter = Limiter(key_func=get_remote_address, storage_uri=url or "memory://")
    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore[arg-type]

    @app.get("/x")
    @limiter.limit(HIGH)
    async def x(request: Request) -> dict[str, bool]:
        return {"ok": True}

    async def close() -> None:
        pass

    return app, close


def build_fastapi_limiter(url: str) -> tuple[Any, Callable[[], Awaitable[None]]]:
    import redis.asyncio
    from fastapi import Depends, FastAPI
    from fastapi_limiter.depends import RateLimiter
    from pyrate_limiter import BucketFactory, Limiter, Rate, RateItem, RedisBucket

    client = redis.asyncio.Redis.from_url(url)
    rate = Rate(10**9, 3600 * 1000)
    buckets: dict[str, Any] = {}

    class PerKey(BucketFactory):
        def wrap_item(self, name: str, weight: int = 1) -> Any:
            return RateItem(name, int(time.time() * 1000), weight)

        async def get(self, item: Any) -> Any:  # type: ignore[override]
            if item.name not in buckets:
                buckets[item.name] = await RedisBucket.init([rate], client, f"bench:{item.name}")
            return buckets[item.name]

    app = FastAPI()

    @app.get("/x", dependencies=[Depends(RateLimiter(Limiter(PerKey())))])
    async def x() -> dict[str, bool]:
        return {"ok": True}

    async def close() -> None:
        await client.aclose()

    return app, close


async def measure(name: str, backend: str, app: Any, n: int, concurrency: int) -> dict[str, Any]:
    for _ in range(200):
        assert await call(app, "/x") == 200, f"{name}: warm-up request was not admitted"
    latencies: list[int] = []

    async def one() -> None:
        s = time.perf_counter_ns()
        status = await call(app, "/x")
        latencies.append(time.perf_counter_ns() - s)
        assert status == 200

    gc.collect()
    cpu0, t0 = time.process_time(), time.perf_counter()
    with RoundTrips() as trips:
        if concurrency == 1:
            for _ in range(n):
                await one()
        else:
            for start in range(0, n, concurrency):
                await asyncio.gather(*(one() for _ in range(min(concurrency, n - start))))
    timing = Timing(n, time.perf_counter() - t0, latencies, time.process_time() - cpu0)
    return {
        "contender": name,
        "backend": backend,
        "concurrency": concurrency,
        **timing.summary(),
        "round_trips_per_request": trips.count / n if trips.count else None,
    }


def run_http(server: RedisServer, n: int) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []

    async def go() -> None:
        variants: list[tuple[str, str, Callable[[], tuple[Any, Callable[[], Awaitable[None]]]]]] = [
            ("no limiter (baseline)", "none", build_baseline),
        ]
        for backend, url in (("memory", None), ("redis", server.url)):
            variants += [
                ("unilimit dependency only", backend, lambda u=url: build_unilimit(u, False)),
                (
                    "unilimit dependency + middleware headers",
                    backend,
                    lambda u=url: build_unilimit(u, True),
                ),
                ("slowapi", backend, lambda u=url: build_slowapi(u)),
            ]
        variants.append(
            (
                "fastapi-limiter 0.2 (pyrate-limiter)",
                "redis",
                lambda: build_fastapi_limiter(server.url),
            )
        )
        for name, backend, build in variants:
            for concurrency in (1, 32):
                server.flush()
                app, close = build()
                try:
                    rec = await measure(name, backend, app, n, concurrency)
                finally:
                    await close()
                records.append(rec)
                print(
                    f"http {name:45s} {backend:6s} c={concurrency:<3d} {rec['ops_per_second']:9.0f} req/s "
                    f"p50 {rec['p50_us']:8.1f}us p99 {rec['p99_us']:9.1f}us",
                    flush=True,
                )

    asyncio.run(go())
    return records
