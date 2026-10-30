"""``InMemoryMetrics``: a dependency-free observer that aggregates events into counters
and a latency histogram. Use it directly (``snapshot()``), to feed your own exporter, or
as the reference for the Prometheus and OpenTelemetry observers.

Semantics (shared by every metrics integration):

- **allowed** / **rejected**: decisions by outcome. A fail-open decision is *allowed* and a
  fail-closed one *rejected*; they are also counted in **fail_open** / **fail_closed**.
- **by_reason**: decisions per :class:`~unilimit.Reason` value.
- **backend_errors**: backend calls that raised ``BackendUnavailable``.
- **partial_commits**: Redis Cluster batches rejected after part of them was consumed.
- **fallback_decisions**: decisions served by the process-local fallback.
- **latency**: backend call duration in seconds, ``ok`` for calls that succeeded and
  ``error`` for those that raised ``BackendUnavailable``.
- **breaker_transitions** and **fallback_transitions**: counts per new state.
"""

from __future__ import annotations

import threading
from bisect import bisect_left
from collections import Counter
from typing import Any

from unilimit.observability import (
    BackendUnavailableEvent,
    BreakerEvent,
    DecisionEvent,
    Event,
    FallbackEvent,
)

#: Upper bounds (seconds) of the latency histogram buckets; a final ``+Inf`` is implicit.
LATENCY_BUCKETS = (
    0.0001,
    0.00025,
    0.0005,
    0.001,
    0.0025,
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
)


class _Histogram:
    def __init__(self, bounds: tuple[float, ...]) -> None:
        self.bounds = bounds
        self.counts = [0] * (len(bounds) + 1)
        self.total = 0.0
        self.count = 0

    def observe(self, value: float) -> None:
        self.counts[bisect_left(self.bounds, value)] += 1
        self.total += value
        self.count += 1

    def snapshot(self) -> dict[str, Any]:
        cumulative, running = [], 0
        for c in self.counts:
            running += c
            cumulative.append(running)
        return {
            "buckets": dict(zip([*map(str, self.bounds), "+Inf"], cumulative, strict=True)),
            "sum": self.total,
            "count": self.count,
        }


class InMemoryMetrics:
    """Thread-safe aggregation of observability events. Pass it as ``observer=``."""

    def __init__(self, buckets: tuple[float, ...] = LATENCY_BUCKETS) -> None:
        self._lock = threading.Lock()
        self._buckets = buckets
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self._counts: Counter[str] = Counter()
            self._reasons: Counter[str] = Counter()
            self._breaker: Counter[str] = Counter()
            self._fallback: Counter[str] = Counter()
            self._latency = {"ok": _Histogram(self._buckets), "error": _Histogram(self._buckets)}

    def __call__(self, event: Event) -> None:
        with self._lock:
            if isinstance(event, DecisionEvent):
                self._counts["allowed" if event.allowed else "rejected"] += 1
                self._reasons[event.reason] += 1
                if event.source == "degraded":
                    self._counts["fail_open" if event.allowed else "fail_closed"] += 1
                elif event.source == "fallback":
                    self._counts["fallback_decisions"] += 1
                if event.partial_commit:
                    self._counts["partial_commits"] += 1
                if event.source == "backend" and event.duration is not None:
                    self._latency["ok"].observe(event.duration)
            elif isinstance(event, BackendUnavailableEvent):
                self._counts["backend_errors"] += 1
                self._latency["error"].observe(event.duration)
            elif isinstance(event, BreakerEvent):
                self._breaker[event.state] += 1
            elif isinstance(event, FallbackEvent):
                self._fallback[event.state] += 1

    def snapshot(self) -> dict[str, Any]:
        """A consistent copy of every figure, JSON-serialisable."""
        with self._lock:
            c = self._counts
            return {
                "allowed": c["allowed"],
                "rejected": c["rejected"],
                "fail_open": c["fail_open"],
                "fail_closed": c["fail_closed"],
                "backend_errors": c["backend_errors"],
                "partial_commits": c["partial_commits"],
                "fallback_decisions": c["fallback_decisions"],
                "by_reason": dict(self._reasons),
                "breaker_transitions": dict(self._breaker),
                "fallback_transitions": dict(self._fallback),
                "latency": {k: h.snapshot() for k, h in self._latency.items()},
            }
