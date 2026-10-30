"""OpenTelemetry integration for UniLimit. Requires the ``otel`` extra:
``pip install "unilimit[otel]"`` (it needs only ``opentelemetry-api``; configure the SDK
and exporters in your application as usual).

```python
from unilimit.integrations.otel import OpenTelemetryObserver

limiter = Limiter.from_url("redis://localhost:6379/0", observer=OpenTelemetryObserver())
```

What it does, and what it deliberately does not:

- **Metrics** with the same semantics as the Prometheus observer (instrument names
  ``unilimit.decisions``, ``unilimit.degraded_decisions``, ``unilimit.backend.errors``,
  ``unilimit.partial_commits``, ``unilimit.backend.operation.duration`` (seconds),
  ``unilimit.breaker.transitions``, ``unilimit.fallback.transitions``). Attributes are
  the same bounded set as the Prometheus labels.
- **Span events, not spans.** OpenTelemetry has no semantic convention for rate limiting,
  and a span per decision would add overhead to every request for little value: your HTTP
  or framework instrumentation already has a span for the request. This observer adds an
  event to the *current* span, only when that span is recording and only for notable
  occurrences: a rejection (``unilimit.rejected``), a degraded decision
  (``unilimit.degraded``) or a backend error (``unilimit.backend_error``). Attributes are
  rule name, reason, backend and outcome: never identities, IPs, paths or keys.
"""

from __future__ import annotations

try:
    from opentelemetry import metrics, trace
    from opentelemetry.metrics import MeterProvider
except ImportError as exc:  # pragma: no cover - exercised only without the extra
    raise ImportError(
        'The OpenTelemetry integration requires opentelemetry-api: pip install "unilimit[otel]"'
    ) from exc

from unilimit.observability import (
    BackendUnavailableEvent,
    BreakerEvent,
    DecisionEvent,
    Event,
    FallbackEvent,
)


class OpenTelemetryObserver:
    """Exports UniLimit events as OpenTelemetry metrics and current-span events."""

    def __init__(self, meter_provider: MeterProvider | None = None, *, rule_label: bool = True):
        meter = (
            meter_provider.get_meter("unilimit")
            if meter_provider is not None
            else metrics.get_meter("unilimit")
        )
        self._rule_label = rule_label
        self._decisions = meter.create_counter("unilimit.decisions", description="Decisions.")
        self._degraded = meter.create_counter(
            "unilimit.degraded_decisions", description="Decisions made by fail_mode."
        )
        self._errors = meter.create_counter(
            "unilimit.backend.errors", description="Backend calls that raised BackendUnavailable."
        )
        self._partial = meter.create_counter(
            "unilimit.partial_commits", description="Cluster batches partially consumed."
        )
        self._latency = meter.create_histogram(
            "unilimit.backend.operation.duration", unit="s", description="Backend call latency."
        )
        self._breaker = meter.create_counter(
            "unilimit.breaker.transitions", description="Circuit breaker transitions."
        )
        self._fallback = meter.create_counter(
            "unilimit.fallback.transitions", description="Local fallback transitions."
        )

    def __call__(self, event: Event) -> None:
        if isinstance(event, DecisionEvent):
            rule = (event.binding_rule or "") if self._rule_label else ""
            self._decisions.add(1, {"reason": event.reason, "source": event.source, "rule": rule})
            if event.source == "degraded":
                mode = "open" if event.allowed else "closed"
                self._degraded.add(1, {"mode": mode})
                self._span_event("unilimit.degraded", {"mode": mode, "backend": event.backend})
            elif not event.allowed:
                self._span_event("unilimit.rejected", {"reason": event.reason, "rule": rule})
            if event.partial_commit:
                self._partial.add(1, {"backend": event.backend})
            if event.source == "backend" and event.duration is not None:
                self._latency.record(event.duration, {"backend": event.backend, "result": "ok"})
        elif isinstance(event, BackendUnavailableEvent):
            self._errors.add(1, {"backend": event.backend, "error": event.error})
            self._latency.record(event.duration, {"backend": event.backend, "result": "error"})
            self._span_event(
                "unilimit.backend_error", {"backend": event.backend, "error": event.error}
            )
        elif isinstance(event, BreakerEvent):
            self._breaker.add(1, {"backend": event.backend, "state": event.state})
        elif isinstance(event, FallbackEvent):
            self._fallback.add(1, {"backend": event.backend, "state": event.state})

    @staticmethod
    def _span_event(name: str, attributes: dict[str, str]) -> None:
        span = trace.get_current_span()
        if span.is_recording():
            span.add_event(name, attributes)
