"""Views of the end-to-end Django project: global rule, decorators and a DRF throttle."""

from __future__ import annotations

import os

from django.http import HttpRequest, JsonResponse
from django.urls import path
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from unilimit import Limiter, Rule
from unilimit.integrations.django import ratelimit
from unilimit.integrations.drf import UniLimitScopedRateThrottle

limiter = Limiter.from_url(
    os.environ["UNILIMIT_TEST_REDIS_URL"],
    cluster=os.environ.get("UNILIMIT_TEST_CLUSTER") == "1",
    global_rules=[Rule("site", "500/day", algorithm="fixed_window")],
    # "/" is exempt because local tooling (port scanners) may probe it and would spend the budget.
    exempt_paths=["/health", "/"],
)


def health(request: HttpRequest) -> JsonResponse:
    return JsonResponse({"pid": os.getpid()})


@ratelimit("e2e-sync", rate="100/day")
def limited_sync(request: HttpRequest) -> JsonResponse:
    return JsonResponse({"pid": os.getpid()})


@ratelimit("e2e-async", rate="100/day")
async def limited_async(request: HttpRequest) -> JsonResponse:
    return JsonResponse({"pid": os.getpid()})


class Throttled(APIView):
    throttle_classes = [UniLimitScopedRateThrottle]
    throttle_scope = "e2e"

    def get(self, request: Request) -> Response:
        return Response({"pid": os.getpid()})


urlpatterns = [
    path("health", health),
    path("sync", limited_sync),
    path("async", limited_async),
    path("api", Throttled.as_view()),
]
