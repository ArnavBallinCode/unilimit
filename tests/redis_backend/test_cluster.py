"""Redis Cluster tests against a real cluster (3 primaries, 3 replicas)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
import redis
import redis.cluster

from tests._servers import RedisCluster, server_binary
from unilimit import Decision, Identity, Limiter, Reason, RequestContext, Rule
from unilimit.backends.base import ConsumeOp
from unilimit.backends.redis import RedisBackend
from unilimit.backends.redis._protocol import MODE_CONSUME, plan_groups
from unilimit.core.keys import key_hash_slot

PER_USER = Rule("per-user", "5/minute", key="user", algorithm="fixed_window")
PER_IP = Rule("per-ip", "5/minute", key="ip", algorithm="fixed_window")
CTX = RequestContext(peer="203.0.113.7", user_id="alice")

_created: list[RedisBackend] = []


@pytest.fixture(autouse=True)
async def _close_backends() -> AsyncIterator[None]:
    yield
    while _created:
        backend = _created.pop()
        backend.close()
        await backend.aclose()


def make_limiter(url: str, **kwargs: Any) -> Limiter:
    backend = RedisBackend(
        url, cluster=True, **{k: kwargs.pop(k) for k in ("cooldown",) if k in kwargs}
    )
    _created.append(backend)
    return Limiter(backend, **kwargs)


@pytest.fixture(params=["sync", "async"])
def mode(request: pytest.FixtureRequest) -> str:
    return str(request.param)


async def evaluate(
    limiter: Limiter, mode: str, rules: list[Rule], ctx: RequestContext = CTX
) -> Decision:
    if mode == "sync":
        return limiter.evaluate(ctx, rules)
    return await limiter.aevaluate(ctx, rules)


def remaining(limiter: Limiter, rule: Rule, ident: Identity) -> int:
    return limiter.peek(rule, ident).remaining


# -- routing -----------------------------------------------------------------------------


def test_cluster_is_real_and_slots_match_the_server(
    redis_cluster: RedisCluster, cluster_url: str
) -> None:
    primaries = redis_cluster.primary_clients()
    assert len(primaries) == 3
    assert {c.cluster("info")["cluster_state"] for c in primaries} == {"ok"}
    limiter = make_limiter(cluster_url)
    node = redis_cluster.nodes[0].client()
    for i in range(300):
        for rule in (PER_USER, PER_IP, Rule("GET /x/{id}", "1/s")):
            key = limiter.key_for(rule, Identity("user", f"u{i}"))
            assert node.cluster("keyslot", key) == key_hash_slot(key)


def test_state_is_stored_on_the_owning_primary_only(
    redis_cluster: RedisCluster, cluster_url: str
) -> None:
    limiter = make_limiter(cluster_url)
    owners = set()
    for i in range(40):
        ident = Identity("user", f"user-{i}")
        limiter.consume(PER_USER, ident)
        key = limiter.key_for(PER_USER, ident)
        owner = redis_cluster.node_for_slot(key_hash_slot(key))
        owners.add(owner.port)
        holders = [
            n.port
            for n in redis_cluster.nodes
            if n.client().info("replication")["role"] == "master" and _owns(n.client(), key)
        ]
        assert holders == [owner.port]
    assert len(owners) == 3  # identities spread over every primary


def _owns(client: redis.Redis, key: str) -> bool:
    try:
        return client.execute_command("GET", key) is not None
    except redis.exceptions.MovedError:  # non-owners redirect
        return False


def test_same_identity_rules_form_a_single_atomic_group(cluster_url: str) -> None:
    limiter = make_limiter(cluster_url)
    ident = Identity("user", "alice")
    rules = [
        PER_USER,
        Rule("daily", "100/day", key="user"),
        Rule("tb", "9/s", key="user", algorithm="token_bucket"),
    ]
    keys = [limiter.key_for(r, ident) for r in rules]
    ops = [ConsumeOp(k, r.spec) for k, r in zip(keys, rules, strict=True)]
    assert len(plan_groups(ops, cluster=True)) == 1


async def test_same_identity_rejection_consumes_nothing(cluster_url: str, mode: str) -> None:
    limiter = make_limiter(cluster_url)
    burst = Rule("burst", "2/minute", key="user", algorithm="fixed_window")
    daily = Rule("daily", "10/day", key="user", algorithm="fixed_window")
    for _ in range(2):
        assert (await evaluate(limiter, mode, [burst, daily])).allowed
    for _ in range(10):
        decision = await evaluate(limiter, mode, [burst, daily])
        assert not decision.allowed
        assert not decision.partial_commit
    assert remaining(limiter, daily, Identity("user", "alice")) == 8


# -- cross-identity (multi-slot) batches -------------------------------------------------


def _distinct_slots(limiter: Limiter) -> None:
    a = key_hash_slot(limiter.key_for(PER_USER, Identity("user", "alice")))
    b = key_hash_slot(limiter.key_for(PER_IP, Identity("ip", "203.0.113.7")))
    assert a != b


async def test_cross_identity_rejection_in_phase_one_consumes_nothing(
    cluster_url: str, mode: str
) -> None:
    limiter = make_limiter(cluster_url)
    _distinct_slots(limiter)
    for _ in range(5):
        assert (await evaluate(limiter, mode, [PER_USER, PER_IP])).allowed
    other_ip = RequestContext(peer="198.51.100.1", user_id="alice")
    for _ in range(10):
        decision = await evaluate(limiter, mode, [PER_USER, PER_IP], other_ip)
        assert not decision.allowed
        assert not decision.partial_commit
    assert remaining(limiter, PER_IP, Identity("ip", "198.51.100.1")) == 5  # untouched


def _inject_between_phases(
    backend: RedisBackend, action: Any, monkeypatch: pytest.MonkeyPatch
) -> list[str]:
    """Run ``action`` once, right before the first commit call (after phase 1)."""
    calls: list[str] = []
    original_call, original_acall = backend._call, backend._acall

    def hook(mode: str) -> None:
        if mode == MODE_CONSUME and "injected" not in calls:
            calls.append("injected")
            action()
        calls.append(mode)

    def call(client: Any, script: Any, ops: Any, group: Any, mode: str, now: Any) -> Any:
        hook(mode)
        return original_call(client, script, ops, group, mode, now)

    async def acall(entry: Any, ops: Any, group: Any, mode: str, now: Any) -> Any:
        hook(mode)
        return await original_acall(entry, ops, group, mode, now)

    monkeypatch.setattr(backend, "_call", call)
    monkeypatch.setattr(backend, "_acall", acall)
    return calls


async def test_commit_reevaluates_and_reports_partial_commit(
    cluster_url: str, mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A concurrent request exhausts the IP quota after phase 1 saw room. The IP group's
    commit must re-evaluate current state and reject; the user group, committed first,
    stays consumed; the request is not admitted; and partial_commit is reported."""
    limiter = make_limiter(cluster_url)
    _distinct_slots(limiter)
    competitor = make_limiter(cluster_url)

    def exhaust_ip() -> None:
        for _ in range(5):
            assert competitor.consume(PER_IP, Identity("ip", "203.0.113.7")).allowed

    backend = limiter.backend
    assert isinstance(backend, RedisBackend)
    calls = _inject_between_phases(backend, exhaust_ip, monkeypatch)
    decision = await evaluate(limiter, mode, [PER_USER, PER_IP])

    assert calls[:3] == ["p", "p", "injected"]  # phase 1 saw room for both
    assert not decision.allowed
    assert decision.reason is Reason.RATE_LIMITED
    assert decision.partial_commit
    assert decision.binding is not None
    assert decision.binding.rule is PER_IP
    assert remaining(competitor, PER_USER, Identity("user", "alice")) == 4  # consumed
    assert remaining(competitor, PER_IP, Identity("ip", "203.0.113.7")) == 0  # by competitor only


async def test_failure_after_a_committed_group_reports_partial_commit(
    cluster_url: str, mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    limiter = make_limiter(cluster_url, fail_mode="closed", cooldown=0)
    backend = limiter.backend
    assert isinstance(backend, RedisBackend)
    original_call, original_acall = backend._call, backend._acall
    commits = []

    def maybe_fail(mode_: str) -> None:
        if mode_ == MODE_CONSUME:
            commits.append(1)
            if len(commits) == 2:
                raise redis.exceptions.ConnectionError("node went away")

    def call(client: Any, script: Any, ops: Any, group: Any, mode_: str, now: Any) -> Any:
        maybe_fail(mode_)
        return original_call(client, script, ops, group, mode_, now)

    async def acall(entry: Any, ops: Any, group: Any, mode_: str, now: Any) -> Any:
        maybe_fail(mode_)
        return await original_acall(entry, ops, group, mode_, now)

    monkeypatch.setattr(backend, "_call", call)
    monkeypatch.setattr(backend, "_acall", acall)
    decision = await evaluate(limiter, mode, [PER_USER, PER_IP])
    assert (decision.allowed, decision.reason) == (False, Reason.BACKEND_UNAVAILABLE)
    assert decision.partial_commit
    assert decision.degraded


async def test_shadow_only_groups_commit_last(cluster_url: str, mode: str) -> None:
    limiter = make_limiter(cluster_url)
    shadow = Rule("shadow-ip", "1/minute", key="ip", action="log")
    for i in range(3):
        decision = await evaluate(limiter, mode, [shadow, PER_USER])
        assert decision.allowed
        assert len(decision.violations) == (0 if i == 0 else 1)


async def test_script_flush_on_every_node(
    redis_cluster: RedisCluster, cluster_url: str, mode: str
) -> None:
    limiter = make_limiter(cluster_url)
    assert (await evaluate(limiter, mode, [PER_USER, PER_IP])).allowed
    for client in redis_cluster.primary_clients():
        client.script_flush()
    decision = await evaluate(limiter, mode, [PER_USER, PER_IP])
    assert decision.allowed
    assert not decision.degraded
    assert [r.remaining for r in decision.results] == [3, 3]


# -- node failure and failover (separate, disposable cluster) ----------------------------


@pytest.fixture
def disposable_cluster() -> Iterator[RedisCluster]:
    cluster = RedisCluster(server_binary())
    try:
        cluster.start()
        yield cluster
    finally:
        cluster.destroy()


@pytest.mark.parametrize("fail_mode", ["open", "closed"])
async def test_primary_crash_and_failover(
    disposable_cluster: RedisCluster, mode: str, fail_mode: str
) -> None:
    limiter = make_limiter(disposable_cluster.url, fail_mode=fail_mode, cooldown=0.2)
    try:
        ident = Identity("user", "failover")
        key = limiter.key_for(PER_USER, ident)
        ctx = RequestContext(user_id="failover")
        assert (await evaluate(limiter, mode, [PER_USER], ctx)).allowed

        victim = disposable_cluster.node_for_slot(key_hash_slot(key))
        # Replication is asynchronous, and a fixed sleep lost the race on Valkey 8.0.2 and
        # Redis 7.2.7 in CI. WAIT only covers writes made on the same connection (on a fresh
        # one it returns at once), so write a marker into the same slot, then WAIT: the
        # replication stream is ordered, so the replica has the earlier consume as well.
        hash_tag = key[key.index("{") : key.index("}") + 1]
        with victim.client(socket_timeout=15.0) as primary:
            primary.set(f"{hash_tag}replicated", 1)
            deadline_wait = time.monotonic() + 15.0
            replicated = 0
            while time.monotonic() < deadline_wait:
                replicated = primary.wait(1, 1000)
                if replicated >= 1:
                    break
            assert replicated >= 1, "the write did not replicate"
        victim.stop()  # crash the primary that owns this identity's slot

        saw_degraded = False
        deadline = time.monotonic() + 30
        while True:
            decision = await evaluate(limiter, mode, [PER_USER], ctx)
            if not decision.degraded:
                break
            saw_degraded = True
            assert decision.allowed is (fail_mode == "open")
            assert time.monotonic() < deadline, "failover did not complete"
            await asyncio.sleep(0.1)

        assert saw_degraded  # the outage was observed and handled per fail_mode
        # Nodes learn the new topology asynchronously: the client may already have been
        # redirected to the promoted replica before the node we ask has updated its view.
        deadline = time.monotonic() + 10
        while disposable_cluster.node_for_slot(key_hash_slot(key)).port == victim.port:
            assert time.monotonic() < deadline, "no replica was promoted"
            await asyncio.sleep(0.1)
        assert decision.allowed
        # The promoted replica had the replicated count (1 before + 1 now).
        assert decision.results[0].remaining == 3
    finally:
        limiter.close()
        await limiter.aclose()


async def test_cold_async_cluster_client_survives_a_concurrent_burst(cluster_url: str) -> None:
    """Regression: a cold redis.asyncio RedisCluster used by many tasks at once raced
    its own topology discovery, and ~15% of a burst failed open. The backend now
    initialises the topology once, under a lock, before concurrent use."""
    for _ in range(3):
        limiter = make_limiter(cluster_url)
        rule = Rule("cold", "1000/day", algorithm="fixed_window")
        results = await asyncio.gather(*(limiter.aconsume(rule, "x") for _ in range(150)))
        assert not any(r.degraded for r in results)
        await limiter.aclose()


def test_sync_cluster_close_releases_every_node_connection(cluster_url: str) -> None:
    """Regression: redis-py's RedisCluster.close() leaves node pools connected (one
    socket per used node). The backend's close() must actually close them, not leave
    them to the garbage collector."""
    backend = RedisBackend(cluster_url, cluster=True)
    ops = [ConsumeOp(f"leak-{i}", PER_USER.spec) for i in range(30)]  # spans all primaries
    backend.consume(ops)
    client, _ = backend._sync()  # keep it reachable, as a long-lived app would

    def open_sockets() -> int:
        return sum(
            1
            for node in client.get_nodes()
            if node.redis_connection is not None
            for conn in node.redis_connection.connection_pool._available_connections
            if conn._sock is not None
        )

    assert open_sockets() >= 3
    backend.close()
    assert open_sockets() == 0
