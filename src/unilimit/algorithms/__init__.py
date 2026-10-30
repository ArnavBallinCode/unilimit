"""Pure rate-limiting algorithms (the reference model for every backend)."""

from __future__ import annotations

from collections.abc import Callable
from typing import NamedTuple

from unilimit.algorithms import fixed_window, sliding_window, token_bucket
from unilimit.algorithms._base import EPS, Algorithm, Evaluation, LimitSpec, State

AlgorithmFn = Callable[[State | None, int, LimitSpec, int], Evaluation]


class AlgorithmImpl(NamedTuple):
    evaluate: AlgorithmFn
    peek: AlgorithmFn


_IMPLS: dict[Algorithm, AlgorithmImpl] = {
    Algorithm.FIXED_WINDOW: AlgorithmImpl(fixed_window.evaluate, fixed_window.peek),
    Algorithm.SLIDING_WINDOW: AlgorithmImpl(sliding_window.evaluate, sliding_window.peek),
    Algorithm.TOKEN_BUCKET: AlgorithmImpl(token_bucket.evaluate, token_bucket.peek),
}


def get_impl(algorithm: Algorithm) -> AlgorithmImpl:
    return _IMPLS[algorithm]


__all__ = ["EPS", "Algorithm", "AlgorithmImpl", "Evaluation", "LimitSpec", "State", "get_impl"]
