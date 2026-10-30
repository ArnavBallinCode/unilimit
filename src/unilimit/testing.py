"""Public helpers for testing code that uses UniLimit."""

from __future__ import annotations

import threading
import uuid
from typing import TYPE_CHECKING

from unilimit.algorithms import Algorithm, LimitSpec
from unilimit.core.rate import US_PER_SECOND

if TYPE_CHECKING:
    from unilimit.backends.base import (
        AsyncBackend,
        AsyncLeaseBackend,
        SyncBackend,
        SyncLeaseBackend,
    )


class ManualClock:
    """A clock that only moves when told to. Thread-safe.

    Pass it to ``MemoryBackend(clock=...)`` for deterministic tests::

        clock = ManualClock()
        limiter = Limiter(MemoryBackend(clock=clock))
        clock.advance(60)
    """

    def __init__(self, start_us: int = 1_000_000_000_000) -> None:
        self._now = start_us
        self._lock = threading.Lock()

    def now_us(self) -> int:
        with self._lock:
            return self._now

    def advance(self, seconds: float = 0, *, us: int = 0) -> None:
        delta = round(seconds * US_PER_SECOND) + us
        if delta < 0:
            raise ValueError("use set() to move the clock backwards")
        with self._lock:
            self._now += delta

    def set(self, now_us: int) -> None:
        with self._lock:
            self._now = now_us


def _test_spec(
    algorithm: Algorithm = Algorithm.FIXED_WINDOW, limit: int = 3, period_s: int = 60
) -> LimitSpec:
    return LimitSpec(algorithm, limit, period_s * US_PER_SECOND, limit)


def verify_backend_conformance(backend: SyncBackend) -> None:
    """Validate that a custom synchronous backend strictly conforms to UniLimit invariants.

    Tests:
    1. Single op consume commits and decrements remaining quota.
    2. Over-quota op rejects and does not decrement further.
    3. Peek never mutates state.
    4. Atomic batch is all-or-nothing: if any enforcing op rejects, zero ops are committed.
    5. Multi-op batch commits all ops when all allow.
    6. Shadow ops (enforce=False) never block the batch and commit only when allowed.
    7. Empty batch returns committed=True with empty results.
    8. Duplicate keys in a batch raise ValueError.
    9. Reset clears keys.
    """
    from unilimit.backends.base import ConsumeOp

    prefix = f"test:{uuid.uuid4().hex[:8]}"
    key_a = f"{prefix}:a"
    key_b = f"{prefix}:b"
    key_full = f"{prefix}:full"
    key_shadow = f"{prefix}:shadow"
    spec = _test_spec(limit=3)

    try:
        # 1. Single op consume commits and decrements remaining quota
        op_a = ConsumeOp(key_a, spec)
        res1 = backend.consume([op_a])
        assert res1.committed, "Expected consume to commit"
        assert len(res1.results) == 1
        assert res1.results[0].allowed
        assert res1.results[0].remaining == 2

        # 2. Peek never mutates state
        peek1 = backend.peek([op_a])
        assert len(peek1) == 1
        assert peek1[0].remaining == 2
        peek2 = backend.peek([op_a])
        assert peek2[0].remaining == 2

        # Consume remaining quota
        res2 = backend.consume([op_a])
        assert res2.committed
        assert res2.results[0].remaining == 1
        res3 = backend.consume([op_a])
        assert res3.committed
        assert res3.results[0].remaining == 0

        # Over-quota op rejects and does not consume
        res4 = backend.consume([op_a])
        assert not res4.committed
        assert not res4.results[0].allowed
        assert res4.results[0].remaining == 0

        # 3. Atomic batch is all-or-nothing
        # Exhaust key_full
        op_full = ConsumeOp(key_full, _test_spec(limit=1))
        assert backend.consume([op_full]).committed

        op_b = ConsumeOp(key_b, spec)
        # Batch with key_b (has quota) and key_full (exhausted)
        batch = backend.consume([op_b, op_full])
        assert not batch.committed, "Batch should reject because key_full is exhausted"
        assert not batch.partial_commit, "Batch must not partially commit"
        # Verify key_b was NOT consumed
        peek_b = backend.peek([op_b])
        assert peek_b[0].remaining == 3, "Unadmitted batch must not consume any quota"

        # 4. Multi-op batch commits all ops when all allow
        batch_ok = backend.consume([op_b])
        assert batch_ok.committed
        assert batch_ok.results[0].remaining == 2

        # 5. Shadow ops (enforce=False)
        op_shadow = ConsumeOp(key_shadow, _test_spec(limit=1), enforce=False)
        res_shadow = backend.consume([op_b, op_shadow])
        assert res_shadow.committed
        assert res_shadow.results[1].allowed
        assert res_shadow.results[1].remaining == 0

        # Shadow op rejects but DOES NOT block the batch
        res_shadow2 = backend.consume([op_b, op_shadow])
        assert res_shadow2.committed, "Shadow op must not block enforcing ops"
        assert not res_shadow2.results[1].allowed

        # 6. Empty batch
        empty = backend.consume([])
        assert empty.committed
        assert empty.results == ()
        assert backend.peek([]) == ()

        # 7. Duplicate keys raise ValueError
        try:
            backend.consume([op_b, op_b])
            raise AssertionError("Duplicate keys must raise ValueError")
        except ValueError:
            pass

        # 8. Reset clears keys
        backend.reset([key_a, key_b])
        assert backend.peek([op_a])[0].remaining == 3
        assert backend.peek([op_b])[0].remaining == 3
    finally:
        backend.reset([key_a, key_b, key_full, key_shadow])


async def verify_async_backend_conformance(backend: AsyncBackend) -> None:
    """Validate that a custom asynchronous backend strictly conforms to UniLimit invariants."""
    from unilimit.backends.base import ConsumeOp

    prefix = f"test:{uuid.uuid4().hex[:8]}"
    key_a = f"{prefix}:a"
    key_b = f"{prefix}:b"
    key_full = f"{prefix}:full"
    key_shadow = f"{prefix}:shadow"
    spec = _test_spec(limit=3)

    try:
        op_a = ConsumeOp(key_a, spec)
        res1 = await backend.aconsume([op_a])
        assert res1.committed, "Expected aconsume to commit"
        assert len(res1.results) == 1
        assert res1.results[0].allowed
        assert res1.results[0].remaining == 2

        peek1 = await backend.apeek([op_a])
        assert len(peek1) == 1
        assert peek1[0].remaining == 2
        peek2 = await backend.apeek([op_a])
        assert peek2[0].remaining == 2

        res2 = await backend.aconsume([op_a])
        assert res2.committed
        assert res2.results[0].remaining == 1
        res3 = await backend.aconsume([op_a])
        assert res3.committed
        assert res3.results[0].remaining == 0

        res4 = await backend.aconsume([op_a])
        assert not res4.committed
        assert not res4.results[0].allowed
        assert res4.results[0].remaining == 0

        op_full = ConsumeOp(key_full, _test_spec(limit=1))
        assert (await backend.aconsume([op_full])).committed

        op_b = ConsumeOp(key_b, spec)
        batch = await backend.aconsume([op_b, op_full])
        assert not batch.committed
        assert not batch.partial_commit
        peek_b = await backend.apeek([op_b])
        assert peek_b[0].remaining == 3

        batch_ok = await backend.aconsume([op_b])
        assert batch_ok.committed
        assert batch_ok.results[0].remaining == 2

        op_shadow = ConsumeOp(key_shadow, _test_spec(limit=1), enforce=False)
        res_shadow = await backend.aconsume([op_b, op_shadow])
        assert res_shadow.committed
        assert res_shadow.results[1].allowed
        assert res_shadow.results[1].remaining == 0

        res_shadow2 = await backend.aconsume([op_b, op_shadow])
        assert res_shadow2.committed
        assert not res_shadow2.results[1].allowed

        empty = await backend.aconsume([])
        assert empty.committed
        assert empty.results == ()
        assert (await backend.apeek([])) == ()

        try:
            await backend.aconsume([op_b, op_b])
            raise AssertionError("Duplicate keys must raise ValueError")
        except ValueError:
            pass

        await backend.areset([key_a, key_b])
        assert (await backend.apeek([op_a]))[0].remaining == 3
        assert (await backend.apeek([op_b]))[0].remaining == 3
    finally:
        await backend.areset([key_a, key_b, key_full, key_shadow])


def verify_lease_backend_conformance(backend: SyncLeaseBackend) -> None:
    """Validate that a custom lease backend strictly conforms to UniLimit concurrency invariants."""
    prefix = f"test:lease:{uuid.uuid4().hex[:8]}"
    key = f"{prefix}:res"
    l1 = f"{prefix}:l1"
    l2 = f"{prefix}:l2"
    l3 = f"{prefix}:l3"
    ttl_us = 5 * US_PER_SECOND

    try:
        r1 = backend.acquire_lease(key, l1, max_concurrent=2, ttl_us=ttl_us)
        assert r1.allowed
        assert r1.current == 1
        assert r1.max_concurrent == 2

        r2 = backend.acquire_lease(key, l2, max_concurrent=2, ttl_us=ttl_us)
        assert r2.allowed
        assert r2.current == 2

        r3 = backend.acquire_lease(key, l3, max_concurrent=2, ttl_us=ttl_us)
        assert not r3.allowed
        assert r3.current == 2

        assert backend.renew_lease(key, l1, ttl_us=ttl_us)
        assert not backend.renew_lease(key, l3, ttl_us=ttl_us)

        backend.release_lease(key, l1)

        r3_retry = backend.acquire_lease(key, l3, max_concurrent=2, ttl_us=ttl_us)
        assert r3_retry.allowed
        assert r3_retry.current == 2
    finally:
        backend.release_lease(key, l1)
        backend.release_lease(key, l2)
        backend.release_lease(key, l3)


async def verify_async_lease_backend_conformance(backend: AsyncLeaseBackend) -> None:
    """Validate that a custom async lease backend conforms to concurrency invariants."""
    prefix = f"test:lease:{uuid.uuid4().hex[:8]}"
    key = f"{prefix}:res"
    l1 = f"{prefix}:l1"
    l2 = f"{prefix}:l2"
    l3 = f"{prefix}:l3"
    ttl_us = 5 * US_PER_SECOND

    try:
        r1 = await backend.aacquire_lease(key, l1, max_concurrent=2, ttl_us=ttl_us)
        assert r1.allowed
        assert r1.current == 1
        assert r1.max_concurrent == 2

        r2 = await backend.aacquire_lease(key, l2, max_concurrent=2, ttl_us=ttl_us)
        assert r2.allowed
        assert r2.current == 2

        r3 = await backend.aacquire_lease(key, l3, max_concurrent=2, ttl_us=ttl_us)
        assert not r3.allowed
        assert r3.current == 2

        assert await backend.arenew_lease(key, l1, ttl_us=ttl_us)
        assert not await backend.arenew_lease(key, l3, ttl_us=ttl_us)

        await backend.arelease_lease(key, l1)

        r3_retry = await backend.aacquire_lease(key, l3, max_concurrent=2, ttl_us=ttl_us)
        assert r3_retry.allowed
        assert r3_retry.current == 2
    finally:
        await backend.arelease_lease(key, l1)
        await backend.arelease_lease(key, l2)
        await backend.arelease_lease(key, l3)


__all__ = [
    "ManualClock",
    "verify_async_backend_conformance",
    "verify_async_lease_backend_conformance",
    "verify_backend_conformance",
    "verify_lease_backend_conformance",
]
