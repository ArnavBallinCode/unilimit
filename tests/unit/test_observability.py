"""Events, observers, throttled failure logging and the process-local fallback.

These tests use a fault-injecting backend; the same behaviour against real Redis
(breaker transitions, recovery, Cluster) is in tests/redis_backend/test_observability.py.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Sequence
from dataclasses import fields
from typing import TypeVar

import pytest

from unilimit import (
    BackendUnavailable,
    BackendUnavailableEvent,
    Decision,
    DecisionEvent,
    Event,
    FallbackEvent,
    Identity,
    InMemoryMetrics,
    Limiter,
    MemoryBackend,
    Reason,
    RequestContext,
    Rule,
)
from unilimit._logging import ThrottledLogger
from unilimit.backends.base import BatchResult, ConsumeOp, OpResult
from unilimit.core.errors import ConfigurationError
from unilimit.core.policy import AccessList
from unilimit.observability import EVENT_NAMES, SCHEMA_VERSION, Emitter
from unilimit.testing import ManualClock

SECRET_USER = "alice-secret-id-42"
SECRET_IP = "203.0.113.77"


class FlakyBackend:
    """A memory backend that can be switched off, to inject ``BackendUnavailable``."""

    name = "flaky"

    def __init__(self, clock: ManualClock) -> None:
        self.inner = MemoryBackend(clock=clock)
        self.down = False
        self.partial = False
        self.calls = 0

    def _check(self) -> None:
        self.calls += 1
        if self.down:
            raise BackendUnavailable("down", partial_commit=self.partial)

    def consume(self, ops: Sequence[ConsumeOp]) -> BatchResult:
        self._check()
        return self.inner.consume(ops)

    def peek(self, ops: Sequence[ConsumeOp]) -> tuple[OpResult, ...]:
        self._check()
        return self.inner.peek(ops)

    def reset(self, keys: Sequence[str]) -> None:
        self.inner.reset(keys)

    def close(self) -> None:
        self.inner.close()

    async def aconsume(self, ops: Sequence[ConsumeOp]) -> BatchResult:
        self._check()
        return await self.inner.aconsume(ops)

    async def apeek(self, ops: Sequence[ConsumeOp]) -> tuple[OpResult, ...]:
        self._check()
        return await self.inner.apeek(ops)

    async def areset(self, keys: Sequence[str]) -> None:
        await self.inner.areset(keys)

    async def aclose(self) -> None:
        await self.inner.aclose()


E = TypeVar("E")


class Recorder:
    def __init__(self) -> None:
        self.events: list[Event] = []

    def __call__(self, event: Event) -> None:
        self.events.append(event)

    def of(self, kind: type[E]) -> list[E]:
        return [e for e in self.events if isinstance(e, kind)]


RULE = Rule("api", "2/minute", key="user", algorithm="fixed_window")


def make(clock: ManualClock, **kwargs: object) -> tuple[Limiter, FlakyBackend, Recorder]:
    backend = FlakyBackend(clock)
    recorder = Recorder()
    kwargs.setdefault("observer", recorder)
    limiter = Limiter(backend, rules=[RULE], **kwargs)  # type: ignore[arg-type]
    return limiter, backend, recorder


CTX = RequestContext(user_id=SECRET_USER, peer=SECRET_IP, path="/private/path?token=abc")


# -- events ---------------------------------------------------------------------------


def test_decision_events_for_allowed_and_rejected(clock: ManualClock) -> None:
    limiter, _, rec = make(clock)
    decisions = [limiter.evaluate(CTX, ["api"], include_global=False) for _ in range(3)]
    assert [d.allowed for d in decisions] == [True, True, False]
    events = rec.of(DecisionEvent)
    assert [e.allowed for e in events] == [True, True, False]
    assert [e.reason for e in events] == ["allowed", "allowed", "rate_limited"]
    first = events[0]
    assert first.source == "backend"
    assert first.backend == "flaky"
    assert first.binding_rule == "api"
    assert first.rules[0].name == "api"
    assert first.rules[0].algorithm == "fixed_window"
    assert first.fail_mode is None
    assert first.duration is not None and first.duration >= 0
    assert not first.partial_commit


def test_policy_decisions_are_events_without_backend_time(clock: ManualClock) -> None:
    limiter, backend, rec = make(clock, exempt_paths=["/health"], deny=AccessList(users=["banned"]))
    limiter.evaluate(RequestContext(path="/health"), ["api"])
    limiter.evaluate(RequestContext(user_id="banned"), ["api"])
    limiter.evaluate(RequestContext(), ["api"], include_global=False)  # no identity: denied
    assert [(e.reason, e.source, e.duration) for e in rec.of(DecisionEvent)] == [
        ("exempt", "policy", None),
        ("denied", "policy", None),
        ("missing_identity", "policy", None),
    ]
    assert rec.of(DecisionEvent)[2].binding_rule == "api"
    assert backend.calls == 0


def test_single_rule_api_emits_decision_events(clock: ManualClock) -> None:
    limiter, _, rec = make(clock)
    assert limiter.consume("api", "u").allowed
    assert limiter.consume("api", "u").allowed
    assert not limiter.consume("api", "u").allowed
    assert [e.reason for e in rec.of(DecisionEvent)] == ["allowed", "allowed", "rate_limited"]
    limiter.peek("api", "u")  # peek is not a decision
    assert len(rec.of(DecisionEvent)) == 3


async def test_sync_async_parity(clock: ManualClock) -> None:
    sync_limiter, _, sync_rec = make(clock)
    async_limiter, _, async_rec = make(ManualClock())
    for _ in range(3):
        sync_limiter.evaluate(CTX, ["api"], include_global=False)
        await async_limiter.aevaluate(CTX, ["api"], include_global=False)
        sync_limiter.consume("api", "u")
        await async_limiter.aconsume("api", "u")

    def shape(rec: Recorder) -> list[dict[str, object]]:
        return [
            {k: v for k, v in e.to_dict().items() if k not in ("timestamp", "duration")}
            for e in rec.events
        ]

    assert shape(sync_rec) == shape(async_rec)
    assert len(sync_rec.events) == 6


def test_no_event_is_built_without_an_observer(
    clock: ManualClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    limiter, backend, _ = make(clock, observer=None)

    def boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("an event was built although nobody observes")

    monkeypatch.setattr(Limiter, "_decision_event", boom)
    assert limiter.evaluate(CTX, ["api"], include_global=False).allowed
    assert limiter.consume("api", "u").allowed
    backend.down = True
    assert limiter.evaluate(CTX, ["api"], include_global=False).allowed  # fail open


# -- event schema ---------------------------------------------------------------------

EXPECTED_SCHEMA = {
    "decision": {
        "event", "schema", "timestamp", "allowed", "reason", "source", "backend", "rules",
        "binding_rule", "fail_mode", "partial_commit", "duration",
    },
    "backend_unavailable": {
        "event", "schema", "timestamp", "backend", "operation", "error", "partial_commit",
        "duration",
    },
    "breaker": {"event", "schema", "timestamp", "backend", "state", "error", "cooldown"},
    "fallback": {"event", "schema", "timestamp", "backend", "state", "decisions"},
}  # fmt: skip


def test_event_schema_is_stable(clock: ManualClock) -> None:
    """Renaming or removing a field is a breaking change: bump SCHEMA_VERSION."""
    assert SCHEMA_VERSION == 1
    assert set(EVENT_NAMES) == set(EXPECTED_SCHEMA)
    limiter, backend, rec = make(clock, fallback="memory")
    limiter.evaluate(CTX, ["api"], include_global=False)
    backend.down = True
    limiter.evaluate(CTX, ["api"], include_global=False)  # fallback activated
    backend.down = False
    limiter.evaluate(CTX, ["api"], include_global=False)  # deactivated
    from unilimit.observability import BreakerEvent

    rec.events.append(BreakerEvent(0.0, "redis", "open", "ConnectionError", 0.5))
    seen = {e.name for e in rec.events}
    assert seen == set(EXPECTED_SCHEMA)
    for event in rec.events:
        data = event.to_dict()
        assert set(data) == EXPECTED_SCHEMA[event.name], event.name
        assert data["event"] == event.name
        assert data["schema"] == SCHEMA_VERSION
        json.dumps(data)  # JSON-serialisable
        assert {f.name for f in fields(event)} | {"event", "schema"} == set(data)


def test_events_never_contain_sensitive_data(clock: ManualClock) -> None:
    limiter, backend, rec = make(clock, fallback="memory", identity_secret="s3cret-key")
    wire = Rule("wire", "1/minute", key="ip")
    ctx = RequestContext(user_id=SECRET_USER, peer=SECRET_IP, path="/private/path?token=abc")
    for _ in range(3):
        limiter.evaluate(ctx, ["api", wire], include_global=False)
    backend.down = True
    limiter.evaluate(ctx, ["api", wire], include_global=False)
    limiter.consume("api", SECRET_USER)
    storage_key = limiter.key_for("api", SECRET_USER)
    blob = json.dumps([e.to_dict() for e in rec.events]) + repr(rec.events)
    for secret in (SECRET_USER, SECRET_IP, "private", "token=abc", "s3cret-key", storage_key):
        assert secret not in blob, secret
    digest = storage_key.split("{")[1].rstrip("}")
    assert digest not in blob


# -- observer behaviour ---------------------------------------------------------------


def test_observer_exceptions_never_change_decisions(
    clock: ManualClock, caplog: pytest.LogCaptureFixture
) -> None:
    def explode(event: Event) -> None:
        raise RuntimeError("observer bug")

    good = Recorder()
    limiter, backend, _ = make(clock, observer=[explode, good])
    with caplog.at_level(logging.WARNING, logger="unilimit"):
        decisions = [limiter.evaluate(CTX, ["api"], include_global=False) for _ in range(3)]
        results = [limiter.consume("api", "other") for _ in range(3)]
        backend.down = True
        failed = limiter.evaluate(CTX, ["api"], include_global=False)
    assert [d.allowed for d in decisions] == [True, True, False]
    assert [r.allowed for r in results] == [True, True, False]
    assert failed.allowed and failed.degraded  # fail open, unaffected
    assert len(good.events) > 0  # later observers still run
    assert "RuntimeError" in caplog.text
    assert "observer bug" not in caplog.text  # messages may carry data: class names only
    # throttled: many failing observer calls, one log line in the interval
    assert caplog.text.count("an observer raised") <= 2


async def test_observer_exceptions_async(clock: ManualClock) -> None:
    def explode(event: Event) -> None:
        raise ValueError

    limiter, _, _ = make(clock, observer=explode)
    assert (await limiter.aconsume("api", "u")).allowed
    assert [
        (await limiter.aevaluate(CTX, ["api"], include_global=False)).allowed for _ in range(3)
    ] == [True, True, False]


def test_building_an_event_cannot_break_enforcement(
    clock: ManualClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    limiter, _, _ = make(clock)

    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("bug in event construction")

    monkeypatch.setattr(Limiter, "_decision_event", boom)
    assert limiter.evaluate(CTX, ["api"], include_global=False).allowed


def test_observers_can_be_added_and_removed(clock: ManualClock) -> None:
    limiter, _, rec = make(clock)
    late = Recorder()
    limiter.add_observer(late)
    limiter.consume("api", "u")
    limiter.remove_observer(late)
    limiter.consume("api", "u")
    assert len(late.events) == 1
    assert len(rec.events) == 2


def test_emitter_without_observers_is_disabled() -> None:
    assert not Emitter().enabled
    assert Emitter([lambda e: None]).enabled


# -- failure handling, fail-open/closed and partial commits ---------------------------


def test_fail_open_and_closed_events(clock: ManualClock, caplog: pytest.LogCaptureFixture) -> None:
    strict = Rule("strict", "5/minute", key="user", fail_mode="closed")
    limiter, backend, rec = make(clock)
    backend.down = True
    with caplog.at_level(logging.WARNING, logger="unilimit"):
        open_ = limiter.evaluate(CTX, ["api"], include_global=False)
        closed = limiter.evaluate(CTX, [strict], include_global=False)
    assert open_.allowed and open_.degraded and not open_.fallback
    assert not closed.allowed and closed.reason is Reason.BACKEND_UNAVAILABLE
    decisions = rec.of(DecisionEvent)
    assert [(e.source, e.fail_mode, e.allowed) for e in decisions] == [
        ("degraded", "open", True),
        ("degraded", "closed", False),
    ]
    errors = rec.of(BackendUnavailableEvent)
    assert [(e.backend, e.operation, e.error) for e in errors] == [
        ("flaky", "consume", "BackendUnavailable")
    ] * 2
    assert "failing open" in caplog.text and "failing closed" in caplog.text


def test_partial_commit_is_reported(clock: ManualClock) -> None:
    limiter, backend, rec = make(clock)
    backend.down = backend.partial = True
    decision = limiter.evaluate(CTX, ["api"], include_global=False)
    assert decision.partial_commit
    assert rec.of(DecisionEvent)[0].partial_commit
    assert rec.of(BackendUnavailableEvent)[0].partial_commit


def test_peek_failure_is_an_error_event_not_a_decision(clock: ManualClock) -> None:
    limiter, backend, rec = make(clock)
    backend.down = True
    assert limiter.peek("api", "u").degraded
    assert [e.operation for e in rec.of(BackendUnavailableEvent)] == ["peek"]
    assert rec.of(DecisionEvent) == []


# -- metrics --------------------------------------------------------------------------


def test_metrics_are_accurate(clock: ManualClock) -> None:
    metrics = InMemoryMetrics()
    strict = Rule("strict", "5/minute", key="user", fail_mode="closed")
    limiter, backend, _ = make(clock, observer=metrics)
    for _ in range(5):
        limiter.evaluate(CTX, ["api"], include_global=False)  # 2 allowed, 3 rejected
    backend.down = True
    limiter.evaluate(CTX, ["api"], include_global=False)  # fail open
    limiter.evaluate(CTX, [strict], include_global=False)  # fail closed
    backend.partial = True
    limiter.evaluate(CTX, ["api"], include_global=False)  # fail open + partial
    snap = metrics.snapshot()
    assert snap["allowed"] == 4 and snap["rejected"] == 4
    assert snap["fail_open"] == 2 and snap["fail_closed"] == 1
    assert snap["backend_errors"] == 3
    assert snap["partial_commits"] == 1
    assert snap["by_reason"] == {"allowed": 4, "rate_limited": 3, "backend_unavailable": 1}
    assert snap["latency"]["ok"]["count"] == 5
    assert snap["latency"]["error"]["count"] == 3
    assert snap["latency"]["ok"]["buckets"]["+Inf"] == 5


def test_metrics_are_exact_under_concurrency(clock: ManualClock) -> None:
    metrics = InMemoryMetrics()
    limiter = Limiter(
        MemoryBackend(clock=clock),
        rules=[Rule("hot", "100000/day", key="global")],
        observer=metrics,
    )
    threads, per_thread = 8, 500

    def work() -> None:
        for _ in range(per_thread):
            limiter.consume("hot", "x")

    pool = [threading.Thread(target=work) for _ in range(threads)]
    for t in pool:
        t.start()
    for t in pool:
        t.join()
    snap = metrics.snapshot()
    assert snap["allowed"] == threads * per_thread
    assert snap["latency"]["ok"]["count"] == threads * per_thread


# -- throttled logging ----------------------------------------------------------------


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def make_logger(interval: float = 10.0) -> tuple[ThrottledLogger, list[str], FakeClock]:
    records: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record.getMessage())

    target = logging.getLogger(f"unilimit.test.{id(records)}")
    target.setLevel(logging.DEBUG)
    target.propagate = False
    target.addHandler(Capture())
    clock = FakeClock()
    return ThrottledLogger(target, interval, clock), records, clock


def test_first_occurrence_is_immediate_then_suppressed_with_a_summary() -> None:
    log, records, clock = make_logger(10)
    for _ in range(5):
        log.log(logging.WARNING, "outage", "down: %s", "x")
    assert records == ["down: x"]  # first immediately, four suppressed
    clock.now += 10
    log.log(logging.WARNING, "outage", "down: %s", "x")
    assert len(records) == 2
    assert "4 similar messages suppressed in the previous 10s" in records[1]


def test_categories_are_independent() -> None:
    log, records, _ = make_logger()
    log.log(logging.WARNING, "backend_unavailable:open", "open")
    log.log(logging.WARNING, "backend_unavailable:closed", "closed")
    log.log(logging.WARNING, "backend_unavailable:open", "open")
    assert records == ["open", "closed"]


def test_flush_reports_what_was_suppressed() -> None:
    log, records, _ = make_logger()
    for _ in range(4):
        log.log(logging.WARNING, "outage", "down")
    log.flush()
    assert len(records) == 2 and "3 similar 'outage' messages suppressed" in records[1]
    log.flush()
    assert len(records) == 2  # nothing left to report


def test_interval_zero_logs_everything() -> None:
    log, records, _ = make_logger(0)
    for _ in range(3):
        log.log(logging.WARNING, "outage", "down")
    assert len(records) == 3


def test_category_count_is_bounded() -> None:
    log, records, _ = make_logger()
    for i in range(1000):
        log.log(logging.WARNING, f"cat-{i}", "m")
    assert len(log._states) <= 257


def test_a_failing_log_handler_never_raises() -> None:
    class Broken(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            raise RuntimeError("handler broke")

    target = logging.getLogger("unilimit.test.broken")
    target.propagate = False
    target.addHandler(Broken())
    target.handlers[0].handleError = lambda record: None  # type: ignore[method-assign]
    log = ThrottledLogger(target, 0)

    class Exploding(logging.Logger):
        def log(self, *a: object, **k: object) -> None:
            raise RuntimeError("logging itself broke")

    log._target = Exploding("x")
    log.log(logging.WARNING, "c", "m")  # must not raise
    log.flush()


def test_throttling_is_exact_under_concurrent_failures() -> None:
    log, records, clock = make_logger(60)
    threads, per_thread = 8, 500

    def work() -> None:
        for _ in range(per_thread):
            log.log(logging.WARNING, "outage", "down")

    pool = [threading.Thread(target=work) for _ in range(threads)]
    for t in pool:
        t.start()
    for t in pool:
        t.join()
    assert records == ["down"]
    log.flush()
    assert f"{threads * per_thread - 1} similar" in records[1]  # every other one is accounted for


def test_a_burst_of_failures_through_the_limiter_logs_once(
    clock: ManualClock, caplog: pytest.LogCaptureFixture
) -> None:
    limiter, backend, _ = make(clock)
    backend.down = True
    with caplog.at_level(logging.WARNING, logger="unilimit"):
        for _ in range(200):
            limiter.evaluate(CTX, ["api"], include_global=False)
    assert caplog.text.count("failing open") == 1


def test_logging_interval_validation() -> None:
    from unilimit import configure_logging

    with pytest.raises(ValueError, match="non-negative"):
        configure_logging(interval=-1)
    configure_logging(interval=5)
    configure_logging(interval=30)


# -- process-local fallback -----------------------------------------------------------


def test_fallback_is_off_by_default(clock: ManualClock) -> None:
    limiter, backend, rec = make(clock)
    backend.down = True
    decisions = [limiter.evaluate(CTX, ["api"], include_global=False) for _ in range(5)]
    assert all(d.allowed and d.degraded and not d.fallback for d in decisions)
    assert limiter.fallback_active is False
    assert rec.of(FallbackEvent) == []


def test_fallback_enforces_locally_and_is_visible(
    clock: ManualClock, caplog: pytest.LogCaptureFixture
) -> None:
    limiter, backend, rec = make(clock, fallback=MemoryBackend(clock=clock))
    assert limiter.evaluate(CTX, ["api"], include_global=False).allowed  # Redis counts 1
    backend.down = True
    with caplog.at_level(logging.WARNING, logger="unilimit"):
        served = [limiter.evaluate(CTX, ["api"], include_global=False) for _ in range(3)]
    # The local counter starts from zero: it does not know what the backend counted.
    assert [d.allowed for d in served] == [True, True, False]
    assert all(d.fallback and not d.degraded for d in served)
    assert served[2].reason is Reason.RATE_LIMITED
    assert all(r.fallback for d in served for r in d.results)
    assert limiter.fallback_active
    assert [e.source for e in rec.of(DecisionEvent)][-3:] == ["fallback"] * 3
    assert [(e.state, e.decisions) for e in rec.of(FallbackEvent)] == [("activated", 0)]
    assert caplog.text.count("process-local fallback") == 1
    assert "fallback" not in rec.of(DecisionEvent)[0].source


def test_fallback_deactivates_on_recovery_and_does_not_double_consume(clock: ManualClock) -> None:
    limiter, backend, rec = make(clock, fallback=MemoryBackend(clock=clock))
    backend.down = True
    for _ in range(2):
        assert limiter.evaluate(CTX, ["api"], include_global=False).fallback
    backend.down = False
    back = limiter.evaluate(CTX, ["api"], include_global=False)
    assert not back.fallback and back.allowed
    assert back.results[0].remaining == 1  # the backend counter had not seen the outage
    assert not limiter.fallback_active
    assert [(e.state, e.decisions) for e in rec.of(FallbackEvent)] == [
        ("activated", 0),
        ("deactivated", 2),
    ]
    # The backend's own counter is untouched by the fallback period: no double consumption.
    assert limiter.peek("api", Identity("user", SECRET_USER)).remaining == 1


def test_fallback_never_loosens_a_fail_closed_rule(clock: ManualClock) -> None:
    strict = Rule("strict", "5/minute", key="user", fail_mode="closed")
    limiter, backend, rec = make(clock, fallback="memory")
    backend.down = True
    decision = limiter.evaluate(CTX, [strict], include_global=False)
    assert not decision.allowed and decision.reason is Reason.BACKEND_UNAVAILABLE
    assert not decision.fallback
    mixed = limiter.evaluate(CTX, ["api", strict], include_global=False)
    assert not mixed.allowed and not mixed.fallback  # one closed rule keeps the batch closed
    assert rec.of(FallbackEvent) == []
    single = limiter.consume(strict, "u")
    assert not single.allowed and not single.fallback


def test_fallback_default_fail_mode_closed_stays_closed(clock: ManualClock) -> None:
    limiter, backend, _ = make(clock, fallback="memory", fail_mode="closed")
    backend.down = True
    assert not limiter.evaluate(CTX, ["api"], include_global=False).allowed
    assert limiter.fallback_active is False


async def test_fallback_async_and_single_rule(clock: ManualClock) -> None:
    limiter, backend, rec = make(clock, fallback=MemoryBackend(clock=clock))
    backend.down = True
    assert [(await limiter.aconsume("api", "u")).allowed for _ in range(3)] == [True, True, False]
    assert (await limiter.aconsume("api", "u")).fallback
    d = await limiter.aevaluate(CTX, ["api"], include_global=False)
    assert d.fallback and d.allowed
    backend.down = False
    assert not (await limiter.aconsume("api", "v")).fallback
    assert not limiter.fallback_active
    assert [e.state for e in rec.of(FallbackEvent)] == ["activated", "deactivated"]


def test_fallback_keeps_the_partial_commit_flag(clock: ManualClock) -> None:
    limiter, backend, _ = make(clock, fallback="memory")
    backend.down = backend.partial = True
    assert limiter.evaluate(CTX, ["api"], include_global=False).partial_commit


def test_peek_never_uses_the_fallback(clock: ManualClock) -> None:
    limiter, backend, _ = make(clock, fallback="memory")
    backend.down = True
    assert limiter.peek("api", "u").degraded


def test_fallback_validation(clock: ManualClock) -> None:
    with pytest.raises(ConfigurationError):
        Limiter(MemoryBackend(), fallback="memory")
    with pytest.raises(ConfigurationError):
        Limiter(FlakyBackend(clock), fallback="redis")  # type: ignore[arg-type]
    with pytest.raises(ConfigurationError):
        Limiter.from_url("memory://", fallback="memory")


def test_decision_is_a_plain_value_with_fallback_flag(clock: ManualClock) -> None:
    assert Decision(allowed=True, reason=Reason.ALLOWED).fallback is False
