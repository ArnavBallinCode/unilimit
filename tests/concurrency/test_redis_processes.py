"""Multiple independent OS processes sharing one Redis (standalone and Cluster).

Each worker is a separately spawned interpreter with its own Limiter, connection pool
and (for async workers) event loop. A barrier releases them together. Assertions are on
exact counts: no over-admission and, for atomic batches, no over-consumption.
"""

from __future__ import annotations

import multiprocessing as mp
from typing import Any

import pytest

from unilimit import Identity, Limiter, Rule

from . import _workers

PROCESSES = 8
CTX = mp.get_context("spawn")


def run_workers(target: Any, args_for: Any) -> list[dict[str, int]]:
    barrier = CTX.Barrier(PROCESSES)
    out = CTX.Queue()
    procs = [
        CTX.Process(target=target, args=(*args_for(i), barrier, out), daemon=True)
        for i in range(PROCESSES)
    ]
    try:
        for p in procs:
            p.start()
        results = [out.get(timeout=180) for _ in procs]
        errors = [r["error"] for r in results if "error" in r]
        assert not errors, errors[0]
        for p in procs:
            p.join(30)
            assert p.exitcode == 0
        return results
    finally:
        for p in procs:
            if p.is_alive():
                p.terminate()


@pytest.fixture(params=["redis", "cluster"])
def target(request: pytest.FixtureRequest) -> tuple[str, bool]:
    if request.param == "redis":
        return request.getfixturevalue("redis_url"), False
    return request.getfixturevalue("cluster_url"), True


@pytest.mark.parametrize("algorithm", ["fixed_window", "sliding_window", "token_bucket"])
@pytest.mark.parametrize("clock", ["frozen", "server"])
def test_processes_never_over_admit_one_identity(
    target: tuple[str, bool], algorithm: str, clock: str
) -> None:
    """8 processes x 150 attempts (half sync, half async) against a limit of 500."""
    url, cluster = target
    # With the server clock, a 1-day window refills < 0.1 token during the test.
    rule = Rule(f"proc-{algorithm}", "500/day", algorithm=algorithm)  # type: ignore[arg-type]
    results = run_workers(
        _workers.single_identity,
        lambda i: (url, cluster, clock == "frozen", rule, 150, i % 2 == 1),
    )
    assert sum(r["degraded"] for r in results) == 0
    assert sum(r["admitted"] for r in results) == 500


def test_processes_multi_rule_standalone_is_fully_atomic(redis_url: str) -> None:
    """Standalone Redis: one script per request, so per-user consumption equals the
    number of admitted requests exactly. Rejected requests consume nothing."""
    users = 16
    results = run_workers(
        _workers.multi_rule,
        lambda i: (redis_url, False, True, users, 150, i % 2 == 1, i),
    )
    admitted = sum(r["admitted"] for r in results)
    assert sum(r["degraded"] for r in results) == 0
    assert sum(r["partial"] for r in results) == 0
    assert admitted == 300
    consumed = _per_user_consumed(redis_url, False, users)
    print(f"\nstandalone multi-rule: admitted={admitted} per_user_consumed={consumed}")
    assert consumed == admitted  # exactly: no over-consumption


def test_processes_multi_rule_cluster_cross_slot(cluster_url: str) -> None:
    """Cluster, per-user and global keys in different slots (two-phase protocol).

    The global group commits last, so admissions equal global consumption exactly.
    Per-user quota may be over-consumed only by requests reported as partial commits:
    per-user consumption must equal admitted + partial commits exactly.
    """
    users = 16
    results = run_workers(
        _workers.multi_rule,
        lambda i: (cluster_url, True, True, users, 150, i % 2 == 1, i),
    )
    admitted = sum(r["admitted"] for r in results)
    partial = sum(r["partial"] for r in results)
    consumed = _per_user_consumed(cluster_url, True, users)
    print(
        f"\ncluster cross-slot: admitted={admitted} partial_commits={partial} "
        f"per_user_consumed={consumed}"
    )
    assert sum(r["degraded"] for r in results) == 0
    assert admitted == 300
    assert consumed == admitted + partial


def _per_user_consumed(url: str, cluster: bool, users: int) -> int:
    limiter = _workers.build(url, cluster, frozen=True)
    rule = _workers.PER_USER
    try:
        return sum(
            rule.capacity - limiter.peek(rule, Identity("user", f"u{u}")).remaining
            for u in range(users)
        )
    finally:
        limiter.close()


def test_limiter_type() -> None:
    assert isinstance(_workers.build("memory://", False, True), Limiter)
