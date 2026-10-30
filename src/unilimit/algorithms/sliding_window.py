"""Sliding window counter (the default algorithm).

State: ``(window_index, current, previous)``, with epoch-aligned windows
``index = now // W``. The usage estimate weights the previous window's count by the
fraction of it that still overlaps the sliding window::

    est = previous * ((W - elapsed) / W) + current

Memory per key is O(1). The estimate assumes requests in the previous window were
uniformly distributed.

Floating-point expressions are written with explicit ``float`` operands in a fixed
order so that the Lua implementation (IEEE-754 doubles) produces identical results.
"""

from __future__ import annotations

import math

from unilimit.algorithms._base import EPS, Evaluation, LimitSpec, State


def _normalise(state: State | None, now_us: int, window: int) -> tuple[int, int, int, int]:
    """Return ``(index, current, previous, elapsed)`` after window rotation."""
    index = now_us // window
    if state is None:
        return index, 0, 0, now_us - index * window
    stored, current, previous = int(state[0]), int(state[1]), int(state[2])
    if stored == index:
        return index, current, previous, now_us - index * window
    if stored == index - 1:
        return index, 0, current, now_us - index * window
    if stored < index - 1:
        return index, 0, 0, now_us - index * window
    # Clock moved backwards into an earlier window: keep the stored window and treat it
    # as just started, which gives the previous window full weight (stricter).
    return stored, current, previous, 0


def _estimate(current: int, previous: int, elapsed: int, window: int) -> float:
    weight = float(window - elapsed) / float(window)
    return float(previous) * weight + float(current)


def _reset_after(current: int, previous: int, elapsed: int, window: int) -> int:
    if current > 0:
        return 2 * window - elapsed
    if previous > 0:
        return window - elapsed
    return 0


def _retry_after(
    current: int, previous: int, elapsed: int, window: int, limit: int, cost: int
) -> int | None:
    if cost > limit:
        return None
    if current + cost <= limit:
        # Satisfiable within this window once enough of `previous` has decayed.
        if previous == 0:
            return 0
        t = float(window - elapsed) - float(limit - current - cost) / float(previous) * float(
            window
        )
        return max(0, math.ceil(t))
    # Needs the next window, where `current` becomes the decaying previous count.
    t_next = float(window) - float(limit - cost) / float(current) * float(window)
    return (window - elapsed) + max(0, math.ceil(t_next))


def evaluate(state: State | None, now_us: int, spec: LimitSpec, cost: int) -> Evaluation:
    window, limit = spec.window_us, spec.limit
    index, current, previous, elapsed = _normalise(state, now_us, window)
    est = _estimate(current, previous, elapsed, window)
    if est + float(cost) > float(limit) + EPS:
        return Evaluation(
            allowed=False,
            remaining=max(0, math.floor(float(limit) - est + EPS)),
            reset_after_us=_reset_after(current, previous, elapsed, window),
            retry_after_us=_retry_after(current, previous, elapsed, window, limit, cost),
        )
    new_current = current + cost
    ttl = 2 * window - elapsed
    return Evaluation(
        allowed=True,
        remaining=max(0, math.floor(float(limit) - est - float(cost) + EPS)),
        reset_after_us=ttl,
        retry_after_us=0,
        new_state=(index, new_current, previous),
        ttl_us=ttl,
    )


def peek(state: State | None, now_us: int, spec: LimitSpec, cost: int) -> Evaluation:
    window, limit = spec.window_us, spec.limit
    _, current, previous, elapsed = _normalise(state, now_us, window)
    est = _estimate(current, previous, elapsed, window)
    allowed = est + float(cost) <= float(limit) + EPS
    return Evaluation(
        allowed=allowed,
        remaining=max(0, math.floor(float(limit) - est + EPS)),
        reset_after_us=_reset_after(current, previous, elapsed, window),
        retry_after_us=0
        if allowed
        else _retry_after(current, previous, elapsed, window, limit, cost),
    )
