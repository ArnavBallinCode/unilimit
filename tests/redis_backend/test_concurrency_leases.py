"""Real Redis tests for distributed concurrent connection limits and leases."""

from __future__ import annotations

import asyncio

import pytest

from unilimit import ConcurrencyLimit, Identity, Limiter
from unilimit.backends.redis import RedisBackend


@pytest.mark.asyncio
async def test_redis_standalone_concurrency_lease(redis_url: str) -> None:
    backend = RedisBackend(redis_url)
    limiter = Limiter(backend)
    limit = ConcurrencyLimit("ws-conns", max_concurrent=2, lease_ttl=5.0)
    user = Identity("user", "alice")

    try:
        # Acquire 1st lease
        d1 = await limiter.aacquire_lease(limit, user, lease_id="conn-1")
        assert d1.allowed is True
        assert d1.current == 1
        assert d1.remaining == 1

        # Acquire 2nd lease
        d2 = await limiter.aacquire_lease(limit, user, lease_id="conn-2")
        assert d2.allowed is True
        assert d2.current == 2
        assert d2.remaining == 0

        # 3rd lease rejected
        d3 = await limiter.aacquire_lease(limit, user, lease_id="conn-3")
        assert d3.allowed is False
        assert d3.current == 2
        assert d3.remaining == 0
        assert d3.retry_after is not None and d3.retry_after > 0

        # Idempotent reacquire with same lease_id allows and refreshes
        d1_renew = await limiter.aacquire_lease(limit, user, lease_id="conn-1")
        assert d1_renew.allowed is True
        assert d1_renew.current == 2

        # Release conn-1
        await limiter.arelease_lease(d1.key, "conn-1")

        # Now conn-3 can be acquired
        d3_retry = await limiter.aacquire_lease(limit, user, lease_id="conn-3")
        assert d3_retry.allowed is True
        assert d3_retry.current == 2
    finally:
        await limiter.aclose()
        limiter.close()


@pytest.mark.asyncio
async def test_redis_cluster_concurrency_lease(cluster_url: str) -> None:
    backend = RedisBackend(cluster_url, cluster=True)
    limiter = Limiter(backend)
    limit = ConcurrencyLimit("cluster-ws", max_concurrent=2, lease_ttl=5.0)
    user = Identity("user", "bob")

    try:
        d1 = await limiter.aacquire_lease(limit, user, lease_id="conn-c1")
        assert d1.allowed is True
        assert d1.current == 1

        d2 = await limiter.aacquire_lease(limit, user, lease_id="conn-c2")
        assert d2.allowed is True
        assert d2.current == 2

        d3 = await limiter.aacquire_lease(limit, user, lease_id="conn-c3")
        assert d3.allowed is False

        assert await limiter.arenew_lease(d1.key, "conn-c1", 5.0) is True
        await limiter.arelease_lease(d1.key, "conn-c1")

        d3_retry = await limiter.aacquire_lease(limit, user, lease_id="conn-c3")
        assert d3_retry.allowed is True
    finally:
        await limiter.aclose()
        limiter.close()


@pytest.mark.asyncio
async def test_redis_lease_automatic_expiration(redis_url: str) -> None:
    backend = RedisBackend(redis_url)
    limiter = Limiter(backend)
    # Short TTL: 1 second
    limit = ConcurrencyLimit("fast-expire", max_concurrent=1, lease_ttl=1.0)
    user = Identity("user", "charlie")

    try:
        d1 = await limiter.aacquire_lease(limit, user, lease_id="conn-exp")
        assert d1.allowed is True

        d2 = await limiter.aacquire_lease(limit, user, lease_id="conn-blocked")
        assert d2.allowed is False

        # Sleep slightly longer than 1 second so lease expires in Redis
        await asyncio.sleep(1.2)

        # Stale lease has expired and is cleaned up on next acquire!
        d3 = await limiter.aacquire_lease(limit, user, lease_id="conn-fresh")
        assert d3.allowed is True
        assert d3.current == 1
    finally:
        await limiter.aclose()
        limiter.close()
