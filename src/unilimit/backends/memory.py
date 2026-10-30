"""In-memory backend for development, tests and single-process deployments.

State is per process: N workers mean N independent limits. Use the Redis backend for
anything shared.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Sequence

from unilimit._logging import logger
from unilimit.algorithms import Algorithm, State
from unilimit.backends.base import (
    BatchResult,
    ConsumeOp,
    LeaseResult,
    OpResult,
    check_unique_keys,
    evaluate_batch,
    peek_batch,
)
from unilimit.core.clock import Clock, MonotonicClock
from unilimit.core.errors import BackendError

_SWEEP_EVERY = 1024
_SWEEP_BUDGET = 128


class MemoryBackend:
    """Thread-safe in-memory backend implementing both sync and async protocols.

    Batches are fully atomic: every operation runs under one lock. The async methods run
    the same code without awaiting inside the critical section. The work is CPU-only and
    takes microseconds, so holding a ``threading.Lock`` briefly is safe on an event loop.

    ``max_keys`` bounds memory under identity-rotation attacks. When exceeded, the
    least-recently-used entries are evicted; evicting a live entry resets that counter.
    """

    name = "memory"

    def __init__(self, *, clock: Clock | None = None, max_keys: int = 100_000) -> None:
        if isinstance(max_keys, bool) or not isinstance(max_keys, int) or max_keys < 1:
            raise ValueError("max_keys must be a positive integer")
        self._clock: Clock = clock if clock is not None else MonotonicClock()
        self._max_keys = max_keys
        self._data: OrderedDict[str, tuple[Algorithm, State, int]] = OrderedDict()
        self._leases: dict[str, dict[str, int]] = {}
        self._lock = threading.Lock()
        self._ops_since_sweep = 0
        self.live_evictions = 0
        """Number of unexpired entries evicted because ``max_keys`` was exceeded."""

    # -- internals (call with the lock held) -------------------------------------------

    def _read(self, op: ConsumeOp, now_us: int) -> State | None:
        entry = self._data.get(op.key)
        if entry is None or entry[2] <= now_us:
            return None
        if entry[0] is not op.spec.algorithm:
            raise BackendError(
                f"state for key {op.key!r} was written by {entry[0].value}, "
                f"not {op.spec.algorithm.value}"
            )
        return entry[1]

    def _write(self, key: str, algorithm: Algorithm, state: State, expires_at: int) -> None:
        self._data[key] = (algorithm, state, expires_at)
        self._data.move_to_end(key)

    def _evict(self, now_us: int) -> None:
        while len(self._data) > self._max_keys:
            _, (_, _, expires_at) = self._data.popitem(last=False)
            if expires_at > now_us:
                self.live_evictions += 1
                if self.live_evictions == 1:
                    logger.warning(
                        "MemoryBackend exceeded max_keys=%d and evicted a live counter; "
                        "limits for evicted identities restart. Raise max_keys or use Redis.",
                        self._max_keys,
                    )

    def _sweep(self, now_us: int) -> None:
        self._ops_since_sweep += 1
        if self._ops_since_sweep < _SWEEP_EVERY:
            return
        self._ops_since_sweep = 0
        expired = []
        for i, (key, (_, _, expires_at)) in enumerate(self._data.items()):
            if i >= _SWEEP_BUDGET:
                break
            if expires_at <= now_us:
                expired.append(key)
        for key in expired:
            del self._data[key]

    # -- sync protocol -----------------------------------------------------------------

    def consume(self, ops: Sequence[ConsumeOp]) -> BatchResult:
        check_unique_keys(ops)
        with self._lock:
            now = self._clock.now_us()
            states = [self._read(op, now) for op in ops]
            result, writes = evaluate_batch(ops, states, now)
            algorithms = {op.key: op.spec.algorithm for op in ops}
            for write in writes:
                self._write(write.key, algorithms[write.key], write.state, now + write.ttl_us)
            if writes:
                self._evict(now)
            self._sweep(now)
            return result

    def peek(self, ops: Sequence[ConsumeOp]) -> tuple[OpResult, ...]:
        check_unique_keys(ops)
        with self._lock:
            now = self._clock.now_us()
            return peek_batch(ops, [self._read(op, now) for op in ops], now)

    def reset(self, keys: Sequence[str]) -> None:
        with self._lock:
            for key in keys:
                self._data.pop(key, None)
                self._leases.pop(key, None)

    def close(self) -> None:
        """No resources to release; stored state is kept."""

    def acquire_lease(
        self, key: str, lease_id: str, max_concurrent: int, ttl_us: int, now_us: int = 0
    ) -> LeaseResult:
        with self._lock:
            if now_us <= 0:
                now_us = self._clock.now_us()
            active = self._leases.setdefault(key, {})
            # Purge expired leases
            expired = [lid for lid, exp in active.items() if exp <= now_us]
            for lid in expired:
                del active[lid]

            # Already held: renew and allow
            if lease_id in active:
                active[lease_id] = now_us + ttl_us
                return LeaseResult(allowed=True, current=len(active), max_concurrent=max_concurrent)

            if len(active) >= max_concurrent:
                earliest_exp = min(active.values()) if active else now_us
                retry_after_us = max(0, earliest_exp - now_us)
                return LeaseResult(
                    allowed=False,
                    current=len(active),
                    max_concurrent=max_concurrent,
                    retry_after_us=retry_after_us,
                )

            active[lease_id] = now_us + ttl_us
            return LeaseResult(allowed=True, current=len(active), max_concurrent=max_concurrent)

    def renew_lease(self, key: str, lease_id: str, ttl_us: int, now_us: int = 0) -> bool:
        with self._lock:
            if now_us <= 0:
                now_us = self._clock.now_us()
            active = self._leases.get(key)
            if active is None:
                return False
            expired = [lid for lid, exp in active.items() if exp <= now_us]
            for lid in expired:
                del active[lid]
            if lease_id in active:
                active[lease_id] = now_us + ttl_us
                return True
            return False

    def release_lease(self, key: str, lease_id: str) -> None:
        with self._lock:
            active = self._leases.get(key)
            if active is not None:
                active.pop(lease_id, None)
                if not active:
                    self._leases.pop(key, None)

    # -- async protocol ----------------------------------------------------------------

    async def aconsume(self, ops: Sequence[ConsumeOp]) -> BatchResult:
        return self.consume(ops)

    async def apeek(self, ops: Sequence[ConsumeOp]) -> tuple[OpResult, ...]:
        return self.peek(ops)

    async def areset(self, keys: Sequence[str]) -> None:
        self.reset(keys)

    async def aclose(self) -> None:
        self.close()

    async def aacquire_lease(
        self, key: str, lease_id: str, max_concurrent: int, ttl_us: int, now_us: int = 0
    ) -> LeaseResult:
        return self.acquire_lease(key, lease_id, max_concurrent, ttl_us, now_us)

    async def arenew_lease(self, key: str, lease_id: str, ttl_us: int, now_us: int = 0) -> bool:
        return self.renew_lease(key, lease_id, ttl_us, now_us)

    async def arelease_lease(self, key: str, lease_id: str) -> None:
        self.release_lease(key, lease_id)

    # -- introspection -----------------------------------------------------------------

    def size(self) -> int:
        """Entries currently held, including expired ones not yet swept."""
        with self._lock:
            return len(self._data)
