"""Real concurrency against the memory backend: exact admission counts, no timing asserts.

The clock is frozen, so every attempt competes for the same quota and the expected
number of admissions is exact. Redis and multi-process variants arrive in Milestone 3.
"""

from __future__ import annotations

import asyncio
import sys
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor

import pytest

from unilimit import Identity, Limiter, MemoryBackend, RequestContext, Rule
from unilimit.testing import ManualClock

THREADS = 32


@pytest.fixture(autouse=True)
def aggressive_thread_switching() -> Iterator[None]:
    """Force very frequent GIL hand-offs so unsynchronised code actually races."""
    previous = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    yield
    sys.setswitchinterval(previous)


def hammer(n_threads: int, attempts: int, fn: object) -> list[bool]:
    barrier = threading.Barrier(n_threads)
    results: list[bool] = []
    lock = threading.Lock()

    def worker(i: int) -> None:
        barrier.wait()
        local = [fn(i) for _ in range(attempts)]  # type: ignore[operator]
        with lock:
            results.extend(local)

    with ThreadPoolExecutor(n_threads) as pool:
        list(pool.map(worker, range(n_threads)))
    return results


def test_threads_same_identity_never_over_admit() -> None:
    for algorithm in ("fixed_window", "sliding_window", "token_bucket"):
        limiter = Limiter(MemoryBackend(clock=ManualClock()))
        rule = Rule("r", "500/minute", algorithm=algorithm)
        results = hammer(
            THREADS, 50, lambda _i, r=rule, lim=limiter: lim.consume(r, "same").allowed
        )
        assert len(results) == THREADS * 50
        assert sum(results) == 500, algorithm


def test_threads_different_identities_are_independent() -> None:
    limiter = Limiter(MemoryBackend(clock=ManualClock()))
    rule = Rule("r", "10/minute")
    results = hammer(THREADS, 25, lambda i: limiter.consume(rule, f"user-{i}").allowed)
    assert sum(results) == THREADS * 10


def test_threads_multi_rule_batches_are_atomic() -> None:
    """A shared global quota plus per-user quotas, all consumed atomically.

    The global limit (300) is below the sum of the per-user limits (16 x 50 = 800).
    Every admitted request consumed exactly one unit from both rules, and rejected
    requests consumed nothing, so per-user consumption sums to exactly 300.
    """
    limiter = Limiter(MemoryBackend(clock=ManualClock()))
    per_user = Rule("per-user", "50/minute", key="user")
    shared = Rule("shared", "300/minute", key="global")
    n = 16

    def attempt(i: int) -> bool:
        ctx = RequestContext(user_id=f"u{i}")
        return limiter.evaluate(ctx, [per_user, shared]).allowed

    results = hammer(n, 60, attempt)
    assert sum(results) == 300
    consumed = sum(
        50 - limiter.peek(per_user, Identity("user", f"u{i}")).remaining for i in range(n)
    )
    assert consumed == 300
    assert limiter.peek(shared, Identity("global", "*")).remaining == 0


async def test_asyncio_gather_never_over_admits() -> None:
    limiter = Limiter(MemoryBackend(clock=ManualClock()))
    rule = Rule("r", "100/minute", algorithm="token_bucket")
    results = await asyncio.gather(*(limiter.aconsume(rule, "same") for _ in range(1_000)))
    assert sum(r.allowed for r in results) == 100


async def test_asyncio_and_threads_share_one_backend() -> None:
    limiter = Limiter(MemoryBackend(clock=ManualClock()))
    rule = Rule("r", "200/minute")

    async def async_side() -> int:
        results = await asyncio.gather(*(limiter.aconsume(rule, "x") for _ in range(200)))
        return sum(r.allowed for r in results)

    thread_results, async_admitted = await asyncio.gather(
        asyncio.to_thread(hammer, 8, 25, lambda _i: limiter.consume(rule, "x").allowed),
        async_side(),
    )
    assert sum(thread_results) + async_admitted == 200
