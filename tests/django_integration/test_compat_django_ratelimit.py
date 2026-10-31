"""The django-ratelimit adapter (``unilimit.compat.django_ratelimit``)."""

from __future__ import annotations

from typing import Any

import pytest
from django.http import HttpRequest, HttpResponse
from django.test import AsyncRequestFactory, RequestFactory
from django.utils.decorators import method_decorator
from django.views import View

from unilimit import ConfigurationError, Identity, Limiter, Rule
from unilimit.compat.django_ratelimit import UNSAFE, ratelimit
from unilimit.testing import ManualClock

from .conftest import PEER, LimiterFactory

factory = RequestFactory(REMOTE_ADDR=PEER)
seen: list[tuple[str, Any]] = []


def ok(request: HttpRequest) -> HttpResponse:
    return HttpResponse("limited" if getattr(request, "limited", False) else "ok")


def statuses(view: Any, requests: list[HttpRequest]) -> list[int]:
    return [view(r).status_code for r in requests]


def plan_key(group: str, request: HttpRequest) -> str:
    seen.append((group, request))
    return request.GET.get("plan", "")


def plan_rate(group: str, request: HttpRequest) -> str | None:
    return None if request.GET.get("plan") == "staff" else "1/m"


def test_callable_key_receives_group_and_request(memory_limiter: LimiterFactory) -> None:
    memory_limiter()
    seen.clear()
    view = ratelimit(group="plans", key=plan_key, rate="2/m")(ok)
    gold = statuses(view, [factory.get("/?plan=gold") for _ in range(3)])
    free = statuses(view, [factory.get("/?plan=free") for _ in range(2)])
    assert (gold, free) == ([200, 200, 429], [200, 200])
    assert {group for group, _ in seen} == {"plans"}
    assert all(isinstance(request, HttpRequest) for _, request in seen)


def test_empty_values_share_one_bucket_like_upstream(memory_limiter: LimiterFactory) -> None:
    memory_limiter()
    view = ratelimit(key="header:x-api-key", rate="2/m")(ok)
    anonymous = statuses(view, [factory.get("/") for _ in range(3)])
    keyed = statuses(view, [factory.get("/", headers={"X-Api-Key": "a"}) for _ in range(2)])
    assert (anonymous, keyed) == ([200, 200, 429], [200, 200])


@pytest.mark.parametrize(
    ("key", "make"),
    [
        ("get:q", lambda v: factory.get(f"/?q={v}")),
        ("post:q", lambda v: factory.post("/", {"q": v})),
        ("header:x-real-ip", lambda v: factory.get("/", headers={"X-Real-Ip": v})),
    ],
)
def test_accessor_keys(memory_limiter: LimiterFactory, key: str, make: Any) -> None:
    memory_limiter()
    view = ratelimit(key=key, rate="1/m")(ok)
    assert statuses(view, [make("a"), make("a"), make("b")]) == [200, 429, 200]


def test_dotted_path_key_and_rate(memory_limiter: LimiterFactory) -> None:
    memory_limiter()
    here = __name__
    view = ratelimit(key=f"{here}.plan_key", rate=f"{here}.plan_rate")(ok)
    staff = statuses(view, [factory.get("/?plan=staff") for _ in range(3)])
    gold = statuses(view, [factory.get("/?plan=gold") for _ in range(2)])
    assert (staff, gold) == ([200, 200, 200], [200, 429])


def test_callable_rates_have_separate_counters(memory_limiter: LimiterFactory) -> None:
    memory_limiter()
    view = ratelimit(key="ip", rate=lambda g, r: r.GET.get("rate"))(ok)
    assert statuses(view, [factory.get("/?rate=1/m"), factory.get("/?rate=1/m")]) == [200, 429]
    assert statuses(view, [factory.get("/?rate=2/m") for _ in range(3)]) == [200, 200, 429]
    unlimited = factory.get("/")
    assert view(unlimited).status_code == 200
    assert unlimited.limited is False  # type: ignore[attr-defined]


def test_stacked_decorators_are_independent_limits(make_limiter: LimiterFactory) -> None:
    limiter = make_limiter()
    minute = ratelimit(group="stack", key="ip", rate="2/m")
    hour = ratelimit(group="stack", key="ip", rate="3/h")
    view = minute(hour(ok))
    assert statuses(view, [factory.get("/") for _ in range(3)]) == [200, 200, 429]
    ip = Identity("ip", PEER)
    assert limiter.peek(Rule("stack:2/1m", "2/minute"), ip).remaining == 0
    assert limiter.peek(Rule("stack:3/1h", "3/hour"), ip).remaining == 1


def test_tuple_rate_and_upstream_grammar(settings: Any) -> None:
    clock = ManualClock()
    limiter = Limiter.from_url("memory://", clock=clock)
    settings.UNILIMIT = {"LIMITER": limiter}
    tuple_view = ratelimit(group="tuple", key="ip", rate=(1, 2))(ok)
    multiplier = ratelimit(group="multi", key="ip", rate="1/2s")(ok)
    first_letter = ratelimit(group="month", key="ip", rate="1/month")(ok)  # a minute
    for view in (tuple_view, multiplier, first_letter):
        assert statuses(view, [factory.get("/"), factory.get("/")]) == [200, 429]
    clock.advance(4.5)
    assert statuses(tuple_view, [factory.get("/")]) == [200]
    assert statuses(multiplier, [factory.get("/")]) == [200]
    clock.advance(120)
    assert statuses(first_letter, [factory.get("/")]) == [200]


def test_block_false_sets_request_limited(memory_limiter: LimiterFactory) -> None:
    memory_limiter()
    view = ratelimit(key="ip", rate="1/m", block=False)(ok)
    responses = [view(factory.get("/")) for _ in range(2)]
    assert [(r.status_code, r.content) for r in responses] == [(200, b"ok"), (200, b"limited")]


def test_request_limited_is_sticky_across_stacked_decorators(
    memory_limiter: LimiterFactory,
) -> None:
    memory_limiter()
    inner = ratelimit(group="inner", key="ip", rate="5/m", block=False)(ok)
    view = ratelimit(group="outer", key="ip", rate="1/m", block=False)(inner)
    assert [view(factory.get("/")).content for _ in range(2)] == [b"ok", b"limited"]


def test_methods(memory_limiter: LimiterFactory) -> None:
    memory_limiter()
    view = ratelimit(key="ip", rate="1/m", method=UNSAFE)(ok)
    assert statuses(view, [factory.get("/") for _ in range(3)]) == [200, 200, 200]
    assert statuses(view, [factory.post("/"), factory.put("/")]) == [200, 429]
    post_only = ratelimit(key="ip", rate="1/m", method="POST")(ok)
    assert statuses(post_only, [factory.get("/"), factory.get("/")]) == [200, 200]
    assert ratelimit.ALL == (None,)  # type: ignore[attr-defined]


async def test_async_views(memory_limiter: LimiterFactory) -> None:
    memory_limiter()
    async_factory = AsyncRequestFactory(REMOTE_ADDR=PEER)

    async def aview(request: HttpRequest) -> HttpResponse:
        return HttpResponse("ok")

    static = ratelimit(key="ip", rate="1/m")(aview)
    dynamic = ratelimit(group="dyn", key="ip", rate=lambda g, r: "1/m")(aview)
    for view in (static, dynamic):
        responses = [await view(async_factory.get("/")) for _ in range(2)]
        assert [r.status_code for r in responses] == [200, 429]


def test_class_based_views_and_upstream_group_names(memory_limiter: LimiterFactory) -> None:
    memory_limiter()
    seen.clear()

    @method_decorator(ratelimit(key=plan_key, rate="1/m"), name="dispatch")
    class Plans(View):
        def get(self, request: HttpRequest) -> HttpResponse:
            return HttpResponse("ok")

    view = Plans.as_view()
    assert statuses(view, [factory.get("/?plan=a"), factory.get("/?plan=a")]) == [200, 429]
    # django-ratelimit's default group for a bound method: module, class, qualname.
    assert seen[0][0] == "django.views.generic.base.Plans.View.dispatch"


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"rate": "fast"},
        {"rate": "1/m", "key": "cookie"},
        {"rate": "1/m", "key": "cookie:session"},
    ],
)
def test_configuration_errors_are_raised_at_import(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ConfigurationError):
        ratelimit(**kwargs)(ok)
