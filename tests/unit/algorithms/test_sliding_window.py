from unilimit.algorithms import Algorithm, State, sliding_window
from unilimit.algorithms._base import Evaluation

from .helpers import S, spec

SPEC = spec(Algorithm.SLIDING_WINDOW, 10, 60)
W = 60 * S
BASE = 1_000 * W  # start of an aligned window


def run(state: State | None, now: int, cost: int = 1) -> tuple[Evaluation, State | None]:
    e = sliding_window.evaluate(state, now, SPEC, cost)
    return e, e.new_state if e.allowed else state


def fill(now: int, count: int = 10) -> State | None:
    state: State | None = None
    for _ in range(count):
        e, state = run(state, now)
        assert e.allowed
    return state


def test_fills_current_window() -> None:
    state = None
    for i in range(10):
        e, state = run(state, BASE)
        assert e.remaining == 9 - i
    assert state == (1_000, 10, 0)
    e, _ = run(state, BASE + 30 * S)
    assert not e.allowed
    assert e.new_state is None


def test_previous_window_is_weighted() -> None:
    state = fill(BASE)
    # Start of next window: previous weight 1.0 -> estimate 10 -> rejected.
    e, _ = run(state, BASE + W)
    assert not e.allowed
    # 10% into the next window: estimate 9 -> exactly one request fits.
    e, state = run(state, BASE + W + 6 * S)
    assert e.allowed
    assert e.remaining == 0
    assert state == (1_001, 1, 10)
    e, _ = run(state, BASE + W + 6 * S)
    assert not e.allowed


def test_retry_after_is_exact_and_sufficient() -> None:
    state = fill(BASE)
    e, _ = run(state, BASE + 30 * S)
    # 30s until rotation, then previous=10 must decay to 9: 10% of the next window.
    assert e.retry_after_us == 36 * S
    assert run(state, BASE + 36 * S + 30 * S - 1)[0].allowed is False
    assert run(state, BASE + 66 * S)[0].allowed

    # Satisfiable within the current window once enough of `previous` decays.
    state = (1_001, 5, 10)
    e, _ = run(state, BASE + W)  # est = 15
    assert not e.allowed
    assert e.retry_after_us == 36 * S  # 10 * (1 - t/60) + 5 + 1 <= 10 -> t = 36s
    assert run(state, BASE + W + 36 * S)[0].allowed
    assert not run(state, BASE + W + 36 * S - 1)[0].allowed


def test_old_state_is_cleared_after_two_windows() -> None:
    state = fill(BASE)
    e, new = run(state, BASE + 2 * W)
    assert e.allowed
    assert new == (1_002, 1, 0)


def test_reset_after_and_ttl() -> None:
    e, state = run(None, BASE + 15 * S)
    assert e.ttl_us == 2 * W - 15 * S
    assert e.reset_after_us == 2 * W - 15 * S
    # In the next window with only `previous` set, the estimate reaches 0 at its end.
    p = sliding_window.peek(state, BASE + W + 10 * S, SPEC, 1)
    assert p.reset_after_us == W - 10 * S
    assert sliding_window.peek(None, BASE, SPEC, 1).reset_after_us == 0


def test_impossible_cost() -> None:
    e = sliding_window.evaluate(None, BASE, SPEC, 11)
    assert not e.allowed
    assert e.retry_after_us is None
    assert sliding_window.peek(None, BASE, SPEC, 11).retry_after_us is None


def test_clock_moving_backwards_is_stricter() -> None:
    state = (1_001, 4, 10)
    # Now is in window 1_000, before the stored window: previous gets full weight.
    e, _ = run(state, BASE + 59 * S)
    assert not e.allowed


def test_remaining_floors_fractional_estimate() -> None:
    state = fill(BASE, 5)
    p = sliding_window.peek(state, BASE + W + 30 * S, SPEC, 1)  # est = 2.5
    assert p.remaining == 7
