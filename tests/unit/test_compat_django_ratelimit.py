from __future__ import annotations

from django.http import HttpRequest, HttpResponse
from django.test import RequestFactory

from unilimit.compat.django_ratelimit import ratelimit


def test_django_ratelimit_compat_blocking() -> None:
    factory = RequestFactory()

    @ratelimit(group="compat_test", rate="2/m", block=True)
    def my_view(request: HttpRequest) -> HttpResponse:
        return HttpResponse("ok")

    res1 = my_view(factory.get("/test"))
    assert res1.status_code == 200

    res2 = my_view(factory.get("/test"))
    assert res2.status_code == 200

    # Third request blocked -> 429
    res3 = my_view(factory.get("/test"))
    assert res3.status_code == 429


def test_django_ratelimit_compat_non_blocking() -> None:
    factory = RequestFactory()

    @ratelimit(group="compat_nonblock", rate="1/m", block=False)
    def my_view(request: HttpRequest) -> HttpResponse:
        if getattr(request, "limited", False):
            return HttpResponse("too fast", status=400)
        return HttpResponse("ok")

    req1 = factory.get("/test")
    res1 = my_view(req1)
    assert res1.status_code == 200
    assert not getattr(req1, "limited", False)

    req2 = factory.get("/test")
    res2 = my_view(req2)
    assert res2.status_code == 400
    assert getattr(req2, "limited", False)
