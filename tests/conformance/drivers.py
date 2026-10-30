"""Uniform drivers over each backend's sync and async halves."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import pytest

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


BACKEND_KINDS = ("memory", "redis", "cluster")


def make_backend(kind: str, clock: ManualClock, request: pytest.FixtureRequest) -> Backend:
    """Build a backend of ``kind`` driven by ``clock``; Redis kinds use real servers."""
    if kind == "memory":
        return MemoryBackend(clock=clock)
    from unilimit.backends.redis import RedisBackend

    if kind == "redis":
        return RedisBackend(request.getfixturevalue("redis_url"), clock=clock)
    return RedisBackend(request.getfixturevalue("cluster_url"), cluster=True, clock=clock)


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
