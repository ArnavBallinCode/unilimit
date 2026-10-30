"""Observability through the FastAPI integration, on every backend."""

from __future__ import annotations

from fastapi import Depends, FastAPI

from unilimit import DecisionEvent, Event, InMemoryMetrics, Rule
from unilimit.integrations.fastapi import RateLimit, setup

from .conftest import LimiterFactory, client


class Recorder:
    def __init__(self) -> None:
        self.events: list[Event] = []

    def __call__(self, event: Event) -> None:
        self.events.append(event)


async def test_middleware_and_dependency_decisions_are_observed(
    make_limiter: LimiterFactory,
) -> None:
    rec, metrics = Recorder(), InMemoryMetrics()
    limiter = make_limiter(
        global_rules=[Rule("site", "100/minute", key="ip")], observer=[rec, metrics]
    )
    app = FastAPI()
    setup(app, limiter)

    @app.get("/x", dependencies=[Depends(RateLimit("x", rate="2/minute"))])
    async def x() -> dict[str, bool]:
        return {"ok": True}

    async with client(app) as c:
        statuses = [(await c.get("/x")).status_code for _ in range(3)]
    assert statuses == [200, 200, 429]
    decisions = [e for e in rec.events if isinstance(e, DecisionEvent)]
    # Each request is two atomic steps: the global rule, then the route rule.
    assert [(e.binding_rule, e.allowed) for e in decisions] == [
        ("site", True), ("x", True),
        ("site", True), ("x", True),
        ("site", True), ("x", False),
    ]  # fmt: skip
    snap = metrics.snapshot()
    assert snap["allowed"] == 5
    assert snap["rejected"] == 1
    assert "203.0.113" not in repr(rec.events)


async def test_a_raising_observer_does_not_change_responses(make_limiter: LimiterFactory) -> None:
    def explode(event: Event) -> None:
        raise RuntimeError("observer bug")

    app = FastAPI()
    setup(app, make_limiter(observer=explode))

    @app.get("/x", dependencies=[Depends(RateLimit("x", rate="1/minute"))])
    async def x() -> dict[str, bool]:
        return {"ok": True}

    async with client(app) as c:
        assert [(await c.get("/x")).status_code for _ in range(2)] == [200, 429]
