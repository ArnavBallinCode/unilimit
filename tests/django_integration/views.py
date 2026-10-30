"""Views for the Django and DRF integration tests. Limiters come from settings.UNILIMIT."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

from django.http import HttpRequest, HttpResponse, JsonResponse, StreamingHttpResponse
from django.utils.decorators import method_decorator
from django.views import View
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle, ScopedRateThrottle
from rest_framework.views import APIView

from unilimit import RequestContext, Rule
from unilimit.integrations.django import ratelimit
from unilimit.integrations.drf import (
    UniLimitAnonRateThrottle,
    UniLimitRateThrottle,
    UniLimitScopedRateThrottle,
    UniLimitThrottle,
    UniLimitUserRateThrottle,
)

# -- Django views ------------------------------------------------------------------------


def ok(request: HttpRequest) -> JsonResponse:
    return JsonResponse({"ok": True})


async def aok(request: HttpRequest) -> JsonResponse:
    return JsonResponse({"ok": True})


@ratelimit("dj-sync", rate="2/minute")
def limited_sync(request: HttpRequest) -> JsonResponse:
    return JsonResponse({"ok": True})


@ratelimit("dj-async", rate="2/minute")
async def limited_async(request: HttpRequest) -> JsonResponse:
    return JsonResponse({"ok": True})


@ratelimit("dj-user", rate="2/minute", key="user")
def by_user(request: HttpRequest) -> JsonResponse:
    return JsonResponse({"ok": True})


@ratelimit("dj-user-async", rate="2/minute", key="user")
async def by_user_async(request: HttpRequest) -> JsonResponse:
    return JsonResponse({"ok": True})


@ratelimit("dj-uoi", rate="2/minute", key="user_or_ip")
async def user_or_ip(request: HttpRequest) -> JsonResponse:
    return JsonResponse({"ok": True})


@ratelimit("dj-multi", rate="3/minute;4/day")
def multi(request: HttpRequest) -> JsonResponse:
    return JsonResponse({"ok": True})


@ratelimit(Rule("dj-pair-a", "2/minute"), Rule("dj-pair-b", "5/minute"))
def pair(request: HttpRequest) -> JsonResponse:
    return JsonResponse({"ok": True})


@ratelimit("dj-header", rate="2/minute", key=["header:X-Api-Key", "ip"])
def by_header(request: HttpRequest) -> JsonResponse:
    return JsonResponse({"ok": True})


def tenant_of(ctx: RequestContext) -> str | None:
    request: HttpRequest = ctx.raw
    return request.GET.get("tenant")


@ratelimit("dj-tenant", rate="2/minute", key=tenant_of)
def by_tenant(request: HttpRequest) -> JsonResponse:
    return JsonResponse({"ok": True})


@ratelimit("dj-post", rate="1/minute", methods=["POST"])
def post_only(request: HttpRequest) -> JsonResponse:
    return JsonResponse({"ok": True})


@ratelimit("site")  # the limiter's global rule, by name: never consumed twice
def shares_global(request: HttpRequest) -> JsonResponse:
    return JsonResponse({"ok": True})


@ratelimit(rate="2/minute")  # named after the view
def unnamed(request: HttpRequest) -> JsonResponse:
    return JsonResponse({"ok": True})


@ratelimit("dj-own-headers", rate="2/minute")
def own_headers(request: HttpRequest) -> HttpResponse:
    response = HttpResponse("ok")
    response["X-RateLimit-Limit"] = "custom"
    return response


def _chunks() -> Iterator[bytes]:
    yield b"a"
    yield b"b"


@ratelimit("dj-stream", rate="2/minute")
def stream(request: HttpRequest) -> StreamingHttpResponse:
    return StreamingHttpResponse(_chunks())


async def _achunks() -> AsyncIterator[bytes]:
    yield b"a"
    yield b"b"


@ratelimit("dj-astream", rate="2/minute")
async def astream(request: HttpRequest) -> StreamingHttpResponse:
    return StreamingHttpResponse(_achunks())


@method_decorator(ratelimit("dj-acbv", rate="2/minute"), name="dispatch")
class AsyncCBV(View):
    async def get(self, request: HttpRequest) -> JsonResponse:
        return JsonResponse({"ok": True})


@method_decorator(ratelimit("dj-scbv", rate="2/minute"), name="dispatch")
class SyncCBV(View):
    def get(self, request: HttpRequest) -> JsonResponse:
        return JsonResponse({"ok": True})


class MethodCBV(View):
    @method_decorator(ratelimit("dj-method", rate="2/minute"))
    async def get(self, request: HttpRequest) -> JsonResponse:
        return JsonResponse({"ok": True})


# -- DRF ---------------------------------------------------------------------------------


class AnonAndUser(APIView):
    throttle_classes = [UniLimitAnonRateThrottle, UniLimitUserRateThrottle]

    def get(self, request: Request) -> Response:
        return Response({"user": getattr(request.user, "pk", None)})


class Scoped(APIView):
    throttle_classes = [UniLimitScopedRateThrottle]
    throttle_scope = "search"

    def get(self, request: Request) -> Response:
        return Response({"ok": True})


class ScopedOther(Scoped):
    """Same scope as ``Scoped``: shares its counter, as in DRF."""


class Unscoped(APIView):
    throttle_classes = [UniLimitScopedRateThrottle]

    def get(self, request: Request) -> Response:
        return Response({"ok": True})


class WithRules(APIView):
    throttle_classes = [UniLimitThrottle]
    unilimit_rules = [
        Rule("drf-a", "2/minute", key="user_or_ip"),
        Rule("drf-b", "5/minute", key="user_or_ip"),
    ]

    def post(self, request: Request) -> Response:
        return Response({"ok": True})


class Burst(UniLimitRateThrottle):
    scope = "burst"
    rate = "2/minute"


class Sustained(UniLimitRateThrottle):
    scope = "sustained"
    rate = "3/minute"


class TwoThrottles(APIView):
    """Two throttles: evaluated as one atomic batch."""

    throttle_classes = [Sustained, Burst]

    def get(self, request: Request) -> Response:
        return Response({"ok": True})


def client_id(ctx: RequestContext) -> str | None:
    request: Request = ctx.raw
    value = request.query_params.get("client")
    return str(value) if value else None


class ByClient(UniLimitRateThrottle):
    scope = "client"
    rate = "2/minute"
    key = staticmethod(client_id)  # type: ignore[assignment]


class CustomIdentity(APIView):
    throttle_classes = [ByClient]

    def get(self, request: Request) -> Response:
        return Response({"ok": True})


class StrictThrottle(UniLimitRateThrottle):
    scope = "strict"
    rate = "5/minute"
    fail_mode = "closed"


class Strict(APIView):
    throttle_classes = [StrictThrottle]

    def get(self, request: Request) -> Response:
        return Response({"ok": True})


class Mixed(APIView):
    """Misconfigured: DRF's AnonRateThrottle and UniLimit's on the same scope."""

    throttle_classes = [AnonRateThrottle, UniLimitAnonRateThrottle]

    def get(self, request: Request) -> Response:
        return Response({"ok": True})


class MixedScoped(APIView):
    throttle_classes = [ScopedRateThrottle, UniLimitScopedRateThrottle]
    throttle_scope = "search"

    def get(self, request: Request) -> Response:
        return Response({"ok": True})


SHARED = Rule("drf-shared", "3/minute", key="user_or_ip")


class ThrottleAndDecorator(APIView):
    """The same rule as a throttle and a decorator: consumed once per request."""

    throttle_classes = [UniLimitThrottle]
    unilimit_rules = [SHARED]

    @method_decorator(ratelimit(SHARED))
    def get(self, request: Request) -> Response:
        return Response({"ok": True})


class DecoratedAPIView(APIView):
    """``@ratelimit`` on a DRF method sees DRF's authenticated user."""

    @method_decorator(ratelimit("drf-decorated", rate="2/minute", key="user"))
    def get(self, request: Request) -> Response:
        return Response({"ok": True})
