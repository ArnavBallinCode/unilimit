"""Property-based invariants that every algorithm must satisfy on arbitrary schedules."""

from __future__ import annotations

from dataclasses import dataclass

from hypothesis import given
from hypothesis import strategies as st

from unilimit.algorithms import EPS, Algorithm, LimitSpec, State, get_impl

T0 = 1_000_000_000_000


@st.composite
def specs(draw: st.DrawFn, algorithm: Algorithm | None = None) -> LimitSpec:
    algo = algorithm or draw(st.sampled_from(list(Algorithm)))
    limit = draw(st.integers(1, 50))
    window = draw(st.integers(max(1_000, limit), 120_000_000))
    capacity = limit
    if algo is Algorithm.TOKEN_BUCKET:
        capacity = draw(st.integers(1, 60))
    return LimitSpec(algo, limit, window, capacity)


@dataclass(frozen=True)
class Step:
    dt: int
    cost: int


@st.composite
def schedules(draw: st.DrawFn, spec: LimitSpec) -> list[Step]:
    # Mix bursts (dt=0), sub-window gaps and long idle gaps; costs may exceed capacity.
    dt = st.one_of(
        st.just(0),
        st.integers(0, spec.window_us // 4 + 1),
        st.integers(0, 3 * spec.window_us),
    )
    cost = st.integers(1, spec.capacity + 1)
    return draw(st.lists(st.builds(Step, dt, cost), max_size=60))


@dataclass(frozen=True)
class Event:
    now: int
    cost: int
    allowed: bool
    state_before: State | None
    state_after: State | None


def simulate(spec: LimitSpec, steps: list[Step]) -> list[Event]:
    impl = get_impl(spec.algorithm)
    state: State | None = None
    now = T0
    events = []
    for step in steps:
        now += step.dt
        e = impl.evaluate(state, now, spec, step.cost)
        new_state = e.new_state if e.allowed else state
        events.append(Event(now, step.cost, e.allowed, state, new_state))
        state = new_state
    return events


@st.composite
def scenarios(draw: st.DrawFn, algorithm: Algorithm | None = None) -> tuple[LimitSpec, list[Step]]:
    spec = draw(specs(algorithm))
    return spec, draw(schedules(spec))


@given(scenarios())
def test_evaluate_and_peek_agree_and_rejection_has_no_state(
    scenario: tuple[LimitSpec, list[Step]],
) -> None:
    spec, steps = scenario
    impl = get_impl(spec.algorithm)
    for ev in simulate(spec, steps):
        e = impl.evaluate(ev.state_before, ev.now, spec, ev.cost)
        p = impl.peek(ev.state_before, ev.now, spec, ev.cost)
        assert p.new_state is None
        assert e.allowed == p.allowed
        assert 0 <= e.remaining <= spec.capacity
        assert 0 <= p.remaining <= spec.capacity
        assert e.reset_after_us >= 0
        if e.allowed:
            assert e.new_state is not None
            assert e.ttl_us > 0
            assert e.retry_after_us == 0
            assert e.remaining <= p.remaining
        else:
            assert e.new_state is None
            assert (e.remaining, e.reset_after_us, e.retry_after_us) == (
                p.remaining,
                p.reset_after_us,
                p.retry_after_us,
            )
            assert (e.retry_after_us is None) == (ev.cost > spec.capacity)


@given(scenarios())
def test_retry_after_is_sufficient(scenario: tuple[LimitSpec, list[Step]]) -> None:
    spec, steps = scenario
    impl = get_impl(spec.algorithm)
    for ev in simulate(spec, steps):
        if ev.allowed:
            continue
        retry = impl.evaluate(ev.state_before, ev.now, spec, ev.cost).retry_after_us
        if retry is not None:
            later = impl.evaluate(ev.state_before, ev.now + retry, spec, ev.cost)
            assert later.allowed, (spec, ev, retry)


@given(scenarios())
def test_state_is_irrelevant_once_ttl_expires(scenario: tuple[LimitSpec, list[Step]]) -> None:
    """Deleting a key when its TTL expires must not change any future decision."""
    spec, steps = scenario
    impl = get_impl(spec.algorithm)
    for ev in simulate(spec, steps):
        if not ev.allowed:
            continue
        ttl = impl.evaluate(ev.state_before, ev.now, spec, ev.cost).ttl_us
        for extra in (0, 1, spec.window_us):
            at = ev.now + ttl + extra
            for cost in {1, ev.cost}:
                assert impl.evaluate(ev.state_after, at, spec, cost) == impl.evaluate(
                    None, at, spec, cost
                )


@given(scenarios(Algorithm.FIXED_WINDOW))
def test_fixed_window_never_exceeds_limit_per_window(
    scenario: tuple[LimitSpec, list[Step]],
) -> None:
    spec, steps = scenario
    per_window: dict[int, int] = {}
    for ev in simulate(spec, steps):
        if ev.allowed:
            assert ev.state_after is not None
            start = int(ev.state_after[0])
            assert start <= ev.now < start + spec.window_us
            per_window[start] = per_window.get(start, 0) + ev.cost
            assert per_window[start] <= spec.limit


@given(scenarios(Algorithm.SLIDING_WINDOW))
def test_sliding_window_estimate_never_exceeds_limit(
    scenario: tuple[LimitSpec, list[Step]],
) -> None:
    spec, steps = scenario
    per_window: dict[int, int] = {}
    w = spec.window_us
    for ev in simulate(spec, steps):
        if not ev.allowed:
            continue
        assert ev.state_after is not None
        index, current, previous = (int(x) for x in ev.state_after)
        elapsed = ev.now - index * w
        est = float(previous) * (float(w - elapsed) / float(w)) + float(current)
        assert est <= spec.limit + EPS
        per_window[index] = per_window.get(index, 0) + ev.cost
        assert per_window[index] == current
        assert current <= spec.limit


@given(scenarios(Algorithm.TOKEN_BUCKET))
def test_token_bucket_admits_at_most_burst_plus_refill(
    scenario: tuple[LimitSpec, list[Step]],
) -> None:
    spec, steps = scenario
    rate = spec.limit / spec.window_us
    admitted = [(ev.now, ev.cost) for ev in simulate(spec, steps) if ev.allowed]
    for i, (start, _) in enumerate(admitted):
        total = 0
        for end, cost in admitted[i:]:
            total += cost
            assert total <= spec.capacity + rate * (end - start) + 1e-6


@given(scenarios())
def test_rejected_requests_never_change_future_outcomes(
    scenario: tuple[LimitSpec, list[Step]],
) -> None:
    """Replaying only the admitted requests yields the same states: rejections are free."""
    spec, steps = scenario
    impl = get_impl(spec.algorithm)
    events = simulate(spec, steps)
    state: State | None = None
    for ev in events:
        if ev.allowed:
            e = impl.evaluate(state, ev.now, spec, ev.cost)
            assert e.allowed
            state = e.new_state
            assert state == ev.state_after
