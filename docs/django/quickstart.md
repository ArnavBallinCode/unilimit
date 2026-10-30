# Django quickstart

UniLimit's Django integration has three parts:

- **`UniLimitMiddleware`** applies application-wide rules (and the limiter's exemptions
  and access lists) before URL resolution, and adds rate-limit headers to every
  response.
- **`@ratelimit(...)`** limits one view. It works on sync and async function views and
  on class-based views.
- **`settings.UNILIMIT`** says which limiter to use.

For Django REST Framework throttles see the [DRF quickstart](../drf/quickstart.md).

## Install

```bash
pip install "unilimit[django,redis]"
```

## A complete project

```python
# settings.py
from unilimit import Rule

INSTALLED_APPS = [
    # ...
    "unilimit.integrations.django",  # optional: `manage.py check` validates UniLimit
]
MIDDLEWARE = [
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "unilimit.integrations.django.UniLimitMiddleware",  # after authentication
]
UNILIMIT = {
    "URL": "redis://localhost:6379/0",
    "OPTIONS": {"global_rules": [Rule("site", "100/minute")]},
}
```

```python
# views.py
from django.http import HttpRequest, JsonResponse
from django.urls import path

from unilimit.integrations.django import ratelimit


@ratelimit("search", rate="10/minute")
def search(request: HttpRequest) -> JsonResponse:
    return JsonResponse({"q": request.GET.get("q", "")})


@ratelimit("report", rate="5/minute")
async def report(request: HttpRequest) -> JsonResponse:  # async views work the same way
    return JsonResponse({"ok": True})


def status(request: HttpRequest) -> JsonResponse:
    return JsonResponse({"ok": True})


urlpatterns = [path("search", search), path("report", report), path("status", status)]
```

Every response carries the headers of the most restrictive counter that applied:

```text
X-RateLimit-Limit: 10
X-RateLimit-Remaining: 9
X-RateLimit-Reset: 60
```

The eleventh `/search` request within the window is rejected before the view runs:

```text
HTTP/1.1 429 Too Many Requests
Retry-After: 54
X-RateLimit-Limit: 10
X-RateLimit-Remaining: 0
X-RateLimit-Reset: 54
```

`/status` has no view-level limit, so only the global `site` rule applies to it.

## Where to go next

- [Middleware](middleware.md): global rules, exemptions, deny lists, proxies, ordering.
- [View decorators](decorators.md): inline and named rules, several rules per view.
- [Authentication and identities](authentication.md).
- [Redis configuration and lifecycle](redis.md).
- [Errors and response headers](errors-and-headers.md).
- [Supported versions](versions.md).
