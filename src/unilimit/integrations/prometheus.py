"""Prometheus metrics for UniLimit. Requires the ``prometheus`` extra:
``pip install "unilimit[prometheus]"``.

```python
from unilimit import Limiter
from unilimit.integrations.prometheus import PrometheusObserver

limiter = Limiter.from_url("redis://localhost:6379/0", observer=PrometheusObserver())
```

The metrics register in ``prometheus_client.REGISTRY`` unless you pass ``registry=``, so
they appear on the endpoint your application already exposes. Create one observer per
registry: registering the same metric names twice raises ``ValueError``.

Cardinality: every label has a small, fixed or configuration-bounded set of values (rule
names you defined, backend kind, exception class names). Identities, IP addresses, paths
and storage keys are never labels. ``rule_label=False`` drops the rule label as well.
"""

from __future__ import annotations

try:
    from prometheus_client import REGISTRY, CollectorRegistry, Counter, Histogram
except ImportError as exc:  # pragma: no cover - exercised only without the extra
    raise ImportError(
        'The Prometheus integration requires prometheus-client: pip install "unilimit[prometheus]"'
    ) from exc

from unilimit.metrics import LATENCY_BUCKETS
from unilimit.observability import (
    BackendUnavailableEvent,
    BreakerEvent,
    DecisionEvent,
    Event,
    FallbackEvent,
)


class PrometheusObserver:
    """Exports UniLimit events as Prometheus metrics (names use ``namespace``, default
    ``unilimit``):

    ``<ns>_decisions_total{reason, source, rule}``
        Decisions. ``reason`` is a ``unilimit.Reason`` value, ``source`` is ``backend``,
        ``fallback``, ``policy`` or ``degraded``, ``rule`` the binding rule (``""`` if
        none). Allowed = ``reason`` in ``allowed``/``exempt``; everything else rejected.
    ``<ns>_degraded_decisions_total{mode}``
        Decisions made by ``fail_mode`` because the backend was unavailable
        (``mode`` = ``open`` or ``closed``).
    ``<ns>_backend_errors_total{backend, error}``
        Backend calls that raised ``BackendUnavailable``; ``error`` is the exception class.
    ``<ns>_partial_commits_total{backend}``
        Redis Cluster batches rejected after part of them was consumed.
    ``<ns>_backend_operation_duration_seconds{backend, result}``
        Backend call latency; ``result`` is ``ok`` or ``error``.
    ``<ns>_breaker_transitions_total{backend, state}``
        Circuit breaker transitions into ``open``, ``half_open`` or ``closed``.
    ``<ns>_fallback_transitions_total{backend, state}``
        Local fallback ``activated`` / ``deactivated``.
    """

    def __init__(
        self,
        registry: CollectorRegistry | None = None,
        *,
        namespace: str = "unilimit",
        rule_label: bool = True,
        buckets: tuple[float, ...] = LATENCY_BUCKETS,
    ) -> None:
        reg = registry if registry is not None else REGISTRY
        self._rule_label = rule_label
        self._decisions = Counter(
            f"{namespace}_decisions_total",
            "Rate-limit decisions.",
            ["reason", "source", "rule"],
            registry=reg,
        )
        self._degraded = Counter(
            f"{namespace}_degraded_decisions_total",
            "Decisions made by fail_mode because the backend was unavailable.",
            ["mode"],
            registry=reg,
        )
        self._errors = Counter(
            f"{namespace}_backend_errors_total",
            "Backend calls that raised BackendUnavailable.",
            ["backend", "error"],
            registry=reg,
        )
        self._partial = Counter(
            f"{namespace}_partial_commits_total",
            "Cluster batches rejected after part of them was consumed.",
            ["backend"],
            registry=reg,
        )
        self._latency = Histogram(
            f"{namespace}_backend_operation_duration_seconds",
            "Backend call latency in seconds.",
            ["backend", "result"],
            buckets=buckets,
            registry=reg,
        )
        self._breaker = Counter(
            f"{namespace}_breaker_transitions_total",
            "Circuit breaker transitions.",
            ["backend", "state"],
            registry=reg,
        )
        self._fallback = Counter(
            f"{namespace}_fallback_transitions_total",
            "Local fallback activations and deactivations.",
            ["backend", "state"],
            registry=reg,
        )

    def __call__(self, event: Event) -> None:
        if isinstance(event, DecisionEvent):
            rule = (event.binding_rule or "") if self._rule_label else ""
            self._decisions.labels(event.reason, event.source, rule).inc()
            if event.source == "degraded":
                self._degraded.labels("open" if event.allowed else "closed").inc()
            if event.partial_commit:
                self._partial.labels(event.backend).inc()
            if event.source == "backend" and event.duration is not None:
                self._latency.labels(event.backend, "ok").observe(event.duration)
        elif isinstance(event, BackendUnavailableEvent):
            self._errors.labels(event.backend, event.error).inc()
            self._latency.labels(event.backend, "error").observe(event.duration)
        elif isinstance(event, BreakerEvent):
            self._breaker.labels(event.backend, event.state).inc()
        elif isinstance(event, FallbackEvent):
            self._fallback.labels(event.backend, event.state).inc()
