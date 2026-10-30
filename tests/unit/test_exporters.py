"""Prometheus and OpenTelemetry observers: accuracy, cardinality, privacy, optional install."""

from __future__ import annotations

import importlib
import sys
import threading

import pytest
from opentelemetry import trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from prometheus_client import CollectorRegistry, generate_latest

from unilimit import Limiter, MemoryBackend, RequestContext, Rule
from unilimit.integrations.otel import OpenTelemetryObserver
from unilimit.integrations.prometheus import PrometheusObserver
from unilimit.testing import ManualClock

from .test_observability import SECRET_IP, SECRET_USER, FlakyBackend

RULE = Rule("api", "2/minute", key="user", algorithm="fixed_window")
CTX = RequestContext(user_id=SECRET_USER, peer=SECRET_IP, path="/private/path?token=abc")


def run_scenario(limiter: Limiter, backend: FlakyBackend) -> None:
    for _ in range(4):
        limiter.evaluate(CTX, ["api"], include_global=False)  # 2 allowed, 2 rejected
    backend.down = True
    limiter.evaluate(CTX, ["api"], include_global=False)  # fail open
    backend.down = False


def make(observer: object) -> tuple[Limiter, FlakyBackend]:
    backend = FlakyBackend(ManualClock())
    return Limiter(backend, rules=[RULE], observer=observer), backend  # type: ignore[arg-type]


# -- Prometheus -----------------------------------------------------------------------


def test_prometheus_metrics_are_accurate() -> None:
    registry = CollectorRegistry()
    limiter, backend = make(PrometheusObserver(registry))
    run_scenario(limiter, backend)

    def value(name: str, **labels: str) -> float:
        return registry.get_sample_value(name, labels) or 0.0

    assert value("unilimit_decisions_total", reason="allowed", source="backend", rule="api") == 2
    assert (
        value("unilimit_decisions_total", reason="rate_limited", source="backend", rule="api") == 2
    )
    assert value("unilimit_decisions_total", reason="allowed", source="degraded", rule="") == 1
    assert value("unilimit_degraded_decisions_total", mode="open") == 1
    assert value("unilimit_degraded_decisions_total", mode="closed") == 0
    assert value("unilimit_backend_errors_total", backend="flaky", error="BackendUnavailable") == 1
    assert (
        value("unilimit_backend_operation_duration_seconds_count", backend="flaky", result="ok")
        == 4
    )
    assert (
        value("unilimit_backend_operation_duration_seconds_count", backend="flaky", result="error")
        == 1
    )


def test_prometheus_never_exposes_identities_or_paths() -> None:
    registry = CollectorRegistry()
    limiter, backend = make(PrometheusObserver(registry))
    run_scenario(limiter, backend)
    limiter.consume("api", SECRET_USER)
    text = generate_latest(registry).decode()
    for secret in (SECRET_USER, SECRET_IP, "private", "token", limiter.key_for("api", SECRET_USER)):
        assert secret not in text
    labels = {
        key for family in registry.collect() for sample in family.samples for key in sample.labels
    }
    assert labels <= {
        "reason",
        "source",
        "rule",
        "mode",
        "backend",
        "error",
        "result",
        "state",
        "le",
    }


def test_prometheus_series_count_is_bounded_by_configuration() -> None:
    registry = CollectorRegistry()
    limiter, _ = make(PrometheusObserver(registry))
    for i in range(500):  # 500 different identities and paths
        limiter.evaluate(
            RequestContext(user_id=f"user-{i}", peer=f"10.0.{i % 250}.1", path=f"/p/{i}"),
            ["api"],
            include_global=False,
        )
    series = sum(len(f.samples) for f in registry.collect())
    assert series < 40


def test_prometheus_rule_label_can_be_dropped() -> None:
    registry = CollectorRegistry()
    limiter, backend = make(PrometheusObserver(registry, rule_label=False))
    run_scenario(limiter, backend)
    assert (
        registry.get_sample_value(
            "unilimit_decisions_total", {"reason": "allowed", "source": "backend", "rule": ""}
        )
        == 2
    )


def test_prometheus_namespace_and_duplicate_registration() -> None:
    registry = CollectorRegistry()
    PrometheusObserver(registry, namespace="shop")
    assert "shop_decisions_total" in generate_latest(registry).decode()
    with pytest.raises(ValueError, match="Duplicated"):
        PrometheusObserver(registry, namespace="shop")


def test_prometheus_concurrent_updates_are_exact() -> None:
    registry = CollectorRegistry()
    limiter = Limiter(
        MemoryBackend(clock=ManualClock()),
        rules=[Rule("hot", "1000000/day", key="global")],
        observer=PrometheusObserver(registry),
    )

    def work() -> None:
        for _ in range(400):
            limiter.consume("hot", "x")

    pool = [threading.Thread(target=work) for _ in range(8)]
    for t in pool:
        t.start()
    for t in pool:
        t.join()
    assert (
        registry.get_sample_value(
            "unilimit_decisions_total", {"reason": "allowed", "source": "backend", "rule": "hot"}
        )
        == 3200
    )


def test_prometheus_breaker_and_fallback_metrics() -> None:
    from unilimit.observability import BreakerEvent, FallbackEvent

    registry = CollectorRegistry()
    observer = PrometheusObserver(registry)
    observer(BreakerEvent(0.0, "redis", "open", "ConnectionError", 0.5))
    observer(BreakerEvent(0.0, "redis", "half_open", None, 0.5))
    observer(BreakerEvent(0.0, "redis", "closed", None, 0.5))
    observer(FallbackEvent(0.0, "redis", "activated", 0))
    for state in ("open", "half_open", "closed"):
        assert (
            registry.get_sample_value(
                "unilimit_breaker_transitions_total", {"backend": "redis", "state": state}
            )
            == 1
        )
    assert (
        registry.get_sample_value(
            "unilimit_fallback_transitions_total", {"backend": "redis", "state": "activated"}
        )
        == 1
    )


# -- OpenTelemetry --------------------------------------------------------------------


def otel_points(reader: InMemoryMetricReader) -> dict[str, list[object]]:
    data = reader.get_metrics_data()
    out: dict[str, list[object]] = {}
    for rm in data.resource_metrics if data else []:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                out[metric.name] = list(metric.data.data_points)
    return out


def test_otel_metrics_are_accurate() -> None:
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    limiter, backend = make(OpenTelemetryObserver(provider))
    run_scenario(limiter, backend)
    points = otel_points(reader)
    decisions = {
        (p.attributes["reason"], p.attributes["source"]): p.value  # type: ignore[attr-defined]
        for p in points["unilimit.decisions"]
    }
    assert decisions == {
        ("allowed", "backend"): 2,
        ("rate_limited", "backend"): 2,
        ("allowed", "degraded"): 1,
    }
    assert points["unilimit.degraded_decisions"][0].value == 1  # type: ignore[attr-defined]
    assert points["unilimit.backend.errors"][0].value == 1  # type: ignore[attr-defined]
    durations = {
        p.attributes["result"]: p.count  # type: ignore[attr-defined]
        for p in points["unilimit.backend.operation.duration"]
    }
    assert durations == {"ok": 4, "error": 1}


def test_otel_adds_events_to_the_recording_span_only_for_notable_occurrences() -> None:
    exporter = InMemorySpanExporter()
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = tracer_provider.get_tracer("test")
    limiter, backend = make(OpenTelemetryObserver(MeterProvider()))
    with tracer.start_as_current_span("request"):
        limiter.evaluate(CTX, ["api"], include_global=False)  # allowed: no event
        limiter.evaluate(CTX, ["api"], include_global=False)
        limiter.evaluate(CTX, ["api"], include_global=False)  # rejected
        backend.down = True
        limiter.evaluate(CTX, ["api"], include_global=False)  # error + degraded
    (span,) = exporter.get_finished_spans()
    assert [e.name for e in span.events] == [
        "unilimit.rejected",
        "unilimit.backend_error",
        "unilimit.degraded",
    ]
    blob = repr([(e.name, dict(e.attributes or {})) for e in span.events])
    for secret in (SECRET_USER, SECRET_IP, "private", "token"):
        assert secret not in blob


def test_otel_without_a_recording_span_adds_nothing() -> None:
    limiter, _ = make(OpenTelemetryObserver(MeterProvider()))
    assert not trace.get_current_span().is_recording()
    for _ in range(3):
        limiter.evaluate(CTX, ["api"], include_global=False)  # must not raise


# -- optional installation ------------------------------------------------------------


@pytest.mark.parametrize(
    ("module", "blocked", "extra"),
    [
        ("unilimit.integrations.prometheus", "prometheus_client", "unilimit[prometheus]"),
        ("unilimit.integrations.otel", "opentelemetry", "unilimit[otel]"),
    ],
)
def test_clear_error_when_the_extra_is_not_installed(
    monkeypatch: pytest.MonkeyPatch, module: str, blocked: str, extra: str
) -> None:
    saved = {k: v for k, v in sys.modules.items() if k == module or k.startswith(blocked)}
    for name in saved:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setitem(sys.modules, blocked, None)  # makes `import <blocked>` fail
    with pytest.raises(ImportError, match=extra.replace("[", r"\[").replace("]", r"\]")):
        importlib.import_module(module)


def test_the_core_never_imports_optional_exporters() -> None:
    import subprocess

    code = (
        "import sys, unilimit; "
        "bad = [m for m in ('prometheus_client', 'opentelemetry', 'django', 'fastapi', 'redis') "
        "if m in sys.modules]; assert not bad, bad"
    )
    subprocess.run([sys.executable, "-c", code], check=True)
