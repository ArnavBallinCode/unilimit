from __future__ import annotations

from collections.abc import Iterator

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from unilimit.algorithms import Algorithm
from unilimit.backends.base import Backend, ConsumeOp, OpResult
from unilimit.backends.memory import MemoryBackend
from unilimit.core.errors import BackendError
from unilimit.testing import ManualClock

from .drivers import BACKEND_KINDS, Driver, S, make_backend, op


async def test_consume_commits_when_allowed(driver: Driver) -> None:
    for remaining in (2, 1, 0):
        batch = await driver.consume([op("a")])
        assert batch.committed
        assert batch.results[0] == OpResult(True, remaining, 10 * S, 0)
    batch = await driver.consume([op("a")])
    assert not batch.committed
    assert batch.results[0] == OpResult(False, 0, 10 * S, 10 * S)


async def test_peek_never_mutates(driver: Driver) -> None:
    await driver.consume([op("a")])
    first = await driver.peek([op("a"), op("unknown")])
    for _ in range(5):
        assert await driver.peek([op("a"), op("unknown")]) == first
    assert first[0].remaining == 2
    assert first[1].remaining == 3
    batch = await driver.consume([op("a")])
    assert batch.results[0].remaining == 1


async def test_batch_is_all_or_nothing(driver: Driver) -> None:
    await driver.consume([op("full", limit=1)])
    before = await driver.peek([op("free"), op("full", limit=1)])
    batch = await driver.consume([op("free"), op("full", limit=1)])
    assert not batch.committed
    # The rule with room reports that it alone would allow, but nothing was consumed.
    assert batch.results[0].allowed
    assert batch.results[0].remaining == 3
    assert not batch.results[1].allowed
    assert await driver.peek([op("free"), op("full", limit=1)]) == before


async def test_batch_commits_every_op(driver: Driver) -> None:
    ops = [
        op("fw"),
        op("sw", algorithm=Algorithm.SLIDING_WINDOW),
        op("tb", algorithm=Algorithm.TOKEN_BUCKET),
    ]
    batch = await driver.consume(ops)
    assert batch.committed
    assert [r.remaining for r in batch.results] == [2, 2, 2]
    assert [r.remaining for r in await driver.peek(ops)] == [2, 2, 2]


async def test_shadow_ops_never_block_and_commit_only_when_allowed(driver: Driver) -> None:
    await driver.consume([op("shadow", limit=1)])
    batch = await driver.consume([op("real"), op("shadow", limit=1, enforce=False)])
    assert batch.committed
    assert not batch.results[1].allowed
    shadow_ok = op("shadow2", enforce=False)
    batch = await driver.consume([op("real"), shadow_ok])
    assert batch.committed
    assert batch.results[1].remaining == 2
    # A rejected batch commits nothing, including allowed shadow ops.
    await driver.consume([op("blocker", limit=1)])
    batch = await driver.consume([shadow_ok, op("blocker", limit=1)])
    assert not batch.committed
    assert (await driver.peek([shadow_ok]))[0].remaining == 2


async def test_only_shadow_ops_always_commit(driver: Driver) -> None:
    batch = await driver.consume([op("s", limit=1, enforce=False)])
    assert batch.committed
    batch = await driver.consume([op("s", limit=1, enforce=False)])
    assert batch.committed
    assert not batch.results[0].allowed


async def test_empty_batch(driver: Driver) -> None:
    batch = await driver.consume([])
    assert batch.committed
    assert batch.results == ()
    assert await driver.peek([]) == ()


async def test_duplicate_keys_are_rejected(driver: Driver) -> None:
    with pytest.raises(ValueError, match="duplicate key"):
        await driver.consume([op("a"), op("a", limit=5)])
    with pytest.raises(ValueError, match="duplicate key"):
        await driver.peek([op("a"), op("a")])
    assert (await driver.peek([op("a")]))[0].remaining == 3


async def test_state_from_another_algorithm_is_an_error(driver: Driver) -> None:
    await driver.consume([op("a")])
    with pytest.raises(BackendError, match="fixed_window"):
        await driver.consume([op("a", algorithm=Algorithm.TOKEN_BUCKET)])
    with pytest.raises(BackendError):
        await driver.peek([op("a", algorithm=Algorithm.SLIDING_WINDOW)])


async def test_reset(driver: Driver) -> None:
    await driver.consume([op("a", limit=1)])
    await driver.reset(["a", "never-existed"])
    assert (await driver.consume([op("a", limit=1)])).committed


async def test_expiry(driver: Driver) -> None:
    await driver.consume([op("a", limit=1)])
    driver.clock.advance(10 - 1e-6)
    assert not (await driver.consume([op("a", limit=1)])).committed
    driver.clock.advance(us=1)
    assert (await driver.consume([op("a", limit=1)])).committed


async def test_close_is_idempotent(driver: Driver) -> None:
    await driver.consume([op("a")])
    await driver.close()
    await driver.close()


def test_consume_op_validates_cost() -> None:
    for bad in (0, -1, True, 1.5):
        with pytest.raises(ValueError, match="cost"):
            op("a", cost=bad)  # type: ignore[arg-type]


# -- property: atomicity across arbitrary batches --------------------------------------

# As with real storage keys, each key is bound to one algorithm.
KEYS = {
    "k0": Algorithm.FIXED_WINDOW,
    "k1": Algorithm.SLIDING_WINDOW,
    "k2": Algorithm.TOKEN_BUCKET,
    "k3": Algorithm.SLIDING_WINDOW,
}


@st.composite
def batches(draw: st.DrawFn) -> list[ConsumeOp]:
    keys = draw(st.lists(st.sampled_from(sorted(KEYS)), min_size=1, max_size=4, unique=True))
    return [
        op(
            k,
            limit=draw(st.integers(1, 4)),
            algorithm=KEYS[k],
            cost=draw(st.integers(1, 3)),
            enforce=draw(st.booleans()),
        )
        for k in keys
    ]


@pytest.fixture(params=BACKEND_KINDS)
def atomic_backend(request: pytest.FixtureRequest) -> Iterator[tuple[Backend, ManualClock]]:
    clock = ManualClock()
    backend = make_backend(request.param, clock, request)
    yield backend, clock
    backend.close()


@settings(max_examples=150, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(steps=st.lists(st.tuples(batches(), st.integers(0, 4 * S)), max_size=25))
def test_batches_are_atomic(
    atomic_backend: tuple[Backend, ManualClock], steps: list[tuple[list[ConsumeOp], int]]
) -> None:
    """Random batches: commit iff every enforcing op allows; rejected batches change nothing."""
    backend, clock = atomic_backend
    backend.reset(sorted(KEYS))
    for ops, dt in steps:
        clock.advance(us=dt)
        before = backend.peek(ops)
        batch = backend.consume(ops)
        enforcing = [r.allowed for r, o in zip(batch.results, ops, strict=True) if o.enforce]
        assert batch.committed == all(enforcing)
        assert not batch.partial_commit  # impossible without concurrent writers
        if not batch.committed:
            assert batch.results == before
            assert backend.peek(ops) == before
        else:
            assert [r.allowed for r in batch.results] == [r.allowed for r in before]


# -- memory-specific behaviour ---------------------------------------------------------


def test_memory_max_keys_evicts_lru() -> None:
    clock = ManualClock()
    backend = MemoryBackend(clock=clock, max_keys=2)
    for key in ("a", "b"):
        backend.consume([op(key, limit=1)])
    backend.consume([op("c", limit=1)])  # evicts "a", the least recently used
    assert backend.size() == 2
    assert backend.live_evictions == 1
    assert backend.consume([op("a", limit=1)]).committed  # counter restarted
    assert not backend.consume([op("c", limit=1)]).committed


def test_memory_sweeps_expired_entries() -> None:
    clock = ManualClock()
    backend = MemoryBackend(clock=clock)
    for i in range(500):
        backend.consume([op(f"k{i}", limit=1)])
    clock.advance(11)
    for _ in range(2048):
        backend.consume([op("live", limit=10_000)])
    assert backend.size() < 500


def test_memory_validates_max_keys() -> None:
    for bad in (0, -1, True):
        with pytest.raises(ValueError, match="max_keys"):
            MemoryBackend(max_keys=bad)


def test_memory_default_clock_is_monotonic() -> None:
    backend = MemoryBackend()
    assert backend.consume([op("a")]).committed
