from __future__ import annotations

import gc
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


@pytest.fixture(autouse=True)
def _fresh_log_throttle() -> Iterator[None]:
    """Failure logs are throttled per process; every test starts with a clean slate."""
    from unilimit._logging import DEFAULT_INTERVAL, throttled

    throttled.reset()
    throttled.configure(DEFAULT_INTERVAL)
    yield
    throttled.reset()
    throttled.configure(DEFAULT_INTERVAL)


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


# -- deterministic leak detection ------------------------------------------------------

_LEAK_CHECKED_DIRS = (
    "redis_backend",
    "conformance",
    "concurrency",
    "fastapi_integration",
    "django_integration",
)


@pytest.fixture
def _collect_garbage() -> Iterator[None]:
    """Run the cyclic GC after the test, so an unclosed socket or connection raises its
    ResourceWarning (an error under filterwarnings=error) in the test that leaked it,
    instead of at an unpredictable later point."""
    yield
    gc.collect()


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if any(part in _LEAK_CHECKED_DIRS for part in item.path.parts):
            item.add_marker(pytest.mark.usefixtures("_collect_garbage"))
