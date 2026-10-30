"""DRF throttles over real DRF request cycles (WSGI and ASGI)."""

from __future__ import annotations

from typing import Any

import pytest
from django.core.exceptions import ImproperlyConfigured
from rest_framework.exceptions import Throttled

from unilimit import Identity, Rule
from unilimit.integrations.drf import (
    ServiceUnavailable,
    UniLimitAnonRateThrottle,
    UniLimitRateThrottle,
    parse_drf_rate,
)
from unilimit.integrations.drf.throttling import UniLimitBaseThrottle

from .auth import SEEN_EXCEPTIONS
from .conftest import LimiterFactory, asgi, ratelimit_headers, wsgi

ALICE = {"Authorization": "User alice"}
BOB = {"Authorization": "User bob"}
IP = Identity("ip", "203.0.113.10")


# -- anon and user throttles -------------------------------------------------------------


def test_anon_and_user_throttles(make_limiter: LimiterFactory) -> None:
    limiter = make_limiter()
    c = wsgi()
    anonymous = [c.get("/api/anon-user") for _ in range(3)]
    alice = [c.get("/api/anon-user", headers=ALICE).status_code for _ in range(4)]
    bob = [c.get("/api/anon-user", headers=BOB).status_code for _ in range(1)]
    assert [r.status_code for r in anonymous] == [200, 200, 429]
    assert alice == [200, 200, 200, 429]  # anon rule does not apply to users; user: 3/min
    assert bob == [200]
    # Anonymous requests also count against "drf:user" by IP (user_or_ip), and the
    # rejected third request consumed nothing from it (one atomic batch).
    assert limiter.peek(Rule("drf:user", "3/minute"), IP).remaining == 1


async def test_throttles_under_asgi(make_limiter: LimiterFactory) -> None:
    make_limiter()
    async with asgi() as c:
        statuses = [(await c.get("/api/anon-user")).status_code for _ in range(3)]
        users = [(await c.get("/api/anon-user", headers=ALICE)).status_code for _ in range(4)]
    assert statuses == [200, 200, 429]
    assert users == [200, 200, 200, 429]


def test_throttled_response_is_drfs(make_limiter: LimiterFactory) -> None:
    make_limiter()
    c = wsgi()
    for _ in range(2):
        ok = c.get("/api/anon-user")
    rejected = c.get("/api/anon-user")
    assert rejected.status_code == 429
    assert rejected.json()["detail"].startswith("Request was throttled.")
    headers = ratelimit_headers(rejected)
    assert int(headers["retry-after"]) >= 1  # set by DRF from wait()
    assert headers["x-ratelimit-limit"] == "2"  # added by UniLimitMiddleware
    assert ratelimit_headers(ok)["x-ratelimit-remaining"] == "0"


def test_without_middleware_throttles_still_work(
    make_limiter: LimiterFactory, settings: Any
) -> None:
    settings.MIDDLEWARE = []
    make_limiter()
    c = wsgi()
    responses = [c.get("/api/anon-user") for _ in range(3)]
    assert [r.status_code for r in responses] == [200, 200, 429]
    assert "retry-after" in ratelimit_headers(responses[2])
    assert "x-ratelimit-limit" not in ratelimit_headers(responses[0])


def test_custom_exception_handler_sees_throttled(
    memory_limiter: LimiterFactory, settings: Any
) -> None:
    settings.REST_FRAMEWORK = {
        **settings.REST_FRAMEWORK,
        "EXCEPTION_HANDLER": "tests.django_integration.auth.recording_exception_handler",
    }
    SEEN_EXCEPTIONS.clear()
    memory_limiter()
    c = wsgi()
    assert [c.get("/api/anon-user").status_code for _ in range(3)] == [200, 200, 429]
    assert [Throttled] == SEEN_EXCEPTIONS


# -- scoped and multiple policies -------------------------------------------------------


def test_scoped_throttle_shares_a_scope_across_views(make_limiter: LimiterFactory) -> None:
    make_limiter()
    c = wsgi()
    statuses = [c.get(p).status_code for p in ["/api/scoped", "/api/scoped-other"] * 2]
    assert statuses == [200, 200, 429, 429]
    assert [c.get("/api/unscoped").status_code for _ in range(5)] == [200] * 5


def test_unilimit_rules_on_the_view(make_limiter: LimiterFactory) -> None:
    limiter = make_limiter()
    c = wsgi()
    assert [c.post("/api/rules").status_code for _ in range(3)] == [200, 200, 429]
    assert limiter.peek(Rule("drf-b", "5/minute"), IP).remaining == 3


def test_several_throttles_are_one_atomic_batch(make_limiter: LimiterFactory) -> None:
    limiter = make_limiter()
    c = wsgi()
    assert [c.get("/api/two").status_code for _ in range(4)] == [200, 200, 429, 429]
    # "sustained" (3/min) was consumed only by the two admitted requests.
    assert limiter.peek(Rule("drf:sustained", "3/minute"), IP).remaining == 1


def test_wait_is_from_the_rejecting_throttle_only(memory_limiter: LimiterFactory) -> None:
    memory_limiter()
    c = wsgi()
    for _ in range(2):
        c.get("/api/two")
    rejected = c.get("/api/two")
    assert rejected.status_code == 429
    assert 1 <= int(rejected["Retry-After"]) <= 60


def test_custom_identity(make_limiter: LimiterFactory) -> None:
    make_limiter()
    c = wsgi()
    a = [c.get("/api/custom?client=a").status_code for _ in range(3)]
    b = c.get("/api/custom?client=b").status_code
    missing = c.get("/api/custom").status_code
    assert (a, b, missing) == ([200, 200, 429], 200, 403)  # no identity: denied


def test_throttle_and_decorator_with_one_rule_consume_once(make_limiter: LimiterFactory) -> None:
    limiter = make_limiter()
    c = wsgi()
    assert [c.get("/api/shared").status_code for _ in range(4)] == [200, 200, 200, 429]
    assert limiter.peek(Rule("drf-shared", "3/minute", key="user_or_ip"), IP).remaining == 0


def test_decorator_on_a_drf_method_sees_drf_authentication(
    memory_limiter: LimiterFactory,
) -> None:
    memory_limiter()
    c = wsgi()
    assert [c.get("/api/decorated", headers=ALICE).status_code for _ in range(3)] == [
        200,
        200,
        429,
    ]
    assert c.get("/api/decorated", headers=BOB).status_code == 200
    assert c.get("/api/decorated").status_code == 403


# -- double throttling is refused ---------------------------------------------------------


@pytest.mark.parametrize("path", ["/api/mixed", "/api/mixed-scoped"])
def test_builtin_and_unilimit_throttles_on_one_scope_are_refused(
    memory_limiter: LimiterFactory, path: str
) -> None:
    memory_limiter()
    with pytest.raises(ImproperlyConfigured, match="counted twice"):
        wsgi().get(path)


# -- configuration conventions ---------------------------------------------------------


def test_rates_from_settings_follow_override_settings(
    memory_limiter: LimiterFactory, settings: Any
) -> None:
    settings.REST_FRAMEWORK = {
        **settings.REST_FRAMEWORK,
        "DEFAULT_THROTTLE_RATES": {"anon": "1/minute", "user": None},
    }
    memory_limiter()
    c = wsgi()
    assert [c.get("/api/anon-user").status_code for _ in range(2)] == [200, 429]
    assert [c.get("/api/anon-user", headers=ALICE).status_code for _ in range(5)] == [200] * 5


def test_missing_rate_is_the_drf_error(memory_limiter: LimiterFactory, settings: Any) -> None:
    settings.REST_FRAMEWORK = {**settings.REST_FRAMEWORK, "DEFAULT_THROTTLE_RATES": {}}
    memory_limiter()
    with pytest.raises(ImproperlyConfigured, match="No default throttle rate set for 'anon'"):
        wsgi().get("/api/anon-user")


def test_default_throttle_classes(settings: Any) -> None:
    settings.REST_FRAMEWORK = {
        **settings.REST_FRAMEWORK,
        "DEFAULT_THROTTLE_CLASSES": ["unilimit.integrations.drf.UniLimitAnonRateThrottle"],
    }
    from rest_framework.settings import api_settings

    # DRF copies DEFAULT_THROTTLE_CLASSES onto APIView at import time; the dotted path
    # resolves like any DRF throttle.
    classes: list[Any] = list(api_settings.DEFAULT_THROTTLE_CLASSES)
    assert classes == [UniLimitAnonRateThrottle]


@pytest.mark.parametrize(
    ("rate", "expected"),
    [
        ("100/day", [(100, 86_400)]),
        ("3/min", [(3, 60)]),
        ("10/s", [(10, 1)]),
        ("1/month", [(1, 60)]),  # DRF reads only "m": per minute, exactly like DRF
        ("5/30s", [(5, 30)]),  # UniLimit grammar
        ("10/second;1000/day", [(10, 1), (1000, 86_400)]),
    ],
)
def test_parse_drf_rate(rate: str, expected: list[tuple[int, int]]) -> None:
    parsed = [(r.limit, r.window_us // 1_000_000) for r in parse_drf_rate(rate)]
    assert parsed == expected


def test_drf_rate_parsing_matches_drf() -> None:
    from rest_framework.throttling import SimpleRateThrottle

    for rate in ["100/day", "3/min", "10/s", "1/month", "7/hour", "2/d"]:
        theirs = SimpleRateThrottle.parse_rate(SimpleRateThrottle.__new__(SimpleRateThrottle), rate)
        ours = parse_drf_rate(rate)[0]
        assert (ours.limit, ours.window_us // 1_000_000) == theirs


def test_invalid_rate() -> None:
    with pytest.raises(ImproperlyConfigured, match="invalid throttle rate"):
        parse_drf_rate("lots")


def test_multi_rate_scope_creates_one_rule_per_rate() -> None:
    class Both(UniLimitRateThrottle):
        scope = "both"
        rate = "2/second;10/day"

    rules = Both().get_rules(None, None)
    assert [r.name for r in rules if isinstance(r, Rule)] == ["drf:both:2/1s", "drf:both:10/1d"]


def test_base_throttle_requires_get_rules() -> None:
    with pytest.raises(NotImplementedError):
        UniLimitBaseThrottle().get_rules(None, None)


def test_service_unavailable_is_an_api_exception() -> None:
    assert ServiceUnavailable.status_code == 503
