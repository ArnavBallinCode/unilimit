from __future__ import annotations

from typing import Any

import pytest

from unilimit.backends.memory import MemoryBackend
from unilimit.testing import (
    ManualClock,
    verify_async_backend_conformance,
    verify_async_lease_backend_conformance,
    verify_backend_conformance,
    verify_lease_backend_conformance,
)


def test_memory_backend_conforms_sync() -> None:
    clock = ManualClock()
    backend = MemoryBackend(clock=clock)
    verify_backend_conformance(backend)


async def test_memory_backend_conforms_async() -> None:
    clock = ManualClock()
    backend = MemoryBackend(clock=clock)
    await verify_async_backend_conformance(backend)


def test_memory_backend_conforms_lease_sync() -> None:
    clock = ManualClock()
    backend = MemoryBackend(clock=clock)
    verify_lease_backend_conformance(backend)


async def test_memory_backend_conforms_lease_async() -> None:
    clock = ManualClock()
    backend = MemoryBackend(clock=clock)
    await verify_async_lease_backend_conformance(backend)


class FlawedBackend:
    """A mock backend that violates all-or-nothing atomicity by partially committing."""

    def __init__(self) -> None:
        self.state: dict[str, int] = {}

    def consume(self, ops: list[Any]) -> object:
        from unilimit.backends.base import BatchResult, OpResult

        # Flawed: commits ops one-by-one without checking if batch passes
        results = []
        committed = True
        for op in ops:
            key = op.key
            spec = op.spec
            current = self.state.get(key, 0)
            if current < spec.limit:
                self.state[key] = current + 1
                results.append(OpResult(True, spec.limit - current - 1, 60_000_000, 0))
            else:
                committed = False
                results.append(OpResult(False, 0, 60_000_000, 60_000_000))
        return BatchResult(committed, tuple(results))

    def peek(self, ops: list[Any]) -> tuple[object, ...]:
        from unilimit.backends.base import OpResult

        results = []
        for op in ops:
            key = op.key
            spec = op.spec
            current = self.state.get(key, 0)
            results.append(
                OpResult(current < spec.limit, max(0, spec.limit - current), 60_000_000, 0)
            )
        return tuple(results)

    def reset(self, keys: list[str]) -> None:
        for k in keys:
            self.state.pop(k, None)


def test_conformance_harness_catches_flawed_backend() -> None:
    flawed = FlawedBackend()
    with pytest.raises(AssertionError):
        verify_backend_conformance(flawed)  # type: ignore[arg-type]
