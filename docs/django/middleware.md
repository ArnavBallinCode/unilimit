# Middleware

`UniLimitMiddleware` runs, on every request and **before URL resolution**:

1. the limiter's exemptions (`exempt_paths`, `exempt`);
2. its access lists (`deny` first, then `allow`);
3. its `global_rules`.

Requests that end in a 404 count too, so scanning for URLs is limited.

```python
# settings.py
MIDDLEWARE = [
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "unilimit.integrations.django.UniLimitMiddleware",
]
```

```python
# ratelimits.py
from django.http import HttpRequest, JsonResponse
from django.urls import path

from unilimit import AccessList, Limiter, Rule, TrustedProxies

limiter = Limiter.from_url(
    "redis://localhost:6379/0",
    global_rules=[
        Rule("site", "300/minute"),  # per client IP
        Rule("burst", "20/second", algorithm="token_bucket", burst=40),
    ],
    exempt_paths=["/health", "/static/*"],
    deny=AccessList(networks=["198.51.100.0/24"]),
    # Only trust X-Forwarded-For when the request comes from your load balancer.
    trusted_proxies=TrustedProxies(["10.0.0.0/8"]),
)

UNILIMIT = {"LIMITER": limiter}


def health(request: HttpRequest) -> JsonResponse:
    return JsonResponse({"ok": True})


def feed(request: HttpRequest) -> JsonResponse:
    return JsonResponse({"items": []})


urlpatterns = [path("health", health), path("feed", feed)]
```

## Middleware order

The middleware has to run **after** `SessionMiddleware` and `AuthenticationMiddleware`
if a global rule is keyed by `"user"`: it reads `request.user`. Without authentication
middleware the user is unknown and the rule's `on_missing` setting applies (by default
`"deny"`: the request gets a 403).

Put it **before** middleware that you want protected from floods, such as expensive
custom middleware. Anything listed before UniLimit runs for requests that UniLimit then
rejects.

Order of enforcement in one request:

1. **`UniLimitMiddleware`**: exemptions, access lists, global rules.
2. **`@ratelimit` decorators**, when the view is called. With several decorators the
   outermost runs first.
3. **DRF throttles**, after DRF has authenticated the request.

Each step is one atomic batch: all of its rules are consumed, or none. The first step
that rejects ends the request. Quota consumed by earlier steps stays consumed, because
those steps admitted the request.

**A counter is never consumed twice in one request.** If the middleware and a view use
the same rule for the same identity, the view skips it.

Response headers describe the most restrictive counter across every step.

## Client IP and proxies

By default the client IP is `REMOTE_ADDR`, and forwarding headers are ignored, because
any client can send them. Behind a proxy or load balancer, list its networks in
`TrustedProxies`. UniLimit then walks `X-Forwarded-For` from right to left, skipping
trusted hops, and uses the first untrusted address. Entries a client prepends cannot
change its identity.

Do not combine this with Django's own `SECURE_PROXY_SSL_HEADER`-style rewriting of
`REMOTE_ADDR` by another middleware: configure one mechanism only.

## Sync and async

The middleware is both sync and async capable. Django adapts it to the server:

- Under **ASGI** it uses the async Redis client, resolves the user with
  `await request.auser()`, and never blocks the event loop.
- Under **WSGI** it uses the sync client.

If any middleware in the chain is sync-only, Django adapts the chain around it. That is
correct, but the sync parts run in a thread; UniLimit itself stays non-blocking.
