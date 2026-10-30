"""Public helpers for testing code that uses UniLimit."""

from __future__ import annotations

import threading

from unilimit.core.rate import US_PER_SECOND


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


__all__ = ["ManualClock"]
