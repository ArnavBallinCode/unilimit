"""Observability through the Django integration (WSGI and ASGI)."""

from __future__ import annotations

from unilimit import DecisionEvent, Event, InMemoryMetrics, Rule

from .conftest import LimiterFactory, asgi, wsgi


class Recorder:
    def __init__(self) -> None:
        self.events: list[Event] = []

    def __call__(self, event: Event) -> None:
        self.events.append(event)


def test_decisions_are_observed_under_wsgi(make_limiter: LimiterFactory) -> None:
    rec, metrics = Recorder(), InMemoryMetrics()
    make_limiter(observer=[rec, metrics], global_rules=[Rule("dj-global", "100/minute")])
    client = wsgi()
    statuses = [client.get("/sync").status_code for _ in range(3)]
    assert statuses == [200, 200, 429]
    decisions = [e for e in rec.events if isinstance(e, DecisionEvent)]
    assert len(decisions) == 6  # middleware step + decorator step per request
    assert [e.allowed for e in decisions if e.binding_rule == "dj-sync"] == [True, True, False]
    assert metrics.snapshot()["rejected"] == 1
    assert "203.0.113" not in repr(rec.events)


async def test_decisions_are_observed_under_asgi(make_limiter: LimiterFactory) -> None:
    rec = Recorder()
    make_limiter(observer=rec)
    async with asgi() as client:
        statuses = [(await client.get("/async")).status_code for _ in range(3)]
    assert statuses == [200, 200, 429]
    allowed = [
        e.allowed
        for e in rec.events
        if isinstance(e, DecisionEvent) and e.binding_rule == "dj-async"
    ]
    assert allowed == [True, True, False]
