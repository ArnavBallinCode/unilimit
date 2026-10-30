from __future__ import annotations

import time
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """Source of time for backends that keep time locally.

    Must return integer microseconds from a clock that never goes backwards in normal
    operation. The absolute origin is irrelevant; only differences are used.
    """

    def now_us(self) -> int: ...


class MonotonicClock:
    """Default clock: ``time.monotonic_ns()`` in microseconds."""

    __slots__ = ()

    def now_us(self) -> int:
        return time.monotonic_ns() // 1000
