"""Backend protocols and the reference batch semantics.

:func:`evaluate_batch` defines the atomic evaluate-then-commit contract of
``consume`` (ARCHITECTURE.md §6.2). Every backend must behave exactly as if it read the
state of every key, called this function, and applied the returned writes in a single
atomic step.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from unilimit.algorithms import Evaluation, LimitSpec, State, get_impl


@dataclass(frozen=True, slots=True)
class ConsumeOp:
    """One limit to evaluate for one storage key.

    ``enforce=False`` marks a shadow op: it is evaluated and reported, never blocks the
    batch, and is only committed when it allows and the batch commits.
    """

    key: str
    spec: LimitSpec
    cost: int = 1
    enforce: bool = True

    def __post_init__(self) -> None:
        if isinstance(self.cost, bool) or not isinstance(self.cost, int) or self.cost < 1:
            raise ValueError(f"cost must be a positive integer, got {self.cost!r}")


@dataclass(frozen=True, slots=True)
class OpResult:
    """Per-op outcome. See :class:`unilimit.algorithms.Evaluation` for field meanings."""

    allowed: bool
    remaining: int
    reset_after_us: int
    retry_after_us: int | None


@dataclass(frozen=True, slots=True)
class BatchResult:
    """``committed`` is True iff every enforcing op allowed and the writes were applied.

    ``partial_commit`` is True only when the batch was *not* committed but some of its
    ops are known to have been consumed. This can only happen for Redis Cluster batches
    whose keys span several hash slots (ARCHITECTURE.md §6.2).
    """

    committed: bool
    results: tuple[OpResult, ...]
    partial_commit: bool = False


@dataclass(frozen=True, slots=True)
class Write:
    key: str
    state: State
    ttl_us: int


def _to_result(evaluation: Evaluation) -> OpResult:
    return OpResult(
        evaluation.allowed,
        evaluation.remaining,
        evaluation.reset_after_us,
        evaluation.retry_after_us,
    )


def check_unique_keys(ops: Sequence[ConsumeOp]) -> None:
    if len(ops) <= 1:
        return
    seen: set[str] = set()
    for op in ops:
        if op.key in seen:
            raise ValueError(f"duplicate key in batch: {op.key!r}")
        seen.add(op.key)


def evaluate_batch(
    ops: Sequence[ConsumeOp], states: Sequence[State | None], now_us: int
) -> tuple[BatchResult, tuple[Write, ...]]:
    """Reference implementation of atomic, all-or-nothing batch consumption.

    1. Every op is evaluated against the current state.
    2. If every enforcing op allows, the batch commits: each allowed op's new state is
       written (rejecting shadow ops write nothing).
    3. Otherwise nothing is written and results use peek semantics (current state).
    """
    if len(ops) != len(states):
        raise ValueError("ops and states must have the same length")
    check_unique_keys(ops)
    evaluations = [
        get_impl(op.spec.algorithm).evaluate(state, now_us, op.spec, op.cost)
        for op, state in zip(ops, states, strict=True)
    ]
    committed = all(e.allowed for e, op in zip(evaluations, ops, strict=True) if op.enforce)
    if committed:
        writes = tuple(
            Write(op.key, e.new_state, e.ttl_us)
            for op, e in zip(ops, evaluations, strict=True)
            if e.allowed and e.new_state is not None
        )
        return BatchResult(True, tuple(_to_result(e) for e in evaluations)), writes
    return BatchResult(False, peek_batch(ops, states, now_us)), ()


def peek_batch(
    ops: Sequence[ConsumeOp], states: Sequence[State | None], now_us: int
) -> tuple[OpResult, ...]:
    """Reference implementation of ``peek``: evaluates without producing any writes."""
    if len(ops) != len(states):
        raise ValueError("ops and states must have the same length")
    check_unique_keys(ops)
    return tuple(
        _to_result(get_impl(op.spec.algorithm).peek(state, now_us, op.spec, op.cost))
        for op, state in zip(ops, states, strict=True)
    )


@runtime_checkable
class SyncBackend(Protocol):
    def consume(self, ops: Sequence[ConsumeOp]) -> BatchResult:
        """Atomically evaluate all ops and commit only if every enforcing op allows."""
        ...

    def peek(self, ops: Sequence[ConsumeOp]) -> tuple[OpResult, ...]:
        """Evaluate without mutating any state."""
        ...

    def reset(self, keys: Sequence[str]) -> None: ...

    def close(self) -> None: ...


@runtime_checkable
class AsyncBackend(Protocol):
    async def aconsume(self, ops: Sequence[ConsumeOp]) -> BatchResult: ...

    async def apeek(self, ops: Sequence[ConsumeOp]) -> tuple[OpResult, ...]: ...

    async def areset(self, keys: Sequence[str]) -> None: ...

    async def aclose(self) -> None: ...


@runtime_checkable
class Backend(SyncBackend, AsyncBackend, Protocol):
    """A backend usable from both sync and async code (required by :class:`Limiter`)."""
