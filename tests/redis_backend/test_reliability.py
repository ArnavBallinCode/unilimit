"""Reliability tests against real Redis processes: outages, restarts, timeouts, cooldown,
script cache loss, malformed state, auth failures, OOM, expiry, cancellation and
connection lifecycle. Each test owns its server, so faults never leak between tests.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
import redis

from tests._proxy import FaultProxy
from tests._servers import RedisProcess, close_client, server_binary
from unilimit import (
    BackendError,
    BackendUnavailable,
    Identity,
    Limiter,
    Reason,
    RequestContext,
    Rule,
)
from unilimit.algorithms import Algorithm, LimitSpec
from unilimit.backends.base import ConsumeOp
from unilimit.backends.redis import RedisBackend

SPEC = LimitSpec(Algorithm.FIXED_WINDOW, 5, 60_000_000, 5)
RULE = Rule("r", "5/minute", algorithm="fixed_window")


@pytest.fixture
def server() -> Iterator[RedisProcess]:
    s = RedisProcess(server_binary()).start()
    yield s
    s.destroy()


@pytest.fixture
def proxy(server: RedisProcess) -> Iterator[FaultProxy]:
    p = FaultProxy(server.port)
    yield p
    p.close()


@pytest.fixture(params=["sync", "async"])
def mode(request: pytest.FixtureRequest) -> str:
    return str(request.param)


_created: list[RedisBackend] = []


def Backend(*args: object, **kwargs: object) -> RedisBackend:
    """RedisBackend that is closed (sync and async halves) when the test ends."""
    backend = RedisBackend(*args, **kwargs)  # type: ignore[arg-type]
    _created.append(backend)
    return backend


@pytest.fixture(autouse=True)
async def _close_backends() -> AsyncIterator[None]:
    yield
    while _created:
        backend = _created.pop()
        backend.close()
        await backend.aclose()


async def consume(limiter: Limiter, mode: str, rule: Rule = RULE, ident: str = "u") -> Any:
    if mode == "sync":
        return limiter.consume(rule, ident)
    return await limiter.aconsume(rule, ident)


async def backend_consume(backend: RedisBackend, mode: str, ops: list[ConsumeOp]) -> Any:
    if mode == "sync":
        return backend.consume(ops)
    return await backend.aconsume(ops)


async def close(limiter: Limiter, mode: str) -> None:
    if mode == "sync":
        limiter.close()
    else:
        await limiter.aclose()


async def until_recovered(limiter: Limiter, mode: str, within: float = 10) -> None:
    deadline = time.monotonic() + within
    while (await consume(limiter, mode, ident="probe")).degraded:
        assert time.monotonic() < deadline, "backend did not recover"
        await asyncio.sleep(0.05)


# -- outages, restarts and cooldown ------------------------------------------------------


async def test_outage_fails_open_then_recovers_after_restart(
    server: RedisProcess, mode: str
) -> None:
    limiter = Limiter(Backend(server.url, cooldown=0.3))
    for _ in range(2):
        assert not (await consume(limiter, mode)).degraded
    server.stop()
    outage = await consume(limiter, mode)
    assert (outage.allowed, outage.degraded) == (True, True)
    server.start()  # same port, empty dataset, empty script cache
    await until_recovered(limiter, mode)
    result = await consume(limiter, mode)
    assert not result.degraded
    assert result.remaining == 4  # a fresh server: the earlier counts are gone
    await close(limiter, mode)


async def test_outage_fails_closed_when_configured(server: RedisProcess, mode: str) -> None:
    limiter = Limiter(Backend(server.url, cooldown=0), fail_mode="closed")
    server.stop()
    result = await consume(limiter, mode)
    assert (result.allowed, result.degraded) == (False, True)
    decision = (
        limiter.evaluate(RequestContext(peer="1.2.3.4"), [RULE])
        if mode == "sync"
        else await limiter.aevaluate(RequestContext(peer="1.2.3.4"), [RULE])
    )
    assert decision.reason is Reason.BACKEND_UNAVAILABLE
    await close(limiter, mode)


async def test_restart_with_stale_pooled_connection_recovers_without_failing_open(
    server: RedisProcess, mode: str
) -> None:
    """A restart leaves dead connections in the pool; the single retry on connection
    errors must recover on the very first request, not fail open."""
    limiter = Limiter(Backend(server.url))
    assert not (await consume(limiter, mode)).degraded
    server.restart()
    result = await consume(limiter, mode)
    assert not result.degraded
    assert result.remaining == 4
    await close(limiter, mode)


async def test_idle_connection_drop_is_transparent(proxy: FaultProxy, mode: str) -> None:
    limiter = Limiter(Backend(proxy.url))
    assert (await consume(limiter, mode)).remaining == 4
    proxy.reset()  # e.g. a load balancer silently dropping idle connections
    result = await consume(limiter, mode)
    assert (result.degraded, result.remaining) == (False, 3)
    await close(limiter, mode)


async def test_timeouts_fail_fast_and_cooldown_skips_the_network(
    proxy: FaultProxy, mode: str
) -> None:
    backend = Backend(proxy.url, socket_timeout=0.2, socket_connect_timeout=0.2, cooldown=0.5)
    limiter = Limiter(backend)
    assert not (await consume(limiter, mode)).degraded
    proxy.mode = "blackhole"

    started = time.monotonic()
    with pytest.raises(BackendUnavailable, match="Timeout"):
        await backend_consume(backend, mode, [ConsumeOp("k", SPEC)])
    assert 0.15 < time.monotonic() - started < 1.5

    started = time.monotonic()  # inside the cooldown: no network round trip at all
    for _ in range(20):
        with pytest.raises(BackendUnavailable, match="cooling down"):
            await backend_consume(backend, mode, [ConsumeOp("k", SPEC)])
    assert time.monotonic() - started < 0.1
    assert (await consume(limiter, mode)).degraded  # fail-open via the Limiter

    proxy.mode = "forward"
    proxy.reset()
    await asyncio.sleep(0.55)
    await until_recovered(limiter, mode)
    await close(limiter, mode)


async def test_after_cooldown_exactly_one_request_probes(proxy: FaultProxy) -> None:
    """Half-open breaker: when the cooldown expires, one request probes a still-dead
    Redis (paying the timeout) while concurrent requests keep failing fast."""
    backend = Backend(proxy.url, socket_timeout=0.3, cooldown=0.2)
    await backend.aconsume([ConsumeOp("k", SPEC)])
    proxy.mode = "blackhole"
    with pytest.raises(BackendUnavailable):
        await backend.aconsume([ConsumeOp("k", SPEC)])  # trips the breaker
    await asyncio.sleep(0.25)  # cooldown over

    async def timed() -> float:
        started = time.monotonic()
        with pytest.raises(BackendUnavailable):
            await backend.aconsume([ConsumeOp("k", SPEC)])
        return time.monotonic() - started

    durations = await asyncio.gather(*(timed() for _ in range(20)))
    assert sum(d > 0.2 for d in durations) == 1  # the single probe waited for the timeout
    assert sum(d < 0.05 for d in durations) == 19  # everyone else failed fast

    proxy.mode = "forward"
    proxy.reset()
    await asyncio.sleep(0.25)
    probe = await backend.aconsume([ConsumeOp("k", SPEC)])  # successful probe closes it
    assert probe.committed
    assert backend._down_until == 0.0
    results = await asyncio.gather(
        *(backend.aconsume([ConsumeOp(f"k{i}", SPEC)]) for i in range(20))
    )
    assert all(r.committed for r in results)


def probing(backend: RedisBackend) -> bool:
    return backend._probing


async def test_cancelled_probe_lets_the_next_request_probe(proxy: FaultProxy) -> None:
    backend = Backend(proxy.url, socket_timeout=5, cooldown=0.1)
    await backend.aconsume([ConsumeOp("k", SPEC)])
    proxy.mode = "blackhole"
    backend._trip(RuntimeError("simulated outage"))
    await asyncio.sleep(0.15)
    probe = asyncio.create_task(backend.aconsume([ConsumeOp("k", SPEC)]))
    await asyncio.sleep(0.05)
    assert probing(backend)
    probe.cancel()
    with pytest.raises(asyncio.CancelledError):
        await probe
    assert not probing(backend)  # released despite cancellation
    proxy.mode = "forward"
    proxy.reset()
    assert (await backend.aconsume([ConsumeOp("k", SPEC)])).committed
    assert backend._down_until == 0.0


async def test_probe_answered_with_a_data_error_closes_the_breaker(server: RedisProcess) -> None:
    """Any answer from Redis, even an error about one key, proves it is reachable."""
    backend = Backend(server.url, cooldown=0.1)
    server.client().set("corrupt", "garbage")
    backend._trip(RuntimeError("simulated outage"))
    await asyncio.sleep(0.15)
    with pytest.raises(BackendError):
        await backend.aconsume([ConsumeOp("corrupt", SPEC)])
    assert backend._down_until == 0.0


async def test_cooldown_can_be_disabled(server: RedisProcess, mode: str) -> None:
    backend = Backend(server.url, cooldown=0)
    server.stop()
    for _ in range(3):
        with pytest.raises(BackendUnavailable) as info:
            await backend_consume(backend, mode, [ConsumeOp("k", SPEC)])
        assert "cooling down" not in str(info.value)


# -- script cache ------------------------------------------------------------------------


async def test_script_flush_is_recovered_transparently(server: RedisProcess, mode: str) -> None:
    limiter = Limiter(Backend(server.url))
    assert (await consume(limiter, mode)).remaining == 4
    server.client().script_flush()
    assert (await consume(limiter, mode)).remaining == 3
    server.client().script_flush()
    peek = limiter.peek(RULE, "u") if mode == "sync" else await limiter.apeek(RULE, "u")
    assert peek.remaining == 3
    await close(limiter, mode)


# -- unusable data -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("algorithm", "raw"),
    [
        ("fixed_window", "garbage"),
        ("fixed_window", ""),
        ("fixed_window", "fw:abc:1"),
        ("fixed_window", "fw:1"),
        ("fixed_window", "fw:1:2:3"),
        ("fixed_window", "fw:-1:2"),
        ("fixed_window", "fw:99999999999999999999:1"),  # beyond exact double precision
        ("sliding_window", "sw:1:2"),
        ("sliding_window", "sw:1:2:x"),
        ("token_bucket", "tb:nan:5"),
        ("token_bucket", "tb:-1:5"),
        ("token_bucket", "tb:inf:5"),
        ("token_bucket", "tb:1e400:5"),
        ("token_bucket", "tb::5"),
        ("token_bucket", "tb:1.5"),
    ],
)
async def test_malformed_state_raises_and_is_not_overwritten(
    server: RedisProcess, mode: str, algorithm: str, raw: str
) -> None:
    rule = Rule("r", "5/minute", algorithm=algorithm)  # type: ignore[arg-type]
    limiter = Limiter(Backend(server.url))
    key = limiter.key_for(rule, "u")
    server.client().set(key, raw)
    with pytest.raises(BackendError, match="unreadable rate-limit state") as info:
        await consume(limiter, mode, rule)
    assert not isinstance(info.value, BackendUnavailable)  # never subject to fail-open
    assert server.client().get(key) == raw.encode()
    if mode == "sync":
        limiter.reset(rule, "u")
    else:
        await limiter.areset(rule, "u")
    assert (await consume(limiter, mode, rule)).remaining == 4
    await close(limiter, mode)


async def test_algorithm_mismatch_and_wrong_type(server: RedisProcess, mode: str) -> None:
    limiter = Limiter(Backend(server.url))
    key = limiter.key_for(RULE, "u")
    server.client().set(key, "tb:1.5:100")
    with pytest.raises(BackendError, match="written by token_bucket, not fixed_window"):
        await consume(limiter, mode)
    server.client().delete(key)
    server.client().hset(key, "x", "1")
    with pytest.raises(BackendError, match="WRONGTYPE") as info:
        await consume(limiter, mode)
    assert not isinstance(info.value, BackendUnavailable)
    await close(limiter, mode)


async def test_one_corrupt_key_aborts_the_batch_without_writes(
    server: RedisProcess, mode: str
) -> None:
    backend = Backend(server.url)
    server.client().set("bad", "nonsense")
    with pytest.raises(BackendError):
        await backend_consume(backend, mode, [ConsumeOp("good", SPEC), ConsumeOp("bad", SPEC)])
    assert server.client().get("good") is None


# -- server-side refusals ----------------------------------------------------------------


async def test_authentication_failure_is_an_error_not_an_outage(mode: str) -> None:
    secured = RedisProcess(server_binary(), extra=("--requirepass", "correct-horse")).start()
    try:
        url = f"redis://:wrong@127.0.0.1:{secured.port}/0"
        limiter = Limiter(Backend(url))
        with pytest.raises(BackendError, match="rejected the connection") as info:
            await consume(limiter, mode)
        assert not isinstance(info.value, BackendUnavailable)  # must not fail open
        ok = Limiter(Backend(f"redis://:correct-horse@127.0.0.1:{secured.port}/0"))
        assert (await consume(ok, mode)).remaining == 4
        await close(ok, mode)
    finally:
        secured.destroy()


async def test_out_of_memory_is_treated_as_unavailable(mode: str) -> None:
    small = RedisProcess(
        server_binary(), extra=("--maxmemory", "2mb", "--maxmemory-policy", "noeviction")
    ).start()
    try:
        client = small.client()
        with contextlib.suppress(redis.exceptions.OutOfMemoryError):
            for i in range(10_000):
                client.set(f"filler:{i}", "x" * 1_000)
        limiter = Limiter(Backend(small.url, cooldown=0))
        result = await consume(limiter, mode)
        assert (result.allowed, result.degraded) == (True, True)
        closed = Limiter(Backend(small.url, cooldown=0), fail_mode="closed")
        assert not (await consume(closed, mode)).allowed
    finally:
        small.destroy()


# -- expiry with the server clock --------------------------------------------------------


@pytest.mark.parametrize("algorithm", ["fixed_window", "sliding_window", "token_bucket"])
async def test_keys_expire_with_server_time(
    server: RedisProcess, mode: str, algorithm: str
) -> None:
    rule = Rule("short", limit=1, window=0.2, algorithm=algorithm)  # type: ignore[arg-type]
    limiter = Limiter(Backend(server.url))
    assert (await consume(limiter, mode, rule)).allowed
    assert not (await consume(limiter, mode, rule)).allowed
    key = limiter.key_for(rule, "u")
    assert 0 < server.client().pttl(key) <= 402
    await asyncio.sleep(0.45)  # longer than any TTL for a 200 ms window (sliding: 2W)
    assert server.client().exists(key) == 0
    assert (await consume(limiter, mode, rule)).allowed
    await close(limiter, mode)


# -- async lifecycle and cancellation ----------------------------------------------------


async def test_async_path_never_uses_the_sync_client(server: RedisProcess) -> None:
    backend = Backend(server.url)
    limiter = Limiter(backend)
    await limiter.aconsume(RULE, "u")
    await limiter.apeek(RULE, "u")
    await limiter.aevaluate(RequestContext(peer="1.2.3.4"), [RULE])
    await limiter.areset(RULE, "u")
    assert backend._client is None  # no blocking client was ever created
    await limiter.aclose()


def test_one_backend_serves_several_event_loops(server: RedisProcess) -> None:
    backend = Backend(server.url)
    limiter = Limiter(backend)

    async def use() -> int:
        result = await limiter.aconsume(RULE, "loops")
        await limiter.aclose()
        return result.remaining

    # Each asyncio.run() creates and closes a new loop; clients must not leak across.
    assert [asyncio.run(use()) for _ in range(3)] == [4, 3, 2]
    assert backend._async == {}


async def test_cancellation_mid_request(server: RedisProcess) -> None:
    backend = Backend(server.url, socket_timeout=5)
    limiter = Limiter(backend)
    assert (await limiter.aconsume(RULE, "c")).remaining == 4
    admin = server.client()
    admin.execute_command("CLIENT", "PAUSE", "400", "WRITE")
    task = asyncio.create_task(limiter.aconsume(RULE, "c"))
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.4)  # pause over
    # The backend stays usable, and the cancelled request was either fully applied
    # (the script is atomic) or not at all.
    result = await limiter.aconsume(RULE, "c")
    assert not result.degraded
    assert result.remaining in (2, 3)
    await limiter.aclose()


async def test_concurrent_cancellations_never_over_admit(server: RedisProcess) -> None:
    limiter = Limiter(Backend(server.url))
    rule = Rule("cancel", "50/minute", algorithm="token_bucket")
    tasks = [asyncio.create_task(limiter.aconsume(rule, "x")) for _ in range(200)]
    await asyncio.sleep(0)
    for t in tasks[::3]:
        t.cancel()
    done = await asyncio.gather(*tasks, return_exceptions=True)
    results = [r for r in done if not isinstance(r, BaseException)]
    assert not any(r.degraded for r in results)
    admitted = sum(r.allowed for r in results)
    assert admitted <= 50
    left = await limiter.apeek(rule, "x")
    assert admitted <= 50 - left.remaining  # every admission consumed quota


# -- connection pool and bursts ------------------------------------------------------------


async def test_async_burst_far_beyond_pool_size_queues_instead_of_failing_open(
    server: RedisProcess,
) -> None:
    """Regression: redis-py's default pool raises past 100 concurrent commands. A burst
    used to fail open for those requests *and* start the outage cooldown, which would
    switch rate limiting off. Excess calls must queue for a connection instead."""
    backend = Backend(server.url, max_connections=4)
    limiter = Limiter(backend)
    rule = Rule("burst", "50/minute", algorithm="token_bucket")
    results = await asyncio.gather(*(limiter.aconsume(rule, "x") for _ in range(400)))
    assert not any(r.degraded for r in results)
    assert sum(r.allowed for r in results) == 50
    assert backend._down_until == 0.0


def test_threaded_burst_far_beyond_pool_size_queues(server: RedisProcess) -> None:
    from concurrent.futures import ThreadPoolExecutor

    backend = Backend(server.url, max_connections=2)
    limiter = Limiter(backend)
    rule = Rule("burst", "30/minute", algorithm="sliding_window")
    with ThreadPoolExecutor(32) as pool:
        results = list(pool.map(lambda _: limiter.consume(rule, "x"), range(300)))
    assert not any(r.degraded for r in results)
    assert sum(r.allowed for r in results) == 30
    assert backend._down_until == 0.0


async def test_pool_timeout_is_overload_not_an_outage(server: RedisProcess) -> None:
    backend = Backend(server.url, max_connections=1, pool_timeout=0.1, socket_timeout=2)
    limiter = Limiter(backend)
    await limiter.aconsume(RULE, "warm")
    server.client().execute_command("CLIENT", "PAUSE", "500", "WRITE")
    slow = asyncio.create_task(limiter.aconsume(RULE, "slow"))  # holds the only slot
    await asyncio.sleep(0.05)
    with pytest.raises(BackendUnavailable, match="no Redis connection became available"):
        await backend.aconsume([ConsumeOp("other", SPEC)])
    assert backend._down_until == 0.0  # Redis is healthy: no cooldown started
    assert not (await slow).degraded
    assert not (await limiter.aconsume(RULE, "after")).degraded


def test_close_releases_and_backend_can_reconnect(server: RedisProcess) -> None:
    backend = Backend(server.url)
    backend.consume([ConsumeOp("k", SPEC)])
    backend.close()
    assert backend._client is None
    backend.close()  # idempotent
    assert backend.consume([ConsumeOp("k", SPEC)]).results[0].remaining == 3


def test_user_supplied_clients_are_not_closed(server: RedisProcess) -> None:
    client = redis.Redis(port=server.port)
    backend = Backend(client=client)
    backend.consume([ConsumeOp("k", SPEC)])
    backend.close()
    assert client.ping()  # still usable: the caller owns it
    with pytest.raises(Exception, match="no async client"):
        asyncio.run(backend.aconsume([ConsumeOp("k", SPEC)]))
    client.close()


def test_configuration_errors() -> None:
    from unilimit import ConfigurationError

    with pytest.raises(ConfigurationError):
        RedisBackend()
    with pytest.raises(ConfigurationError):
        RedisBackend("redis://x", client=redis.Redis())
    with pytest.raises(ConfigurationError):
        RedisBackend("redis://x", cooldown=-1)
    with pytest.raises(ConfigurationError):
        RedisBackend(client=redis.Redis(), cluster=True)
    limiter = Limiter.from_url("redis://localhost:1/0", cluster=False, cooldown=0, namespace="n")
    assert isinstance(limiter.backend, RedisBackend)
    assert limiter.namespace == "n"


def test_identity_secret_keys_are_hashed_in_redis(server: RedisProcess) -> None:
    limiter = Limiter(Backend(server.url), identity_secret="s3cret")
    limiter.consume(RULE, Identity("api_key", "sk_live_123"))
    keys = [k.decode() if isinstance(k, bytes) else k for k in server.client().keys("*")]
    assert keys
    assert all("sk_live_123" not in k for k in keys)


async def test_user_supplied_async_client_is_used_and_not_closed(server: RedisProcess) -> None:
    import redis.asyncio

    client = redis.asyncio.Redis(port=server.port)
    backend = RedisBackend(async_client=client)
    assert not backend.cluster
    result = await backend.aconsume([ConsumeOp("k", SPEC)])
    assert result.committed
    await backend.aclose()
    assert await client.ping()  # the caller owns it
    with pytest.raises(Exception, match="no sync client"):
        backend.consume([ConsumeOp("k", SPEC)])
    await client.aclose()


def test_supplied_cluster_client_is_detected(redis_cluster: object, cluster_url: str) -> None:
    import redis.cluster

    client = redis.cluster.RedisCluster.from_url(cluster_url)
    backend = RedisBackend(client=client)
    assert backend.cluster
    assert backend.consume([ConsumeOp("a", SPEC), ConsumeOp("b", SPEC)]).committed
    close_client(client)


def test_sync_gate_timeout_is_overload_not_an_outage(server: RedisProcess) -> None:
    import threading

    backend = Backend(server.url, max_connections=1, pool_timeout=0.1, socket_timeout=2)
    backend.consume([ConsumeOp("warm", SPEC)])
    server.client().execute_command("CLIENT", "PAUSE", "400", "WRITE")
    holder = threading.Thread(target=backend.consume, args=([ConsumeOp("slow", SPEC)],))
    holder.start()
    time.sleep(0.05)
    with pytest.raises(BackendUnavailable, match="no Redis connection became available"):
        backend.consume([ConsumeOp("other", SPEC)])
    assert backend._down_until == 0.0
    holder.join()


async def test_probe_answered_with_wrongtype_closes_the_breaker(server: RedisProcess) -> None:
    backend = Backend(server.url, cooldown=0.1)
    server.client().hset("hash", "f", "1")
    backend._trip(RuntimeError("simulated outage"))
    await asyncio.sleep(0.15)
    with pytest.raises(BackendError, match="WRONGTYPE"):
        await backend.aconsume([ConsumeOp("hash", SPEC)])
    assert backend._down_until == 0.0


def test_backend_option_validation() -> None:
    from unilimit import ConfigurationError

    for kwargs in ({"max_connections": 0}, {"max_connections": True}, {"pool_timeout": -1}):
        with pytest.raises(ConfigurationError):
            RedisBackend("redis://x", **kwargs)
    assert isinstance(RedisBackend.from_url("redis://x"), RedisBackend)
