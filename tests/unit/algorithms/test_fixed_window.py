from unilimit.algorithms import Algorithm, State, fixed_window
from unilimit.algorithms._base import Evaluation

from .helpers import T0, S, spec

SPEC = spec(Algorithm.FIXED_WINDOW, 3, 10)


def run(state: State | None, now: int, cost: int = 1) -> tuple[Evaluation, State | None]:
    e = fixed_window.evaluate(state, now, SPEC, cost)
    return e, e.new_state if e.allowed else state


def test_admits_up_to_limit_then_rejects_until_window_end() -> None:
    state = None
    for expected_remaining in (2, 1, 0):
        e, state = run(state, T0)
        assert e.allowed
        assert e.remaining == expected_remaining
        assert e.reset_after_us == 10 * S
        assert e.ttl_us == 10 * S
    e, state2 = run(state, T0 + 4 * S)
    assert not e.allowed
    assert e.new_state is None
    assert e.retry_after_us == 6 * S
    assert e.reset_after_us == 6 * S
    assert e.remaining == 0
    assert state2 == state


def test_window_is_anchored_to_first_request() -> None:
    e, state = run(None, T0 + 7)
    assert state == (T0 + 7, 1)
    _, state = run(state, T0 + 7 + 10 * S - 1)
    assert state == (T0 + 7, 2)
    e, state = run(state, T0 + 7 + 10 * S)  # window expired: new anchor
    assert state == (T0 + 7 + 10 * S, 1)
    assert e.remaining == 2


def test_cost_and_impossible_cost() -> None:
    e, state = run(None, T0, cost=3)
    assert e.allowed
    assert e.remaining == 0
    e = fixed_window.evaluate(None, T0, SPEC, 4)
    assert not e.allowed
    assert e.retry_after_us is None
    assert e.reset_after_us == 0
    e = fixed_window.evaluate(state, T0, SPEC, 4)
    assert e.retry_after_us is None


def test_partial_cost_rejected_when_it_does_not_fit() -> None:
    _, state = run(None, T0, cost=2)
    e, _ = run(state, T0 + S, cost=2)
    assert not e.allowed
    assert e.remaining == 1
    assert e.retry_after_us == 9 * S


def test_peek() -> None:
    p = fixed_window.peek(None, T0, SPEC, 1)
    assert (p.allowed, p.remaining, p.reset_after_us, p.retry_after_us) == (True, 3, 0, 0)
    assert p.new_state is None
    _, state = run(None, T0, cost=3)
    p = fixed_window.peek(state, T0 + S, SPEC, 1)
    assert (p.allowed, p.remaining, p.reset_after_us, p.retry_after_us) == (False, 0, 9 * S, 9 * S)
    assert fixed_window.peek(None, T0, SPEC, 4).retry_after_us is None
    assert fixed_window.peek(state, T0, SPEC, 4).retry_after_us is None


def test_clock_moving_backwards_keeps_window_active() -> None:
    _, state = run(None, T0, cost=3)
    e, _ = run(state, T0 - 5 * S)
    assert not e.allowed
    assert e.retry_after_us == 10 * S
