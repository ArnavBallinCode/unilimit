"""Observability against real Redis: circuit breaker transitions, outage and recovery,
the process-local fallback, and Redis Cluster."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator

import pytest

from tests._servers import RedisCluster, RedisProcess, server_binary
from unilimit import (
    BackendUnavailableEvent,
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
from unilimit.backends.redis import RedisBackend
from unilimit.core.keys import key_hash_slot
from unilimit.observability import BreakerEvent
from unilimit.testing import ManualClock

PER_USER = Rule("per-user", "5/minute", key="user", algorithm="fixed_window")
CTX = RequestContext(user_id="outage")


class Recorder:
    def __init__(self) -> None:
        self.events: list[Event] = []

    def __call__(self, event: Event) -> None:
        self.events.append(event)

    def breaker(self) -> list[str]:
        return [e.state for e in self.events if isinstance(e, BreakerEvent)]

    def fallback(self) -> list[str]:
        return [e.state for e in self.events if isinstance(e, FallbackEvent)]


def is_active(limiter: Limiter) -> bool:
    return limiter.fallback_active  # a function: mypy would narrow the property


@pytest.fixture
def own_redis() -> Iterator[RedisProcess]:
    server = RedisProcess(server_binary()).start()
    yield server
    server.destroy()


@pytest.fixture
def disposable_cluster() -> Iterator[RedisCluster]:
    cluster = RedisCluster(server_binary())
    try:
        cluster.start()
        yield cluster
    finally:
        cluster.destroy()


def limiter_for(
    server: RedisProcess, observer: Recorder | InMemoryMetrics, **kwargs: object
) -> Limiter:
    backend = RedisBackend(server.url, cooldown=0.2, socket_timeout=0.5, socket_connect_timeout=0.5)
    return Limiter(backend, rules=[PER_USER], observer=observer, **kwargs)  # type: ignore[arg-type]


def test_breaker_transitions_and_recovery_are_observable(own_redis: RedisProcess) -> None:
    rec, metrics = Recorder(), InMemoryMetrics()
    limiter = limiter_for(own_redis, [rec, metrics])  # type: ignore[arg-type]
    try:
        assert limiter.evaluate(CTX, ["per-user"], include_global=False).allowed
        assert rec.breaker() == []
        own_redis.stop()
        down = limiter.evaluate(CTX, ["per-user"], include_global=False)
        assert down.degraded
        assert rec.breaker() == ["open"]
        breaker = next(e for e in rec.events if isinstance(e, BreakerEvent))
        assert breaker.backend == "redis"
        assert breaker.error in {"ConnectionError", "TimeoutError"}
        assert "127.0.0.1" not in repr(breaker)  # class name only, never the message

        failing_fast = limiter.evaluate(CTX, ["per-user"], include_global=False)
        assert failing_fast.degraded
        assert rec.breaker() == ["open"]  # still cooling down: no new transition

        own_redis.start()
        time.sleep(0.3)  # let the cooldown expire
        recovered = limiter.evaluate(CTX, ["per-user"], include_global=False)
        assert recovered.allowed
        assert not recovered.degraded
    finally:
        limiter.close()
    assert rec.breaker() == ["open", "half_open", "closed"]
    snap = metrics.snapshot()
    assert snap["breaker_transitions"] == {"open": 1, "half_open": 1, "closed": 1}
    assert snap["backend_errors"] == 2
    assert snap["fail_open"] == 2
    assert snap["latency"]["error"]["count"] == 2


def test_a_failed_probe_reopens_the_breaker(own_redis: RedisProcess) -> None:
    rec = Recorder()
    limiter = limiter_for(own_redis, rec)
    try:
        limiter.evaluate(CTX, ["per-user"], include_global=False)
        own_redis.stop()
        limiter.evaluate(CTX, ["per-user"], include_global=False)
        time.sleep(0.3)
        limiter.evaluate(CTX, ["per-user"], include_global=False)  # probe fails
    finally:
        limiter.close()
    assert rec.breaker() == ["open", "half_open", "open"]


def test_fallback_serves_during_a_real_outage_and_stops_on_recovery(
    own_redis: RedisProcess,
) -> None:
    rec = Recorder()
    clock = ManualClock()
    limiter = limiter_for(own_redis, rec, fallback=MemoryBackend(clock=clock))
    try:
        assert limiter.evaluate(CTX, ["per-user"], include_global=False).results[0].remaining == 4
        own_redis.stop()
        served = [limiter.evaluate(CTX, ["per-user"], include_global=False) for _ in range(6)]
        # The local counter starts from zero, not from Redis's count of 1: five admissions
        # and then a rejection. Each worker process has its own such counter.
        assert [d.allowed for d in served] == [True] * 5 + [False]
        assert all(d.fallback for d in served)
        assert served[-1].reason is Reason.RATE_LIMITED
        assert is_active(limiter)
        own_redis.start()
        time.sleep(0.3)
        back = limiter.evaluate(CTX, ["per-user"], include_global=False)
        assert back.allowed
        assert not back.fallback
        assert not is_active(limiter)
        # Redis restarted empty: its counter starts over, and the outage was not merged in.
        assert back.results[0].remaining == 4
    finally:
        limiter.close()
    assert rec.fallback() == ["activated", "deactivated"]
    deactivated = [e for e in rec.events if isinstance(e, FallbackEvent)][-1]
    assert deactivated.decisions == 6
    sources = [e.source for e in rec.events if isinstance(e, DecisionEvent)]
    assert sources == ["backend"] + ["fallback"] * 6 + ["backend"]


async def test_async_outage_and_recovery(own_redis: RedisProcess) -> None:
    rec = Recorder()
    limiter = limiter_for(own_redis, rec, fallback="memory")
    try:
        assert (await limiter.aevaluate(CTX, ["per-user"], include_global=False)).allowed
        own_redis.stop()
        served = [
            await limiter.aevaluate(CTX, ["per-user"], include_global=False) for _ in range(6)
        ]
        assert [d.allowed for d in served] == [True] * 5 + [False]
        own_redis.start()
        await asyncio.sleep(0.3)
        assert not (await limiter.aevaluate(CTX, ["per-user"], include_global=False)).fallback
    finally:
        await limiter.aclose()
        limiter.close()
    assert rec.breaker() == ["open", "half_open", "closed"]
    assert rec.fallback() == ["activated", "deactivated"]


def test_fail_closed_rules_do_not_use_the_fallback_on_real_redis(own_redis: RedisProcess) -> None:
    strict = Rule("strict", "5/minute", key="user", fail_mode="closed")
    rec = Recorder()
    limiter = limiter_for(own_redis, rec, fallback="memory")
    try:
        own_redis.stop()
        decision = limiter.evaluate(CTX, [strict], include_global=False)
    finally:
        limiter.close()
    assert not decision.allowed
    assert decision.reason is Reason.BACKEND_UNAVAILABLE
    assert not decision.fallback
    assert rec.fallback() == []


async def test_standalone_and_cluster_emit_the_same_events(
    redis_url: str, cluster_url: str
) -> None:
    shapes = {}
    for name, url, cluster in (("standalone", redis_url, False), ("cluster", cluster_url, True)):
        rec = Recorder()
        limiter = Limiter(RedisBackend(url, cluster=cluster), rules=[PER_USER], observer=rec)
        try:
            for _ in range(6):
                limiter.evaluate(CTX, ["per-user"], include_global=False)
            await limiter.aevaluate(CTX, ["per-user"], include_global=False)
        finally:
            await limiter.aclose()
            limiter.close()
        events = [e for e in rec.events if isinstance(e, DecisionEvent)]
        assert [e.backend for e in events] == ["redis-cluster" if cluster else "redis"] * 7
        shapes[name] = [(e.allowed, e.reason, e.source) for e in events]
        assert all(e.duration is not None and e.duration > 0 for e in events)
    assert shapes["standalone"] == shapes["cluster"]


def test_cluster_primary_crash_is_observable(disposable_cluster: RedisCluster) -> None:
    rec = Recorder()
    backend = RedisBackend(disposable_cluster.url, cluster=True, cooldown=0.2)
    limiter = Limiter(backend, rules=[PER_USER], observer=rec, fallback="memory")
    ctx = RequestContext(user_id="x")
    try:
        assert limiter.evaluate(ctx, ["per-user"], include_global=False).allowed
        key = limiter.key_for(PER_USER, Identity("user", "x"))
        slot_owner = disposable_cluster.node_for_slot(key_hash_slot(key))
        hash_tag = key[key.index("{") : key.index("}") + 1]
        with slot_owner.client() as primary:
            primary.set(f"{hash_tag}replicated", 1)
            assert primary.wait(1, 5000) >= 1, "the write did not replicate"
        slot_owner.stop()
        saw_fallback = False
        deadline = time.monotonic() + 30
        while True:
            decision = limiter.evaluate(ctx, ["per-user"], include_global=False)
            saw_fallback = saw_fallback or decision.fallback
            if not decision.fallback and not decision.degraded:
                break  # failover completed: Redis serves again
            assert time.monotonic() < deadline, "failover did not complete"
            time.sleep(0.1)
    finally:
        limiter.close()
    assert saw_fallback
    assert rec.breaker()[0] == "open"
    assert rec.breaker()[-1] == "closed"
    assert rec.fallback() == ["activated", "deactivated"]
    errors = [e for e in rec.events if isinstance(e, BackendUnavailableEvent)]
    assert errors
    assert {e.backend for e in errors} == {"redis-cluster"}
