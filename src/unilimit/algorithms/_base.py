"""Shared types for the pure algorithm implementations.

Algorithms are pure functions over immutable state tuples. They never perform I/O and
never read a clock: ``now_us`` is always passed in. The Python implementations are the
reference model that the Redis Lua script (Milestone 3) must reproduce exactly.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

State = tuple[int | float, ...]
"""Algorithm-specific immutable state. Each algorithm documents its own layout."""

EPS = 1e-9
"""Tolerance (in quota units) for floating-point comparisons.

Prevents values such as ``1.9999999999999998`` from being treated as below ``2``. It
can admit at most ``EPS`` of a request beyond the limit, which is never a whole request.
"""


class Algorithm(StrEnum):
    FIXED_WINDOW = "fixed_window"
    SLIDING_WINDOW = "sliding_window"
    TOKEN_BUCKET = "token_bucket"  # noqa: S105 - not a credential

    @property
    def code(self) -> str:
        """Short, stable code used in storage keys. Never change these."""
        return _CODES[self]


_CODES = {
    Algorithm.FIXED_WINDOW: "fw",
    Algorithm.SLIDING_WINDOW: "sw",
    Algorithm.TOKEN_BUCKET: "tb",
}


@dataclass(frozen=True, slots=True)
class LimitSpec:
    """Everything an algorithm needs to know about a limit.

    ``capacity`` is the maximum quota available at once: ``limit`` for window
    algorithms, ``burst`` (default ``limit``) for the token bucket.
    """

    algorithm: Algorithm
    limit: int
    window_us: int
    capacity: int


@dataclass(frozen=True, slots=True)
class Evaluation:
    """Outcome of evaluating one limit at one instant.

    For ``evaluate``: ``new_state`` and ``ttl_us`` are set only when ``allowed``;
    ``remaining``/``reset_after_us`` then describe the state *after* consumption.
    For ``peek`` and rejections: ``new_state`` is ``None`` and the figures describe the
    current, unmodified state.

    ``retry_after_us`` is ``0`` when allowed and ``None`` when the cost can never fit.
    """

    allowed: bool
    remaining: int
    reset_after_us: int
    retry_after_us: int | None
    new_state: State | None = None
    ttl_us: int = 0
