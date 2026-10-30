# Metrics: built-in, Prometheus and OpenTelemetry

All metrics integrations are [observers](events.md) with the same semantics, so a
dashboard built on one works on the others.

## Semantics

| Metric | Meaning |
|---|---|
| decisions | Every decision, by `reason` and `source`. *Allowed* = reason `allowed` or `exempt`; every other reason is a rejection. |
| fail-open / fail-closed | Decisions made by `fail_mode` because the backend was unavailable (`source="degraded"`). Fail-open decisions are also allowed, fail-closed ones also rejected. |
| backend errors | Backend calls that raised `BackendUnavailable`, by exception class. |
| partial commits | Redis Cluster batches rejected after part of them was consumed. |
| backend latency | Duration of the backend call in seconds, `result="ok"` or `"error"`. Does not include planning or your observers. |
| breaker transitions | Circuit breaker transitions into `open`, `half_open`, `closed`. |
| fallback | Decisions served by the local fallback, and its activations/deactivations. |

## Built-in: `InMemoryMetrics`

No dependency. Useful in tests, for health endpoints, or to feed your own exporter.

```python
from unilimit import InMemoryMetrics, Limiter, Rule

metrics = InMemoryMetrics()
limiter = Limiter.from_url(
    "redis://localhost:6379/0",
    rules=[Rule("metrics-demo", "1/minute", key="user")],
    observer=metrics,
)
limiter.consume("metrics-demo", "alice")
limiter.consume("metrics-demo", "alice")

snapshot = metrics.snapshot()
assert snapshot["allowed"] == 1
assert snapshot["rejected"] == 1
assert snapshot["by_reason"] == {"allowed": 1, "rate_limited": 1}
assert snapshot["latency"]["ok"]["count"] == 2
limiter.close()
```

`snapshot()` returns `allowed`, `rejected`, `fail_open`, `fail_closed`, `backend_errors`,
`partial_commits`, `fallback_decisions`, `by_reason`, `breaker_transitions`,
`fallback_transitions` and `latency` (cumulative histogram buckets in seconds plus
`sum` and `count`). It is thread-safe and exact under concurrency.

## Prometheus

```bash
pip install "unilimit[prometheus]"
```

```python
from prometheus_client import CollectorRegistry, generate_latest

from unilimit import Limiter, Rule
from unilimit.integrations.prometheus import PrometheusObserver

registry = CollectorRegistry()  # omit to use prometheus_client's default REGISTRY
limiter = Limiter.from_url(
    "redis://localhost:6379/0",
    rules=[Rule("prom-demo", "1/minute", key="user")],
    observer=PrometheusObserver(registry),
)
limiter.consume("prom-demo", "alice")
limiter.consume("prom-demo", "alice")

text = generate_latest(registry).decode()
assert (
    'unilimit_decisions_total{reason="rate_limited",rule="prom-demo",source="backend"} 1.0' in text
)
assert "alice" not in text
limiter.close()
```

Metrics register in `prometheus_client.REGISTRY` by default, so they appear on the
endpoint your application already exposes (`make_asgi_app()`, `start_http_server()`,
`django-prometheus`, ...). Create one observer per registry and namespace.

| Metric | Labels |
|---|---|
| `unilimit_decisions_total` | `reason`, `source`, `rule` |
| `unilimit_degraded_decisions_total` | `mode` (`open`/`closed`) |
| `unilimit_backend_errors_total` | `backend`, `error` |
| `unilimit_partial_commits_total` | `backend` |
| `unilimit_backend_operation_duration_seconds` (histogram) | `backend`, `result` |
| `unilimit_breaker_transitions_total` | `backend`, `state` |
| `unilimit_fallback_transitions_total` | `backend`, `state` |

**Cardinality.** Every label has a small, fixed or configuration-bounded set of values:
reasons, sources, backend kinds, exception class names and the names of the rules *you*
defined. Identities, IP addresses, paths and storage keys are never labels
(`test_prometheus_series_count_is_bounded_by_configuration` drives 500 different users,
IPs and paths and checks the series count stays constant). Pass `rule_label=False` to drop
the `rule` label too, and `namespace="shop"` to rename the metrics (`shop_decisions_total`).

Useful alerts:

```text
rate(unilimit_backend_errors_total[5m]) > 0               # Redis trouble
increase(unilimit_breaker_transitions_total{state="open"}[5m]) > 0
rate(unilimit_degraded_decisions_total{mode="open"}[5m]) > 0   # limits are not enforced
histogram_quantile(0.99, rate(unilimit_backend_operation_duration_seconds_bucket[5m]))
```

## OpenTelemetry

```bash
pip install "unilimit[otel]"     # opentelemetry-api only
```

```python
from unilimit import Limiter
from unilimit.integrations.otel import OpenTelemetryObserver

limiter = Limiter.from_url("redis://localhost:6379/0", observer=OpenTelemetryObserver())
limiter.close()
```

Configure the SDK, exporters and readers in your application as usual; the observer only
uses the API, and pass `OpenTelemetryObserver(meter_provider)` to choose a provider.

It records **metrics** with the same names and attributes as above
(`unilimit.decisions`, `unilimit.degraded_decisions`, `unilimit.backend.errors`,
`unilimit.partial_commits`, `unilimit.backend.operation.duration` in seconds,
`unilimit.breaker.transitions`, `unilimit.fallback.transitions`).

It deliberately creates **no spans**. OpenTelemetry has no semantic convention for rate
limiting, and a span per decision would add overhead to every request while your HTTP
instrumentation already has a span for it. Instead, when the *current* span is
recording, it adds an event for notable occurrences only: `unilimit.rejected`,
`unilimit.degraded` and `unilimit.backend_error`, with the rule, reason and backend as
attributes (never identities, IPs or paths).

Without the extra installed, importing `unilimit.integrations.otel` raises an
`ImportError` that names `unilimit[otel]`; the core never imports it.

## Overhead

See [benchmarks](benchmarks.md#cost-of-an-observer-in-practice) for the measured cost of an
observer.
