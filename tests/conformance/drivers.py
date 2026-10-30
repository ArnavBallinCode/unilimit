"""Uniform drivers over each backend's sync and async halves."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from unilimit.algorithms import Algorithm, LimitSpec
from unilimit.backends.base import Backend, BatchResult, ConsumeOp, OpResult
from unilimit.backends.memory import MemoryBackend
from unilimit.testing import ManualClock

S = 1_000_000


@dataclass
class Driver:
    """Uniform awaitable facade over the sync or async half of a backend."""

    backend: Backend
    clock: ManualClock
    mode: str

    async def consume(self, ops: Sequence[ConsumeOp]) -> BatchResult:
        if self.mode == "sync":
            return self.backend.consume(ops)
        return await self.backend.aconsume(ops)

    async def peek(self, ops: Sequence[ConsumeOp]) -> tuple[OpResult, ...]:
        if self.mode == "sync":
            return self.backend.peek(ops)
        return await self.backend.apeek(ops)

    async def reset(self, keys: Sequence[str]) -> None:
        if self.mode == "sync":
            self.backend.reset(keys)
        else:
            await self.backend.areset(keys)

    async def close(self) -> None:
        if self.mode == "sync":
            self.backend.close()
        else:
            await self.backend.aclose()


BackendFactory = Callable[[ManualClock], Backend]
BACKENDS: dict[str, BackendFactory] = {"memory": lambda clock: MemoryBackend(clock=clock)}


def op(
    key: str,
    limit: int = 3,
    window_s: int = 10,
    *,
    algorithm: Algorithm = Algorithm.FIXED_WINDOW,
    capacity: int | None = None,
    cost: int = 1,
    enforce: bool = True,
) -> ConsumeOp:
    spec = LimitSpec(algorithm, limit, window_s * S, capacity or limit)
    return ConsumeOp(key, spec, cost, enforce)
