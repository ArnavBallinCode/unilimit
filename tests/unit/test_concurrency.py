"""Unit tests for ConcurrencyLimit, LeaseDecision, and MemoryBackend leases."""

from __future__ import annotations

import pytest

from unilimit import ConcurrencyLimit, Identity, Limiter, MemoryBackend
from unilimit.core.errors import ConfigurationError
from unilimit.testing import ManualClock


def test_concurrency_limit_validation() -> None:
    limit = ConcurrencyLimit("ws-conns", max_concurrent=5, lease_ttl=10.0, heartbeat_interval=3.0)
    assert limit.name == "ws-conns"
    assert limit.max_concurrent == 5
    assert limit.lease_ttl == 10.0
    assert limit.effective_heartbeat_interval == 3.0

    # Default heartbeat interval is lease_ttl / 2
    limit_default = ConcurrencyLimit("ws-conns", max_concurrent=5, lease_ttl=10.0)
    assert limit_default.effective_heartbeat_interval == 5.0

    with pytest.raises(ConfigurationError, match="name must be a non-empty string"):
        ConcurrencyLimit("", max_concurrent=5)

    with pytest.raises(ConfigurationError, match="max_concurrent must be a positive integer"):
        ConcurrencyLimit("test", max_concurrent=0)

    with pytest.raises(ConfigurationError, match="lease_ttl must be a positive number"):
        ConcurrencyLimit("test", max_concurrent=5, lease_ttl=0)

    with pytest.raises(ConfigurationError, match="heartbeat_interval must be positive"):
        ConcurrencyLimit("test", max_concurrent=5, lease_ttl=10.0, heartbeat_interval=15.0)


def test_memory_backend_leases() -> None:
    clock = ManualClock()
    backend = MemoryBackend(clock=clock)
    limiter = Limiter(backend)
    limit = ConcurrencyLimit("test-conn", max_concurrent=2, lease_ttl=10.0)
    user = Identity("user", "alice")

    # Acquire 1st connection
    d1 = limiter.acquire_lease(limit, user, lease_id="conn-1")
    assert d1.allowed is True
    assert d1.current == 1
    assert d1.remaining == 1
    assert d1.retry_after is None

    # Acquire 2nd connection
    d2 = limiter.acquire_lease(limit, user, lease_id="conn-2")
    assert d2.allowed is True
    assert d2.current == 2
    assert d2.remaining == 0

    # 3rd connection is rejected
    d3 = limiter.acquire_lease(limit, user, lease_id="conn-3")
    assert d3.allowed is False
    assert d3.current == 2
    assert d3.remaining == 0
    assert d3.retry_after == 10.0

    # Re-acquiring with existing lease_id is idempotent (renews)
    d1_again = limiter.acquire_lease(limit, user, lease_id="conn-1")
    assert d1_again.allowed is True
    assert d1_again.current == 2

    # Release conn-1
    limiter.release_lease(d1.key, "conn-1")

    # Now conn-3 can be acquired
    d3_after_release = limiter.acquire_lease(limit, user, lease_id="conn-3")
    assert d3_after_release.allowed is True
    assert d3_after_release.current == 2


def test_memory_backend_lease_expiration() -> None:
    clock = ManualClock()
    backend = MemoryBackend(clock=clock)
    limiter = Limiter(backend)
    limit = ConcurrencyLimit("test-conn", max_concurrent=1, lease_ttl=5.0)
    user = Identity("user", "bob")

    d1 = limiter.acquire_lease(limit, user, lease_id="conn-1")
    assert d1.allowed is True

    # Immediate second attempt rejected
    assert limiter.acquire_lease(limit, user, lease_id="conn-2").allowed is False

    # Advance clock past lease TTL (5 seconds)
    clock.advance(6.0)

    # Expired lease was swept automatically; new connection allowed!
    d2 = limiter.acquire_lease(limit, user, lease_id="conn-2")
    assert d2.allowed is True
    assert d2.current == 1


def test_memory_backend_lease_renewal() -> None:
    clock = ManualClock()
    backend = MemoryBackend(clock=clock)
    limiter = Limiter(backend)
    limit = ConcurrencyLimit("test-conn", max_concurrent=1, lease_ttl=5.0)
    user = Identity("user", "carol")

    d1 = limiter.acquire_lease(limit, user, lease_id="conn-1")
    assert d1.allowed is True

    # Advance clock 3 seconds
    clock.advance(3.0)

    # Renew lease for another 5 seconds
    assert limiter.renew_lease(d1.key, "conn-1", ttl_seconds=5.0) is True

    # Advance clock 3 seconds (total 6 seconds from start, but 3 seconds from renewal)
    clock.advance(3.0)

    # Still occupied because it was renewed!
    assert limiter.acquire_lease(limit, user, lease_id="conn-2").allowed is False

    # Advance 3 more seconds (6 seconds from renewal): now expired
    clock.advance(3.0)
    assert limiter.acquire_lease(limit, user, lease_id="conn-2").allowed is True


@pytest.mark.asyncio
async def test_async_leases() -> None:
    clock = ManualClock()
    backend = MemoryBackend(clock=clock)
    limiter = Limiter(backend)
    limit = ConcurrencyLimit("async-conn", max_concurrent=2, lease_ttl=10.0)
    user = Identity("user", "dan")

    d1 = await limiter.aacquire_lease(limit, user)
    assert d1.allowed is True

    d2 = await limiter.aacquire_lease(limit, user)
    assert d2.allowed is True

    d3 = await limiter.aacquire_lease(limit, user)
    assert d3.allowed is False

    assert await limiter.arenew_lease(d1.key, d1.lease_id, 10.0) is True
    await limiter.arelease_lease(d1.key, d1.lease_id)

    d4 = await limiter.aacquire_lease(limit, user)
    assert d4.allowed is True
