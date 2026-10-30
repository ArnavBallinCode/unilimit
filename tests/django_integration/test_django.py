"""Middleware and ``@ratelimit`` over real WSGI and ASGI request cycles."""

from __future__ import annotations

from typing import Any

import pytest

from unilimit import Identity, Rule

from .conftest import LimiterFactory, asgi, ratelimit_headers, wsgi

SITE = Rule("site", "5/minute", algorithm="fixed_window")


# -- sync and async views, both handlers -------------------------------------------------


@pytest.mark.parametrize("path", ["/sync", "/async", "/scbv", "/acbv", "/method-cbv"])
def test_decorator_under_wsgi(make_limiter: LimiterFactory, path: str) -> None:
    make_limiter()
    c = wsgi()
    responses = [c.get(path) for _ in range(3)]
    assert [r.status_code for r in responses] == [200, 200, 429]
    assert ratelimit_headers(responses[0])["x-ratelimit-remaining"] == "1"
    assert responses[2].json() == {"detail": "Too Many Requests"}
    assert "retry-after" in ratelimit_headers(responses[2])


@pytest.mark.parametrize("path", ["/sync", "/async", "/scbv", "/acbv", "/method-cbv"])
async def test_decorator_under_asgi(make_limiter: LimiterFactory, path: str) -> None:
    make_limiter()
    async with asgi() as c:
        responses = [await c.get(path) for _ in range(3)]
    assert [r.status_code for r in responses] == [200, 200, 429]
    assert ratelimit_headers(responses[1])["x-ratelimit-remaining"] == "0"
    assert int(ratelimit_headers(responses[2])["retry-after"]) >= 1


async def test_global_rules_count_every_request_including_404(
    make_limiter: LimiterFactory,
) -> None:
    make_limiter(global_rules=[SITE])
    async with asgi() as c:
        statuses = [(await c.get(p)).status_code for p in ["/ok", "/aok", "/missing", "/ok"]]
        statuses += [(await c.get("/ok")).status_code, (await c.get("/missing")).status_code]
    assert statuses == [200, 200, 404, 200, 200, 429]


def test_global_rules_under_wsgi(make_limiter: LimiterFactory) -> None:
    make_limiter(global_rules=[SITE])
    c = wsgi()
    responses = [c.get("/ok") for _ in range(6)]
    assert [r.status_code for r in responses] == [200] * 5 + [429]
    assert ratelimit_headers(responses[0])["x-ratelimit-limit"] == "5"


async def test_headers_report_the_most_restrictive_step(make_limiter: LimiterFactory) -> None:
    make_limiter(global_rules=[SITE])
    async with asgi() as c:
        first = await c.get("/async")  # global 5/minute, view 2/minute
    assert ratelimit_headers(first)["x-ratelimit-limit"] == "2"
    assert ratelimit_headers(first)["x-ratelimit-remaining"] == "1"


async def test_decorator_without_middleware_adds_its_own_headers(
    make_limiter: LimiterFactory, settings: Any
) -> None:
    settings.MIDDLEWARE = []
    make_limiter()
    async with asgi() as c:
        responses = [await c.get("/async") for _ in range(3)]
    assert [r.status_code for r in responses] == [200, 200, 429]
    assert ratelimit_headers(responses[0])["x-ratelimit-remaining"] == "1"
    c2 = wsgi()
    assert ratelimit_headers(c2.get("/sync"))["x-ratelimit-remaining"] == "1"


def test_view_headers_are_not_overwritten(memory_limiter: LimiterFactory) -> None:
    memory_limiter()
    response = wsgi().get("/own-headers")
    assert response["X-RateLimit-Limit"] == "custom"
    assert response["X-RateLimit-Remaining"] == "1"


# -- no double consumption ---------------------------------------------------------------


async def test_a_rule_in_middleware_and_decorator_is_consumed_once(
    make_limiter: LimiterFactory,
) -> None:
    limiter = make_limiter(rules=[SITE], global_rules=["site"])
    async with asgi() as c:
        responses = [await c.get("/shares-global") for _ in range(6)]
    assert [r.status_code for r in responses] == [200] * 5 + [429]
    assert limiter.peek(SITE, Identity("ip", "203.0.113.10")).remaining == 0


def test_rules_in_one_decorator_are_atomic(make_limiter: LimiterFactory) -> None:
    limiter = make_limiter()
    c = wsgi()
    assert [c.get("/pair").status_code for _ in range(4)] == [200, 200, 429, 429]
    ip = Identity("ip", "203.0.113.10")
    assert limiter.peek(Rule("dj-pair-b", "5/minute"), ip).remaining == 3  # 2, not 4


def test_multi_rate_string(make_limiter: LimiterFactory) -> None:
    limiter = make_limiter()
    c = wsgi()
    assert [c.get("/multi").status_code for _ in range(4)] == [200, 200, 200, 429]
    day = Rule("dj-multi:4/1d", "4/day")
    assert limiter.peek(day, Identity("ip", "203.0.113.10")).remaining == 1


# -- identities --------------------------------------------------------------------------


def test_methods_filter(memory_limiter: LimiterFactory) -> None:
    memory_limiter()
    c = wsgi()
    assert [c.get("/post").status_code for _ in range(3)] == [200] * 3
    assert [c.post("/post").status_code for _ in range(2)] == [200, 429]


def test_unnamed_inline_rule_is_named_after_the_view(memory_limiter: LimiterFactory) -> None:
    limiter = memory_limiter()
    c = wsgi()
    assert [c.get("/unnamed").status_code for _ in range(3)] == [200, 200, 429]
    rule = Rule("tests.django_integration.views.unnamed", "2/minute")
    assert limiter.peek(rule, Identity("ip", "203.0.113.10")).remaining == 0


def test_header_key_with_ip_fallback(make_limiter: LimiterFactory) -> None:
    make_limiter()
    c = wsgi()
    a = [c.get("/header", headers={"X-Api-Key": "a"}).status_code for _ in range(3)]
    b = [c.get("/header", headers={"X-Api-Key": "b"}).status_code for _ in range(2)]
    anonymous = [c.get("/header").status_code for _ in range(3)]
    assert (a, b, anonymous) == ([200, 200, 429], [200, 200], [200, 200, 429])


def test_callable_key_sees_the_request(memory_limiter: LimiterFactory) -> None:
    memory_limiter()
    c = wsgi()
    acme = [c.get("/tenant?tenant=acme").status_code for _ in range(3)]
    other = c.get("/tenant?tenant=other").status_code
    missing = c.get("/tenant")
    assert (acme, other, missing.status_code) == ([200, 200, 429], 200, 403)


@pytest.mark.parametrize("handler", ["wsgi", "asgi"])
async def test_trusted_proxies(memory_limiter: LimiterFactory, handler: str) -> None:
    from unilimit import TrustedProxies

    memory_limiter(trusted_proxies=TrustedProxies(["10.0.0.0/8"]))
    spoofed = {"X-Forwarded-For": "198.51.100.1"}
    forwarded = [{"X-Forwarded-For": f"198.51.100.{i}"} for i in (1, 1, 1, 2)]
    if handler == "wsgi":
        proxy, direct = wsgi(peer="10.0.0.5"), wsgi(peer="203.0.113.99")
        via_proxy = [proxy.get("/sync", headers=h).status_code for h in forwarded]
        spoof = [direct.get("/sync", headers=spoofed).status_code for _ in range(3)]
    else:
        async with asgi(peer="10.0.0.5") as aproxy, asgi(peer="203.0.113.99") as adirect:
            via_proxy = [(await aproxy.get("/sync", headers=h)).status_code for h in forwarded]
            spoof = [(await adirect.get("/sync", headers=spoofed)).status_code for _ in range(3)]
    # Behind the trusted proxy the forwarded client counts; a direct client cannot spoof.
    assert via_proxy == [200, 200, 429, 200]
    assert spoof == [200, 200, 429]
