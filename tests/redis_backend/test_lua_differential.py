"""Lua vs Python differential tests.

The Python algorithms and ``evaluate_batch``/``peek_batch`` are the reference model.
For arbitrary prior states, clocks and batches, the Lua script must produce identical
results (decisions, remaining, reset, retry) and an identical stored state string, set
the matching TTL, and leave every key it does not commit byte-for-byte unchanged.
"""

from __future__ import annotations

import math
import os
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest
import redis
import redis.cluster
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tests._servers import close_client
from unilimit.algorithms import Algorithm, LimitSpec, State
from unilimit.algorithms._base import MAX_REFILL_US
from unilimit.backends.base import BatchResult, ConsumeOp, evaluate_batch, peek_batch
from unilimit.backends.redis import RedisBackend, decode_state, encode_state
from unilimit.backends.redis._protocol import MODE_CONSUME, MODE_PEEK, ScriptReply, plan_groups
from unilimit.core.rate import MAX_WINDOW_US, MIN_WINDOW_US
from unilimit.testing import ManualClock

MAX_NOW = 2**52  # far beyond any realistic epoch in µs (~1.8e15 in 2026)


@dataclass
class Harness:
    backend: RedisBackend
    client: Any
    clock: ManualClock
    tag: str

    def key(self, i: int) -> str:
        # A shared hash tag keeps every key of a batch in one slot on Cluster too.
        return f"t:{{{self.tag}}}:{i}"

    def load(self, ops: list[ConsumeOp], states: list[State | None]) -> None:
        for op, state in zip(ops, states, strict=True):
            if state is None:
                self.client.delete(op.key)
            else:
                self.client.set(op.key, encode_state(op.spec.algorithm, state))

    def raw(self, key: str) -> bytes | None:
        value: bytes | None = self.client.get(key)
        return value


@pytest.fixture(params=["redis", "cluster"])
def harness(request: pytest.FixtureRequest) -> Iterator[Harness]:
    clock = ManualClock()
    if request.param == "redis":
        url = request.getfixturevalue("redis_url")
        client: Any = redis.Redis.from_url(url)
        backend = RedisBackend(url, clock=clock)
    else:
        url = request.getfixturevalue("cluster_url")
        client = redis.cluster.RedisCluster.from_url(url)
        backend = RedisBackend(url, cluster=True, clock=clock)
    yield Harness(backend, client, clock, request.param)
    backend.close()
    close_client(client)


# -- strategies --------------------------------------------------------------------------


@st.composite
def specs(draw: st.DrawFn) -> LimitSpec:
    algorithm = draw(st.sampled_from(list(Algorithm)))
    window = draw(
        st.one_of(
            st.sampled_from([MIN_WINDOW_US, 1_000_000, 60_000_000, 86_400_000_000, MAX_WINDOW_US]),
            st.integers(MIN_WINDOW_US, MAX_WINDOW_US),
        )
    )
    limit = draw(
        st.one_of(
            st.integers(1, 100),
            st.integers(1, min(window, 10**9)),
            st.just(window),  # one event per microsecond: the finest representable rate
        )
    )
    capacity = limit
    if algorithm is Algorithm.TOKEN_BUCKET:
        # Largest burst whose full refill takes at most MAX_REFILL_US (LimitSpec invariant).
        largest = max(1, MAX_REFILL_US * limit // window)
        capacity = draw(
            st.one_of(
                st.just(min(limit, largest)),
                st.integers(1, min(100, largest)),
                st.integers(1, min(10**9, largest)),
                st.just(min(10**9, largest)),
            )
        )
    return LimitSpec(algorithm, limit, window, capacity)


@st.composite
def nows(draw: st.DrawFn, window: int) -> int:
    base = draw(
        st.one_of(
            st.integers(1_600_000_000_000_000, 4_000_000_000_000_000), st.integers(0, MAX_NOW)
        )
    )
    # Bias towards window boundaries, where index arithmetic is most fragile.
    boundary = (base // window) * window
    return draw(st.sampled_from([base, boundary, boundary + 1, max(0, boundary - 1)]))


@st.composite
def states(draw: st.DrawFn, spec: LimitSpec, now: int) -> State | None:
    if draw(st.booleans()):
        return None
    w = spec.window_us
    small = st.integers(0, spec.limit + 2) | st.integers(0, 3)
    if spec.algorithm is Algorithm.FIXED_WINDOW:
        start = max(0, now - draw(st.integers(-w, 2 * w)))
        return (start, draw(small))
    if spec.algorithm is Algorithm.SLIDING_WINDOW:
        index = max(0, now // w + draw(st.integers(-3, 1)))
        return (index, draw(small), draw(small))
    cap = float(spec.capacity)
    tokens = draw(
        st.one_of(
            st.floats(0, cap * 1.5, allow_nan=False, allow_infinity=False),
            st.sampled_from([0.0, cap, cap - 1e-12, math.nextafter(cap, 0), 1.0]).filter(
                lambda t: t >= 0
            ),
            st.integers(0, spec.capacity).map(float),
        )
    )
    last = max(0, now - draw(st.integers(-w, 3 * w)))
    return (tokens, last)


@dataclass(frozen=True)
class Case:
    now: int
    ops: list[ConsumeOp]
    states: list[State | None]


@st.composite
def cases(draw: st.DrawFn, size: int = 4) -> Case:
    n = draw(st.integers(1, size))
    spec0 = draw(specs())
    now = draw(nows(spec0.window_us))
    ops, prior = [], []
    for i in range(n):
        spec = spec0 if i == 0 else draw(specs())
        cost = draw(st.integers(1, min(spec.capacity + 1, 10**9)) | st.integers(1, 3))
        enforce = draw(st.booleans()) if n > 1 else True
        ops.append(ConsumeOp(f"__{i}", spec, cost, enforce))
        prior.append(draw(states(spec, now)))
    return Case(now, ops, prior)


def bind(h: Harness, case: Case) -> list[ConsumeOp]:
    return [ConsumeOp(h.key(i), op.spec, op.cost, op.enforce) for i, op in enumerate(case.ops)]


# -- the differential checks -------------------------------------------------------------


def run_script(h: Harness, ops: list[ConsumeOp], mode: str, now: int) -> ScriptReply:
    """One script call for the whole batch (keys share a hash tag, so also on Cluster)."""
    client, script = h.backend._sync()
    groups = plan_groups(ops, h.backend.cluster)
    assert len(groups) == 1
    return h.backend._call(client, script, ops, groups[0], mode, now)


def check_consume(h: Harness, case: Case) -> None:
    ops = bind(h, case)
    h.load(ops, case.states)
    before = [h.raw(op.key) for op in ops]
    expected, writes = evaluate_batch(ops, case.states, case.now)

    reply = run_script(h, ops, MODE_CONSUME, case.now)

    assert BatchResult(reply.committed, reply.results) == expected
    assert reply.now_us == case.now
    written = {w.key: w for w in writes}
    for i, (op, raw_before) in enumerate(zip(ops, before, strict=True)):
        raw = h.raw(op.key)
        if op.key in written:
            w = written[op.key]
            # Exact: the state string and TTL the script computed and wrote.
            assert reply.written[i] == encode_state(op.spec.algorithm, w.state)
            assert reply.ttls_us[i] == w.ttl_us
            expected_ms = math.ceil(w.ttl_us / 1000) + 1
            if raw is None:
                # Only keys with millisecond-scale TTLs can expire (in real time) before
                # this read; everything else must still be present.
                assert expected_ms <= 1_000
            else:
                assert raw.decode() == reply.written[i]
                assert decode_state(raw) == (op.spec.algorithm, w.state)
                pttl = h.client.pttl(op.key)
                assert expected_ms - 1_000 < pttl <= expected_ms
        else:
            assert reply.written[i] is None
            assert reply.ttls_us[i] == 0
            assert raw == raw_before
            if raw is not None:
                assert h.client.pttl(op.key) == -1  # untouched: still no TTL


def check_peek(h: Harness, case: Case) -> None:
    ops = bind(h, case)
    h.load(ops, case.states)
    before = [h.raw(op.key) for op in ops]
    reply = run_script(h, ops, MODE_PEEK, case.now)
    assert reply.results == peek_batch(ops, case.states, case.now)
    assert not reply.committed
    assert reply.written == (None,) * len(ops)
    assert [h.raw(op.key) for op in ops] == before
    h.clock.set(case.now)
    assert h.backend.peek(ops) == reply.results  # public path agrees


# UNILIMIT_DIFF_EXAMPLES scales every differential test (e.g. 5000 for a deep run).
EXAMPLES = int(os.environ.get("UNILIMIT_DIFF_EXAMPLES", "400"))
SETTINGS = settings(
    max_examples=EXAMPLES, suppress_health_check=[HealthCheck.function_scoped_fixture]
)


@SETTINGS
@given(case=cases(size=1))
def test_single_op_consume_matches_reference(harness: Harness, case: Case) -> None:
    check_consume(harness, case)


@SETTINGS
@given(case=cases())
def test_batch_consume_matches_reference(harness: Harness, case: Case) -> None:
    check_consume(harness, case)


@SETTINGS
@given(case=cases())
def test_peek_matches_reference(harness: Harness, case: Case) -> None:
    check_peek(harness, case)


@st.composite
def sequences(draw: st.DrawFn) -> tuple[LimitSpec, int, list[tuple[int, int]]]:
    spec = draw(specs())
    start = draw(nows(spec.window_us))
    step = st.tuples(
        st.one_of(
            st.just(0), st.integers(0, spec.window_us // 3 + 1), st.integers(0, 3 * spec.window_us)
        ),
        st.integers(1, min(spec.capacity + 1, 10**9)) | st.integers(1, 3),
    )
    return spec, start, draw(st.lists(step, min_size=1, max_size=40))


@settings(
    max_examples=max(1, EXAMPLES * 3 // 8),
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(seq=sequences())
def test_sequences_match_reference(
    harness: Harness, seq: tuple[LimitSpec, int, list[tuple[int, int]]]
) -> None:
    """Reachable states: drive identical request sequences through both implementations."""
    spec, now, steps = seq
    state: State | None = None
    for dt, cost in steps:
        now += dt
        check_consume(harness, Case(now, [ConsumeOp("__0", spec, cost)], [state]))
        _, writes = evaluate_batch([ConsumeOp("k", spec, cost)], [state], now)
        if writes:
            state = writes[0].state


# -- hand-picked numerical boundaries ----------------------------------------------------

W_DAY = 86_400_000_000
BOUNDARY_CASES = [
    # finest representable rate: one event per µs
    (
        LimitSpec(Algorithm.TOKEN_BUCKET, 1_000, 1_000, 1_000),
        (0.0, 1_800_000_000_000_000),
        1_800_000_000_000_001,
        1,
    ),
    (
        LimitSpec(Algorithm.SLIDING_WINDOW, 1_000, 1_000, 1_000),
        (1_800_000_000_000, 999, 1_000),
        1_800_000_000_000_999,
        1,
    ),
    # very large limits and the longest window
    (
        LimitSpec(Algorithm.SLIDING_WINDOW, 10**9, MAX_WINDOW_US, 10**9),
        (4_000_000_000_000_000 // MAX_WINDOW_US - 1, 0, 999_999_999),
        4_000_000_000_000_000,
        7,
    ),
    (
        LimitSpec(Algorithm.FIXED_WINDOW, 10**9, MAX_WINDOW_US, 10**9),
        (3_999_000_000_000_000, 10**9 - 1),
        4_000_000_000_000_000,
        1,
    ),
    (
        LimitSpec(Algorithm.TOKEN_BUCKET, 10**9, W_DAY, 10**9),
        (0.5, 1_800_000_000_000_000),
        1_800_000_000_000_086,
        1,
    ),
    # fractional emission interval and the snap-to-capacity edge
    (
        LimitSpec(Algorithm.TOKEN_BUCKET, 21, 42_357, 3),
        (2.9999999999999996, 1_000_000_000_000),
        1_000_000_000_001,
        1,
    ),
    (
        LimitSpec(Algorithm.TOKEN_BUCKET, 7, 60_000_000, 7),
        (0.0, 1_000_000_000_000),
        1_000_008_571_429,
        1,
    ),
    # exact window boundaries and the last microsecond before them
    (
        LimitSpec(Algorithm.SLIDING_WINDOW, 10, 60_000_000, 10),
        (29_999, 10, 0),
        30_000 * 60_000_000,
        1,
    ),
    (
        LimitSpec(Algorithm.SLIDING_WINDOW, 10, 60_000_000, 10),
        (29_999, 10, 0),
        30_000 * 60_000_000 - 1,
        1,
    ),
    (
        LimitSpec(Algorithm.FIXED_WINDOW, 3, 10_000_000, 3),
        (1_000_000_000_000, 3),
        1_000_010_000_000,
        1,
    ),
    (
        LimitSpec(Algorithm.FIXED_WINDOW, 3, 10_000_000, 3),
        (1_000_000_000_000, 3),
        1_000_009_999_999,
        1,
    ),
    # near 2^52 and at time zero
    (LimitSpec(Algorithm.SLIDING_WINDOW, 5, 1_000, 5), (MAX_NOW // 1_000, 2, 5), MAX_NOW + 999, 3),
    (LimitSpec(Algorithm.TOKEN_BUCKET, 5, 1_000, 5), None, 0, 5),
    # clock moved backwards
    (
        LimitSpec(Algorithm.SLIDING_WINDOW, 10, 60_000_000, 10),
        (30_001, 4, 10),
        30_000 * 60_000_000 + 59_000_000,
        1,
    ),
    (
        LimitSpec(Algorithm.TOKEN_BUCKET, 10, 10_000_000, 10),
        (3.25, 2_000_000_000_000),
        1_999_999_000_000,
        1,
    ),
    # the longest legal full-refill time (10 years): largest reset/TTL values
    (
        LimitSpec(Algorithm.TOKEN_BUCKET, 1, 86_400_000_000, 3_660),
        (0.0, 1_800_000_000_000_000),
        1_800_000_000_000_000,
        1,
    ),
    (LimitSpec(Algorithm.TOKEN_BUCKET, 1, 86_400_000_000, 3_660), None, 1_800_000_000_000_000, 1),
    # cost larger than capacity (can never succeed)
    (
        LimitSpec(Algorithm.TOKEN_BUCKET, 10, 10_000_000, 4),
        (4.0, 1_000_000_000_000),
        1_000_000_000_000,
        5,
    ),
]


@pytest.mark.parametrize(("spec", "state", "now", "cost"), BOUNDARY_CASES)
def test_numerical_boundaries(
    harness: Harness, spec: LimitSpec, state: State | None, now: int, cost: int
) -> None:
    case = Case(now, [ConsumeOp("__0", spec, cost)], [state])
    check_peek(harness, case)
    check_consume(harness, case)


def _eps_boundary_cases() -> list[tuple[LimitSpec, State, int, int]]:
    """Sliding-window states whose estimate lies exactly EPS from an integer.

    Here a 1-ulp difference flips admit/reject or ``remaining``. With W a multiple of
    1e9 µs and ``previous * (W - elapsed) = W*m ± k``, the estimate is ``m ± 1e-9``.
    These inputs fail if Lua evaluates ``previous * (W - e) / W`` in a different order
    than Python, which random sampling essentially never hits.
    """
    out: list[tuple[LimitSpec, State, int, int]] = []
    for k in (1, 2, 3):
        window = 10**9 * k
        for previous in range(2, 60):
            for m in range(previous):
                for sign in (1, -1):
                    num = window * m + sign * k
                    if num <= 0 or num % previous or not 0 < num // previous <= window:
                        continue
                    elapsed = window - num // previous
                    index = 1_800_000_000_000_000 // window
                    for limit in (max(1, m), m + 1, m + 2):
                        for cost in (1, 2):
                            if cost <= limit:
                                spec = LimitSpec(Algorithm.SLIDING_WINDOW, limit, window, limit)
                                out.append(
                                    (spec, (index, 0, previous), index * window + elapsed, cost)
                                )
    return out


EPS_BOUNDARY_CASES = _eps_boundary_cases()


def test_eps_boundary_family_is_substantial() -> None:
    assert len(EPS_BOUNDARY_CASES) > 200


@pytest.mark.parametrize("index", range(0, len(EPS_BOUNDARY_CASES), 5))
def test_eps_boundaries(harness: Harness, index: int) -> None:
    spec, state, now, cost = EPS_BOUNDARY_CASES[index]
    case = Case(now, [ConsumeOp("__0", spec, cost)], [state])
    check_peek(harness, case)
    check_consume(harness, case)


def test_server_time_is_used_by_default(redis_url: str) -> None:
    import time

    backend = RedisBackend(redis_url)
    spec = LimitSpec(Algorithm.FIXED_WINDOW, 5, 60_000_000, 5)
    before = time.time_ns() // 1000
    backend.consume([ConsumeOp("server-time", spec)])
    after = time.time_ns() // 1000
    reader = redis.Redis.from_url(redis_url)
    raw = reader.get("server-time")
    reader.close()
    assert isinstance(raw, bytes)
    _, (start, count) = decode_state(raw)
    assert count == 1
    assert before - 1_000_000 <= start <= after + 1_000_000  # same host: clocks agree
    backend.close()


@settings(max_examples=200, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(spec=specs(), cost=st.integers(1, 3))
def test_keys_never_expire_before_the_model_ttl(redis_url: str, spec: LimitSpec, cost: int) -> None:
    """With the server clock, absolute expiry must be at or after ``now + ttl``."""
    client = redis.Redis.from_url(redis_url)
    if int(client.info("server")["redis_version"].split(".")[0]) < 7:
        pytest.skip("PEXPIRETIME requires Redis 7")
    backend = RedisBackend(redis_url)
    op = ConsumeOp("expiry", spec, min(cost, spec.capacity))
    client.delete(op.key)
    client_, script = backend._sync()
    reply = backend._call(client_, script, [op], plan_groups([op], False)[0], MODE_CONSUME, None)
    assert reply.committed
    expire_at_ms = client.pexpiretime(op.key)
    assert expire_at_ms * 1000 >= reply.now_us + reply.ttls_us[0]
    backend.close()
    client.close()
