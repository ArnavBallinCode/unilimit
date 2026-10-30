# Authentication and identities

A rule's `key` decides *who* a counter belongs to.

| `key` | Identity |
|---|---|
| `"ip"` | The client IP after [trusted-proxy](middleware.md#client-ip-and-proxies) resolution. IPv6 addresses are grouped by /64. |
| `"user"` | `request.user.pk` of an authenticated user. |
| `"user_or_ip"` | The user if authenticated, otherwise the client IP. |
| `"api_key"` | The `X-API-Key` header, or the header named by `Limiter(api_key_header=...)`. |
| `"tenant"` | The header named by `Limiter(tenant_header=...)`. |
| `"global"` | One counter for everyone. |
| `"header:X-Name"` | The value of any request header. |
| a callable | Your function of the `RequestContext`. |
| a list | A fallback chain: the first identity that resolves wins. |

Identity values are never stored in Redis in plain text. Keys contain a keyed hash
(set `identity_secret=` in production).

```python
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, JsonResponse
from django.urls import path

from unilimit import Limiter, RequestContext, Rule
from unilimit.integrations.django import ratelimit

limiter = Limiter.from_url(
    "redis://localhost:6379/0",
    api_key_header="X-API-Key",
    tenant_header="X-Tenant",
    identity_secret="change-me",  # keyed hashing of identities in Redis
)
UNILIMIT = {"LIMITER": limiter}


@ratelimit("profile", rate="30/minute", key="user")
@login_required
def profile(request: HttpRequest) -> JsonResponse:
    return JsonResponse({"user": request.user.pk})


@ratelimit("articles", rate="60/minute", key="user_or_ip")
def articles(request: HttpRequest) -> JsonResponse:
    return JsonResponse({"articles": []})


# API key first, then the client IP for requests without a key.
@ratelimit("partner", rate="600/minute", key=["api_key", "ip"])
def partner(request: HttpRequest) -> JsonResponse:
    return JsonResponse({"orders": []})


@ratelimit("tenant", rate="1000/minute", key="tenant")
def tenant_usage(request: HttpRequest) -> JsonResponse:
    return JsonResponse({"ok": True})


# Your own function: here, per organisation in the query string.
def organisation(ctx: RequestContext) -> str | None:
    return ctx.raw.GET.get("org")  # ctx.raw is the Django request


@ratelimit("org-reports", rate="20/minute", key=organisation)
def org_reports(request: HttpRequest) -> JsonResponse:
    return JsonResponse({"org": request.GET.get("org")})


@ratelimit(Rule("lenient", "5/minute", key="user", on_missing="skip"))
def lenient(request: HttpRequest) -> JsonResponse:  # anonymous requests are not limited
    return JsonResponse({"ok": True})


urlpatterns = [
    path("profile", profile),
    path("articles", articles),
    path("partner", partner),
    path("tenant", tenant_usage),
    path("org-reports", org_reports),
    path("lenient", lenient),
]
```

## Which user?

`"user"` and `"user_or_ip"` read `request.user`, so they need Django's
`AuthenticationMiddleware` (or any middleware that sets `request.user`).

- Under **WSGI** the lazy `request.user` is evaluated synchronously.
- Under **ASGI** UniLimit awaits `request.auser()`, so a database-backed session lookup
  never blocks the event loop.

`@ratelimit` runs when the view is called, after Django's middleware, so it sees the same
`request.user` as the view. A `key="user"` rule applies `on_missing` to anonymous
requests (by default 403), before `@login_required` can redirect them. Use
`key="user_or_ip"` if anonymous requests should be limited by IP instead.

For DRF token, JWT or other DRF authentication, `request.user` is not set until DRF
authenticates, which happens inside the view. Use the
[DRF throttles](../drf/quickstart.md), which run after DRF authentication.

## Missing identities

Identity functions return `None` for "no identity". The next key in a chain is tried,
then `on_missing` applies:

| `on_missing` | Behaviour |
|---|---|
| `"deny"` (default) | 403. |
| `"skip"` | The rule is not applied to this request. |
| `"raise"` | `IdentityError`, a server error. |

Set it on the limiter and override it per rule. Values must be strings or integers of at
most 1,024 characters; anything else is treated as missing and logged, without the value.

Identity functions must be synchronous and fast. Do lookups in middleware and store the
result on the request.
