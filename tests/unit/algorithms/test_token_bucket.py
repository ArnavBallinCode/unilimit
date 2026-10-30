from unilimit.algorithms import Algorithm, State, token_bucket
from unilimit.algorithms._base import Evaluation, LimitSpec

from .helpers import T0, S, spec

SPEC = spec(Algorithm.TOKEN_BUCKET, 10, 10)  # 1 token/s, capacity 10


def run(
    state: State | None, now: int, cost: int = 1, s: LimitSpec = SPEC
) -> tuple[Evaluation, State | None]:
    e = token_bucket.evaluate(state, now, s, cost)
    return e, e.new_state if e.allowed else state


def test_burst_then_steady_refill() -> None:
    state = None
    for i in range(10):
        e, state = run(state, T0)
        assert e.allowed
        assert e.remaining == 9 - i
    e, _ = run(state, T0)
    assert not e.allowed
    assert e.retry_after_us == S
    e, _ = run(state, T0 + S // 2)
    assert e.retry_after_us == S // 2
    e, state = run(state, T0 + S)
    assert e.allowed
    assert e.remaining == 0


def test_burst_larger_than_rate() -> None:
    s = spec(Algorithm.TOKEN_BUCKET, 10, 1, capacity=20)
    state = None
    for _ in range(20):
        e, state = run(state, T0, s=s)
        assert e.allowed
    assert not run(state, T0, s=s)[0].allowed
    e, _ = run(state, T0 + S // 10, s=s)
    assert e.allowed


def test_reset_after_is_time_to_full() -> None:
    e, state = run(None, T0, cost=4)
    assert e.reset_after_us == 4 * S
    assert e.ttl_us == 4 * S
    p = token_bucket.peek(state, T0 + 4 * S, SPEC, 1)
    assert p.remaining == 10
    assert p.reset_after_us == 0


def test_refill_never_exceeds_capacity() -> None:
    _, state = run(None, T0)
    p = token_bucket.peek(state, T0 + 10_000 * S, SPEC, 1)
    assert p.remaining == 10


def test_impossible_cost() -> None:
    e = token_bucket.evaluate(None, T0, SPEC, 11)
    assert not e.allowed
    assert e.retry_after_us is None
    assert token_bucket.peek(None, T0, SPEC, 11).retry_after_us is None


def test_fractional_rates_have_no_off_by_one() -> None:
    # 7 per minute: the emission interval is not an integer number of µs.
    s = spec(Algorithm.TOKEN_BUCKET, 7, 60)
    state = None
    for i in range(7):
        e, state = run(state, T0, s=s)
        assert e.remaining == 6 - i
    e, _ = run(state, T0, s=s)
    assert e.retry_after_us is not None
    p = token_bucket.peek(state, T0 + e.retry_after_us, s, 1)
    assert p.allowed
    assert p.remaining == 1
    full = token_bucket.peek(state, T0 + e.reset_after_us, s, 1)
    assert full.remaining == 7


def test_clock_not_advancing_does_not_refill_twice() -> None:
    _, state = run(None, T0, cost=10)
    assert state is not None
    later = T0 + 5 * S
    _, state2 = run(state, later)
    # A stale (earlier) timestamp must not refill again and keeps `last`.
    e = token_bucket.peek(state2, T0 + S, SPEC, 1)
    assert e.remaining == 4
    assert state2 is not None
    assert state2[1] == later


def test_refilled_bucket_is_exactly_full() -> None:
    # Regression (found by deep fuzzing): refilling for exactly `reset_after` produced
    # 2.9999999999999996 tokens instead of 3.0, so an expired key and a kept key
    # diverged in stored state.
    s = LimitSpec(Algorithm.TOKEN_BUCKET, 21, 42_357, 3)
    e, state = run(None, T0, cost=3, s=s)
    later = T0 + e.ttl_us
    assert token_bucket.evaluate(state, later, s, 1) == token_bucket.evaluate(None, later, s, 1)
