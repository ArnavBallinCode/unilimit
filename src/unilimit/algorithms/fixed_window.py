"""Fixed window, anchored per key.

State: ``(start_us, count)``. A window starts at the first request after the previous
window expired, so different identities do not all reset at the same instant.

Known property: up to ``2 × limit`` requests can pass across a window boundary.
"""

from __future__ import annotations

from unilimit.algorithms._base import Evaluation, LimitSpec, State


def _active(state: State | None, now_us: int, window_us: int) -> tuple[int, int] | None:
    if state is None:
        return None
    start, count = int(state[0]), int(state[1])
    # A clock that moved backwards (now < start) keeps the window active: stricter, never looser.
    if now_us < start + window_us:
        return start, count
    return None


def evaluate(state: State | None, now_us: int, spec: LimitSpec, cost: int) -> Evaluation:
    window = spec.window_us
    active = _active(state, now_us, window)
    start, count = active if active is not None else (now_us, 0)
    left = window - max(0, now_us - start)
    if count + cost > spec.limit:
        return Evaluation(
            allowed=False,
            remaining=spec.limit - count,
            reset_after_us=left if count > 0 else 0,
            retry_after_us=None if cost > spec.limit else left,
        )
    new_count = count + cost
    return Evaluation(
        allowed=True,
        remaining=spec.limit - new_count,
        reset_after_us=left,
        retry_after_us=0,
        new_state=(start, new_count),
        ttl_us=left,
    )


def peek(state: State | None, now_us: int, spec: LimitSpec, cost: int) -> Evaluation:
    active = _active(state, now_us, spec.window_us)
    if active is None:
        fits = cost <= spec.limit
        return Evaluation(fits, spec.limit, 0, 0 if fits else None)
    start, count = active
    left = spec.window_us - max(0, now_us - start)
    if count + cost <= spec.limit:
        return Evaluation(True, spec.limit - count, left, 0)
    return Evaluation(False, spec.limit - count, left, None if cost > spec.limit else left)
