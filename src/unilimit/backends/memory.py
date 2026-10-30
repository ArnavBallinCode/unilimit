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

    def __init__(self, *, clock: Clock | None = None, max_keys: int = 100_000) -> None:
        if isinstance(max_keys, bool) or not isinstance(max_keys, int) or max_keys < 1:
            raise ValueError("max_keys must be a positive integer")
        self._clock: Clock = clock if clock is not None else MonotonicClock()
        self._max_keys = max_keys
        self._data: OrderedDict[str, tuple[Algorithm, State, int]] = OrderedDict()
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

    def close(self) -> None:
        """No resources to release; stored state is kept."""

    # -- async protocol ----------------------------------------------------------------

    async def aconsume(self, ops: Sequence[ConsumeOp]) -> BatchResult:
        return self.consume(ops)

    async def apeek(self, ops: Sequence[ConsumeOp]) -> tuple[OpResult, ...]:
        return self.peek(ops)

    async def areset(self, keys: Sequence[str]) -> None:
        self.reset(keys)

    async def aclose(self) -> None:
        self.close()

    # -- introspection -----------------------------------------------------------------

    def size(self) -> int:
        """Entries currently held, including expired ones not yet swept."""
        with self._lock:
            return len(self._data)
