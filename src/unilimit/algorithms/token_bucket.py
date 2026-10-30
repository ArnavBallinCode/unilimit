"""Token bucket.

State: ``(tokens, last_us)``. The bucket holds at most ``capacity`` (``burst``) tokens
and refills continuously at ``limit / window`` tokens per microsecond.

This is mathematically equivalent to GCRA. It stores tokens, which are bounded by
``capacity``, rather than GCRA's absolute theoretical arrival time (~1.8e15 µs). An
absolute TAT in a double has 0.25 µs resolution, and the rounding accumulates when
fractional emission intervals are added to it. See ARCHITECTURE.md §7.3.
"""

from __future__ import annotations

import math

from unilimit.algorithms._base import EPS, Evaluation, LimitSpec, State


def _refill(state: State | None, now_us: int, spec: LimitSpec) -> tuple[float, int, float]:
    """Return ``(tokens, last_us, rate)`` refilled up to ``now_us``."""
    rate = float(spec.limit) / float(spec.window_us)
    capacity = float(spec.capacity)
    if state is None:
        return capacity, now_us, rate
    tokens, last = float(state[0]), int(state[1])
    if now_us <= last:
        # Clock did not advance (or moved backwards): no refill; keep `last` so the
        # same interval is never credited twice.
        return min(capacity, tokens), last, rate
    tokens = tokens + float(now_us - last) * rate
    # Snap to exactly full: after `reset_after` the bucket must equal a fresh one, so
    # expiring the key at its TTL never changes later decisions.
    if tokens >= capacity - EPS:
        tokens = capacity
    return tokens, now_us, rate


def _until(tokens_needed: float, rate: float) -> int:
    return max(0, math.ceil(tokens_needed / rate)) if tokens_needed > 0.0 else 0


def evaluate(state: State | None, now_us: int, spec: LimitSpec, cost: int) -> Evaluation:
    tokens, last, rate = _refill(state, now_us, spec)
    capacity = float(spec.capacity)
    if tokens + EPS >= float(cost):
        new_tokens = max(0.0, tokens - float(cost))
        reset_after = _until(capacity - new_tokens, rate)
        return Evaluation(
            allowed=True,
            remaining=math.floor(new_tokens + EPS),
            reset_after_us=reset_after,
            retry_after_us=0,
            new_state=(new_tokens, last),
            ttl_us=max(1, reset_after),
        )
    return Evaluation(
        allowed=False,
        remaining=math.floor(tokens + EPS),
        reset_after_us=_until(capacity - tokens, rate),
        retry_after_us=None if cost > spec.capacity else _until(float(cost) - tokens, rate),
    )


def peek(state: State | None, now_us: int, spec: LimitSpec, cost: int) -> Evaluation:
    tokens, _, rate = _refill(state, now_us, spec)
    allowed = tokens + EPS >= float(cost)
    if allowed:
        retry: int | None = 0
    elif cost > spec.capacity:
        retry = None
    else:
        retry = _until(float(cost) - tokens, rate)
    return Evaluation(
        allowed=allowed,
        remaining=math.floor(tokens + EPS),
        reset_after_us=_until(float(spec.capacity) - tokens, rate),
        retry_after_us=retry,
    )
