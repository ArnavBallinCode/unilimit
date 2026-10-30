from __future__ import annotations

import pytest
from starlette.requests import Request
from starlette.responses import JSONResponse

from unilimit.compat.slowapi import (
    Limiter,
    RateLimitExceeded,
    _rate_limit_exceeded_handler,
    get_remote_address,
)


def _make_request(host: str = "203.0.113.10", method: str = "GET") -> Request:
    scope = {
        "type": "http",
        "method": method,
        "path": "/test",
        "headers": [],
        "client": (host, 12345),
    }
    return Request(scope)


def test_get_remote_address() -> None:
    req = _make_request("198.51.100.1")
    assert get_remote_address(req) == "198.51.100.1"


def test_slowapi_sync_endpoint_limiting() -> None:
    limiter = Limiter()

    @limiter.limit("2/minute")
    def my_view(request: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    req = _make_request()
    res1 = my_view(req)
    assert res1.status_code == 200

    res2 = my_view(req)
    assert res2.status_code == 200

    with pytest.raises(RateLimitExceeded) as exc_info:
        my_view(req)
    assert exc_info.value.status_code == 429

    # Handler renders JSONResponse with 429
    handled = _rate_limit_exceeded_handler(req, exc_info.value)
    assert handled.status_code == 429


@pytest.mark.asyncio
async def test_slowapi_async_endpoint_limiting() -> None:
    limiter = Limiter()

    @limiter.limit("1/minute")
    async def my_async_view(request: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    req = _make_request()
    res = await my_async_view(req)
    assert res.status_code == 200

    with pytest.raises(RateLimitExceeded):
        await my_async_view(req)
