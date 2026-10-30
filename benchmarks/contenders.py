"""One adapter per library/algorithm/backend/mode, all exposing ``hit(key) -> bool`` (or
``await ahit(key)``): *consume one unit for ``key`` and say whether it was admitted*.

Every library is configured the way its documentation recommends for per-key limits.
Algorithms are matched by **semantics**, not by name; see benchmarks/README.md for what
is and is not comparable.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

# algorithm families (what the comparison is about)
FIXED = "fixed_window"
SLIDING_COUNTER = "sliding_window_counter"
SLIDING_LOG = "sliding_log"
TOKEN = "token_bucket"  # noqa: S105 - an algorithm name, not a credential


class Instance:
    """A configured limiter. Subclasses implement ``hit`` (sync) or ``ahit`` (async)."""

    is_async = False

    def hit(self, key: str) -> bool:  # pragma: no cover - interface
        raise NotImplementedError

    async def ahit(self, key: str) -> bool:  # pragma: no cover - interface
        raise NotImplementedError

    def prepare(self, keys: list[str]) -> None:
        """One-off setup for known keys, outside the measured section."""

    async def aprepare(self, keys: list[str]) -> None:
        self.prepare(keys)

    def close(self) -> None:
        pass

    async def aclose(self) -> None:
        self.close()


@dataclass(frozen=True)
class Contender:
    family: str
    algorithm: str
    backend: str  # "memory" | "redis"
    mode: str  # "sync" | "async"
    build: Callable[[str | None, int, int], Instance]  # (redis_url, limit, window_seconds)
    note: str = ""

    @property
    def name(self) -> str:
        return f"{self.family}/{self.algorithm}/{self.backend}/{self.mode}"


# -- UniLimit -------------------------------------------------------------------------


class UniLimit(Instance):
    def __init__(self, url: str | None, limit: int, window: int, algorithm: str, async_: bool,
                 observed: bool = False) -> None:  # fmt: skip
        from unilimit import InMemoryMetrics, Limiter, Rule

        kwargs: dict[str, Any] = {"observer": InMemoryMetrics()} if observed else {}
        self.limiter = Limiter.from_url(url or "memory://", **kwargs)
        burst = limit if algorithm == TOKEN else None
        # UniLimit's "sliding_window" is a sliding window *counter* (two adjacent windows).
        name = "sliding_window" if algorithm == SLIDING_COUNTER else algorithm
        self.rule = Rule("bench", f"{limit}/{window}s", algorithm=name, burst=burst)
        self.is_async = async_

    def hit(self, key: str) -> bool:
        return self.limiter.consume(self.rule, key).allowed

    async def ahit(self, key: str) -> bool:
        return (await self.limiter.aconsume(self.rule, key)).allowed

    def close(self) -> None:
        self.limiter.close()

    async def aclose(self) -> None:
        await self.limiter.aclose()
        self.limiter.close()


# -- limits (the library SlowAPI, Flask-Limiter and others build on) --------------------


def _limits_item(limit: int, window: int) -> Any:
    from limits import RateLimitItemPerSecond

    return RateLimitItemPerSecond(limit, window)  # `window` seconds


class Limits(Instance):
    def __init__(
        self, url: str | None, limit: int, window: int, algorithm: str, async_: bool
    ) -> None:
        self.is_async = async_
        self.item = _limits_item(limit, window)
        if async_:
            from limits.aio import storage as astorage
            from limits.aio import strategies as astrategies

            store = astorage.RedisStorage(url) if url else astorage.MemoryStorage()
            cls = {
                FIXED: astrategies.FixedWindowRateLimiter,
                SLIDING_COUNTER: astrategies.SlidingWindowCounterRateLimiter,
                SLIDING_LOG: astrategies.MovingWindowRateLimiter,
            }[algorithm]
        else:
            from limits import storage as sstorage
            from limits import strategies as sstrategies

            store = sstorage.RedisStorage(url) if url else sstorage.MemoryStorage()
            cls = {
                FIXED: sstrategies.FixedWindowRateLimiter,
                SLIDING_COUNTER: sstrategies.SlidingWindowCounterRateLimiter,
                SLIDING_LOG: sstrategies.MovingWindowRateLimiter,
            }[algorithm]
        self.storage = store
        self.strategy = cls(store)

    def hit(self, key: str) -> bool:
        return bool(self.strategy.hit(self.item, key))

    async def ahit(self, key: str) -> bool:
        return bool(await self.strategy.hit(self.item, key))  # type: ignore[misc]


# -- PyrateLimiter ----------------------------------------------------------------------


class Pyrate(Instance):
    """PyrateLimiter's built-in limiters share one bucket for all names; per-key limits use
    its documented ``BucketFactory`` extension point with one bucket per key."""

    def __init__(
        self, url: str | None, limit: int, window: int, algorithm: str, async_: bool
    ) -> None:
        from pyrate_limiter import (
            BucketFactory,
            InMemoryBucket,
            Limiter,
            Rate,
            RateItem,
            RedisBucket,
            StateBucket,
            TokenBucket,
        )

        self.is_async = async_
        rate = Rate(limit, window * 1000, burst=limit if algorithm == TOKEN else None)
        redis_client: Any = None
        if url:
            if async_:
                import redis.asyncio

                redis_client = redis.asyncio.Redis.from_url(url)
            else:
                import redis

                redis_client = redis.Redis.from_url(url)
        self._redis_client = redis_client
        buckets: dict[str, Any] = {}
        self._buckets = buckets

        def make_bucket(key: str) -> Any:
            if algorithm == TOKEN:
                return StateBucket([rate], algorithm=TokenBucket())
            if url:
                return RedisBucket.init([rate], redis_client, f"bench:{key}")
            return InMemoryBucket([rate])

        class PerKey(BucketFactory):
            def wrap_item(self, name: str, weight: int = 1) -> Any:
                return RateItem(name, int(time.time() * 1000), weight)

            def get(self, item: Any) -> Any:
                return buckets[item.name]

        self._make_bucket = make_bucket
        self._factory = PerKey()
        self.limiter = Limiter(self._factory)

    def prepare(self, keys: list[str]) -> None:
        for key in keys:
            if key not in self._buckets:
                bucket = self._make_bucket(key)
                if hasattr(bucket, "__await__"):
                    raise RuntimeError("use aprepare for asyncio Redis buckets")
                self._buckets[key] = bucket

    async def aprepare(self, keys: list[str]) -> None:
        for key in keys:
            if key not in self._buckets:
                bucket = self._make_bucket(key)
                if hasattr(bucket, "__await__"):
                    bucket = await bucket
                self._buckets[key] = bucket

    def hit(self, key: str) -> bool:
        return bool(self.limiter.try_acquire(key, blocking=False))

    async def ahit(self, key: str) -> bool:
        return bool(await self.limiter.try_acquire_async(key, blocking=False))

    def close(self) -> None:
        if self._redis_client is not None and not self.is_async:
            self._redis_client.close()

    async def aclose(self) -> None:
        if self._redis_client is not None and self.is_async:
            await self._redis_client.aclose()


def contenders() -> list[Contender]:
    out: list[Contender] = []

    def uni(algorithm: str, backend: str, mode: str, observed: bool = False) -> Contender:
        return Contender(
            "unilimit+metrics" if observed else "unilimit",
            algorithm,
            backend,
            mode,
            lambda url, limit, window: UniLimit(
                url, limit, window, algorithm, mode == "async", observed
            ),
        )

    def lim(algorithm: str, backend: str, mode: str) -> Contender:
        return Contender(
            "limits", algorithm, backend, mode,
            lambda url, limit, window: Limits(url, limit, window, algorithm, mode == "async"),
        )  # fmt: skip

    def pyr(algorithm: str, backend: str, mode: str) -> Contender:
        return Contender(
            "pyrate-limiter", algorithm, backend, mode,
            lambda url, limit, window: Pyrate(url, limit, window, algorithm, mode == "async"),
        )  # fmt: skip

    for backend in ("memory", "redis"):
        for mode in ("sync", "async"):
            for algorithm in (FIXED, SLIDING_COUNTER, TOKEN):
                out.append(uni(algorithm, backend, mode))
            out.append(uni(SLIDING_COUNTER, backend, mode, observed=True))
            out.append(lim(FIXED, backend, mode))
            out.append(lim(SLIDING_COUNTER, backend, mode))
            out.append(lim(SLIDING_LOG, backend, mode))
            out.append(pyr(SLIDING_LOG, backend, mode))
    # PyrateLimiter token bucket: a local state bucket only (no Redis variant)
    out.append(pyr(TOKEN, "memory", "sync"))
    out.append(pyr(TOKEN, "memory", "async"))
    return out
