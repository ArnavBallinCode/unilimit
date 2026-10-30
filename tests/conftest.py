from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from hypothesis import HealthCheck, settings

from tests._servers import RedisCluster, RedisProcess, server_binary
from unilimit import MemoryBackend
from unilimit.testing import ManualClock

# Deterministic by default (fixed seed); `HYPOTHESIS_PROFILE=explore` for deep fuzzing.
settings.register_profile(
    "default",
    derandomize=True,
    max_examples=300,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.register_profile("explore", max_examples=5_000, deadline=None)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "default"))


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock()


@pytest.fixture
def backend(clock: ManualClock) -> MemoryBackend:
    return MemoryBackend(clock=clock)


def pytest_report_header() -> str:
    binary = os.environ.get("UNILIMIT_REDIS_SERVER") or "redis-server (PATH)"
    return f"unilimit redis binary: {binary}"


# -- real Redis servers (started lazily, once per session) -------------------------------


@pytest.fixture(scope="session")
def redis_server() -> Iterator[RedisProcess]:
    server = RedisProcess(server_binary()).start()
    yield server
    server.destroy()


@pytest.fixture
def redis_url(redis_server: RedisProcess) -> str:
    redis_server.client().flushall()
    return redis_server.url


@pytest.fixture(scope="session")
def redis_cluster() -> Iterator[RedisCluster]:
    cluster = RedisCluster(server_binary()).start()
    yield cluster
    cluster.destroy()


@pytest.fixture
def cluster_url(redis_cluster: RedisCluster) -> str:
    redis_cluster.flushall()
    return redis_cluster.url
