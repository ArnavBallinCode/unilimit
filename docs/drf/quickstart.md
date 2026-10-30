# Django REST Framework quickstart

`unilimit.integrations.drf` provides throttles that implement DRF's `BaseThrottle`
interface. They go wherever DRF throttles go (`throttle_classes`,
`DEFAULT_THROTTLE_CLASSES`) and reject through DRF's own `Throttled` exception, so your
`EXCEPTION_HANDLER` and renderers keep working. Unlike DRF's built-in throttles they
share exact counters across processes, atomically, in Redis.

They run **after DRF authentication**, so token, JWT and session users are all known.

## Install

```bash
pip install "unilimit[drf,redis]"
```

## Settings

```python
# settings.py
UNILIMIT = {"URL": "redis://localhost:6379/0"}

REST_FRAMEWORK = {
    "DEFAULT_THROTTLE_CLASSES": [
        "unilimit.integrations.drf.UniLimitAnonRateThrottle",
        "unilimit.integrations.drf.UniLimitUserRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "anon": "100/day",
        "user": "1000/day",
        "uploads": "10/hour",
    },
}
```

## Views

```python
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView
from django.urls import path

from unilimit import Rule
from unilimit.integrations.drf import (
    UniLimitAnonRateThrottle,
    UniLimitScopedRateThrottle,
    UniLimitThrottle,
    UniLimitUserRateThrottle,
)


class Profile(APIView):
    # Anonymous requests by IP ("anon"), authenticated requests by user ("user").
    throttle_classes = [UniLimitAnonRateThrottle, UniLimitUserRateThrottle]

    def get(self, request: Request) -> Response:
        return Response({"ok": True})


class Upload(APIView):
    throttle_classes = [UniLimitScopedRateThrottle]
    throttle_scope = "uploads"  # rate from DEFAULT_THROTTLE_RATES["uploads"]

    def post(self, request: Request) -> Response:
        return Response({"ok": True})


class Search(APIView):
    throttle_classes = [UniLimitThrottle]
    # Any number of UniLimit rules, inline or registered on the limiter by name.
    unilimit_rules = [
        Rule("search-burst", "5/second", key="user_or_ip"),
        Rule("search-daily", "500/day", key="user_or_ip"),
    ]

    def get(self, request: Request) -> Response:
        return Response({"results": []})


urlpatterns = [
    path("profile", Profile.as_view()),
    path("upload", Upload.as_view()),
    path("search", Search.as_view()),
]
```

## Throttle classes

| Class | Replaces | Identity |
|---|---|---|
| `UniLimitAnonRateThrottle` | `AnonRateThrottle` | Client IP, for anonymous requests only. |
| `UniLimitUserRateThrottle` | `UserRateThrottle` | The user, or the client IP when anonymous. |
| `UniLimitScopedRateThrottle` | `ScopedRateThrottle` | As `UniLimitUserRateThrottle`; scope from the view's `throttle_scope`. |
| `UniLimitRateThrottle` | `SimpleRateThrottle` | Subclass it: set `scope`, optionally `rate`, `key`. |
| `UniLimitThrottle` | (new) | The view's `unilimit_rules`, any `Rule`s. |

## Multiple throttles per view

**All UniLimit throttles of a view are consumed in one atomic batch.** A request rejected
by `UniLimitUserRateThrottle` consumes nothing from the others, which DRF's built-in
throttles cannot guarantee.

## Rejections

A throttled request raises DRF's `Throttled` exception: status 429 with `Retry-After`
and DRF's standard body, rendered by your `EXCEPTION_HANDLER` as usual. When the
limiter is configured with `fail_mode="closed"` and Redis is down, the request fails
with `ServiceUnavailable` (503). A deny-listed client or a missing identity with
`on_missing="deny"` raises `PermissionDenied` (403).

With `UniLimitMiddleware` installed, the rate-limit headers are added to DRF responses
too. See [Django errors and headers](../django/errors-and-headers.md).

## Custom identities

```python
from unilimit import RequestContext
from unilimit.integrations.drf import UniLimitRateThrottle


def organisation(ctx: RequestContext) -> str | None:
    return ctx.headers.get("x-organisation")


class OrganisationThrottle(UniLimitRateThrottle):
    scope = "org"
    key = staticmethod(organisation)  # `staticmethod`: it is a class attribute
```

Client IPs come from the limiter's `trusted_proxies`, **not** from DRF's `NUM_PROXIES`.
See [Django middleware](../django/middleware.md#client-ip-and-proxies).
