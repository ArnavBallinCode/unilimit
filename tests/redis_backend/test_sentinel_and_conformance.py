from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from unilimit.backends.redis import RedisBackend
from unilimit.backends.redis.backend import _parse_sentinel_url
from unilimit.core.errors import ConfigurationError
from unilimit.testing import (
    verify_async_backend_conformance,
    verify_async_lease_backend_conformance,
    verify_backend_conformance,
    verify_lease_backend_conformance,
)


def test_parse_sentinel_url_basic() -> None:
    sentinels, service, opts = _parse_sentinel_url("sentinel://localhost:26379/mymaster")
    assert sentinels == (("localhost", 26379),)
    assert service == "mymaster"
    assert opts == {}


def test_parse_sentinel_url_multi_host_with_auth_and_options() -> None:
    url = (
        "redis+sentinel://user:secret@sentinel1:26379,sentinel2:26380/production/3"
        "?socket_timeout=1.5&custom=val"
    )
    sentinels, service, opts = _parse_sentinel_url(url)
    assert sentinels == (("sentinel1", 26379), ("sentinel2", 26380))
    assert service == "production"
    assert opts["username"] == "user"
    assert opts["password"] == "secret"
    assert opts["db"] == 3
    assert opts["socket_timeout"] == 1.5
    assert opts["custom"] == "val"


def test_parse_sentinel_url_invalid() -> None:
    with pytest.raises(ConfigurationError, match="unsupported sentinel scheme"):
        _parse_sentinel_url("http://localhost:26379/mymaster")

    with pytest.raises(ConfigurationError, match="service name"):
        _parse_sentinel_url("sentinel://localhost:26379/")

    with pytest.raises(ConfigurationError, match="invalid port"):
        _parse_sentinel_url("sentinel://localhost:notaport/mymaster")


def test_sentinel_backend_configuration() -> None:
    backend = RedisBackend.from_sentinel(
        [("sentinel1", 26379), ("sentinel2", 26380)],
        "mymaster",
        sentinel_kwargs={"socket_timeout": 2.0},
        client_options={"db": 1},
    )
    assert backend.sentinels == (("sentinel1", 26379), ("sentinel2", 26380))
    assert backend.service_name == "mymaster"
    assert backend.name == "redis-sentinel"
    assert not backend.cluster

    # sentinel URL constructor
    backend_url = RedisBackend("sentinel://10.0.0.1:26379/prod_master?db=2")
    assert backend_url.sentinels == (("10.0.0.1", 26379),)
    assert backend_url.service_name == "prod_master"
    assert backend_url.name == "redis-sentinel"


def test_sentinel_validation_errors() -> None:
    with pytest.raises(ConfigurationError, match="service_name is required"):
        RedisBackend(sentinels=[("localhost", 26379)])

    with pytest.raises(ConfigurationError, match="cluster=True cannot be used with Sentinel"):
        RedisBackend(sentinels=[("localhost", 26379)], service_name="m", cluster=True)


def test_sentinel_sync_and_async_client_initialization() -> None:
    backend = RedisBackend.from_sentinel([("127.0.0.1", 26379)], "mymaster")

    mock_master = MagicMock()
    mock_sentinel_cls = MagicMock()
    mock_sentinel_instance = mock_sentinel_cls.return_value
    mock_sentinel_instance.master_for.return_value = mock_master

    with patch("redis.sentinel.Sentinel", mock_sentinel_cls):
        client, script = backend._sync()
        assert client is mock_master
        assert mock_sentinel_instance.master_for.called
        assert mock_master.register_script.call_count == 2


async def test_sentinel_async_entry_initialization() -> None:
    backend = RedisBackend.from_sentinel([("127.0.0.1", 26379)], "mymaster")

    mock_async_master = MagicMock()
    mock_async_sentinel_cls = MagicMock()
    mock_async_sentinel_instance = mock_async_sentinel_cls.return_value
    mock_async_sentinel_instance.master_for.return_value = mock_async_master

    with patch("redis.asyncio.sentinel.Sentinel", mock_async_sentinel_cls):
        entry = backend._async_entry()
        assert entry.client is mock_async_master
        assert mock_async_sentinel_instance.master_for.called
        assert mock_async_master.register_script.call_count == 2


def test_tls_url_and_options_configuration() -> None:
    tls_url = "rediss://:mypass@prod-redis.internal:6380/0"
    backend = RedisBackend(tls_url, client_options={"ssl_cert_reqs": "required"})
    assert backend._url == tls_url
    assert backend._options["ssl_cert_reqs"] == "required"
    assert backend.name == "redis"


def test_limiter_from_url_sentinel_and_tls() -> None:
    from unilimit import Limiter

    limiter = Limiter.from_url(
        "redis+sentinel://sentinel1:26379,sentinel2:26380/mymaster",
        namespace="sentinel_app",
        sentinel_kwargs={"socket_timeout": 3.0},
    )
    assert isinstance(limiter.backend, RedisBackend)
    assert limiter.backend.sentinels == (("sentinel1", 26379), ("sentinel2", 26380))
    assert limiter.backend.service_name == "mymaster"
    assert limiter.backend.name == "redis-sentinel"
    assert limiter.namespace == "sentinel_app"

    limiter_tls = Limiter.from_url(
        "rediss://localhost:6380/0",
        namespace="tls_app",
        client_options={"ssl_cert_reqs": "required"},
    )
    assert isinstance(limiter_tls.backend, RedisBackend)
    assert limiter_tls.backend._url == "rediss://localhost:6380/0"
    assert limiter_tls.backend._options["ssl_cert_reqs"] == "required"
    assert limiter_tls.backend.name == "redis"


def test_redis_backend_conforms_sync(redis_url: str) -> None:
    backend = RedisBackend(redis_url)
    try:
        verify_backend_conformance(backend)
    finally:
        backend.close()


async def test_redis_backend_conforms_async(redis_url: str) -> None:
    backend = RedisBackend(redis_url)
    try:
        await verify_async_backend_conformance(backend)
    finally:
        await backend.aclose()


def test_redis_backend_conforms_lease_sync(redis_url: str) -> None:
    backend = RedisBackend(redis_url)
    try:
        verify_lease_backend_conformance(backend)
    finally:
        backend.close()


async def test_redis_backend_conforms_lease_async(redis_url: str) -> None:
    backend = RedisBackend(redis_url)
    try:
        await verify_async_lease_backend_conformance(backend)
    finally:
        await backend.aclose()
