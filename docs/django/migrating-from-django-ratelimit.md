# Migrating from django-ratelimit

Two paths:

1. **Change the imports.** `unilimit.compat.django_ratelimit` keeps django-ratelimit's
   `@ratelimit(group=, key=, rate=, method=, block=)` and enforces it with UniLimit. This
   page covers that path.
2. **Move to the native [`@ratelimit`](decorators.md)** for new code: named rules shared
   across views, several rates in one atomic batch, and DRF-aware identities.

Either way, configure `UNILIMIT` (and preferably the middleware) as in the
[quickstart](quickstart.md). Counters stored in Django's cache by django-ratelimit are
not carried over; budgets start fresh.

## Change the imports

```python
from django.http import HttpRequest, JsonResponse
from django.urls import path

from unilimit.compat.django_ratelimit import UNSAFE, ratelimit

UNILIMIT = {"URL": "redis://localhost:6379/0"}


def staff_unlimited(group: str, request: HttpRequest) -> str | None:
    return None if request.user.is_staff else "5/m"  # None: no limit for this request


@ratelimit(key="ip", rate=staff_unlimited)
def search(request: HttpRequest) -> JsonResponse:
    return JsonResponse({"results": []})


@ratelimit(key="header:x-api-key", rate="100/h", method=UNSAFE)
def submit(request: HttpRequest) -> JsonResponse:
    return JsonResponse({"ok": True})


@ratelimit(key="ip", rate="3/m", block=False)
def comment(request: HttpRequest) -> JsonResponse:
    if getattr(request, "limited", False):  # counted, not rejected
        return JsonResponse({"error": "slow down"}, status=400)
    return JsonResponse({"ok": True})


urlpatterns = [path("search", search), path("submit", submit), path("comment", comment)]
```

The sixth anonymous `/search` request in a minute gets 429 with `Retry-After`. `/submit`
counts only `POST`, `PUT`, `PATCH` and `DELETE`. The fourth `/comment` request still
reaches the view, with `request.limited` set.

## What is supported

- `key`: `"ip"`, `"user"`, `"user_or_ip"`, `"header:<name>"`, `"get:<name>"`,
  `"post:<name>"`, a callable `key(group, request)` or a dotted path to one. As upstream,
  an empty value is one shared bucket.
- `rate`: `"5/m"`, `"100/2h"`, `(count, seconds)`, a callable `rate(group, request)` or a
  dotted path to one. Rates are read like django-ratelimit reads them: only the first
  letter of the period counts (`"5/month"` is five a minute) and no unit means seconds.
  A callable returning `None` leaves that request unlimited.
- `method`: one method, a list, `ALL` or `UNSAFE`.
- `block=False` counts without rejecting and sets `request.limited`, including when an
  earlier decorator on the same request was over its limit.
- `group`, defaulting to the same name django-ratelimit computes, so key and rate
  callables receive the values they expect.
- Function views and class-based views through `method_decorator`, sync and async.

## What changes

- **Rejections.** A blocked request gets UniLimit's rejection response: 429 with
  `Retry-After`, or your `UNILIMIT["REJECTION_VIEW"]`, which replaces `RATELIMIT_VIEW`
  and receives `(request, decision)`. `Ratelimited` is still importable but is not
  raised.
- **Storage.** Counters live in the `UNILIMIT` backend, not in a Django cache.
- **Algorithm.** UniLimit's sliding window counter, not fixed windows: no burst of twice
  the limit at a window edge.
- **Client IPs.** `"ip"` is the connection's address, resolved through the limiter's
  `trusted_proxies=` (instead of `RATELIMIT_IP_META_KEY`), with IPv6 grouped by /64 and
  IPv4 by `Limiter(ipv4_prefix=...)` (instead of the `RATELIMIT_*_MASK` settings).
- **Anonymous users.** `key="user"` rejects anonymous requests (no identity) instead of
  sharing one bucket; use `"user_or_ip"`. A key callable returning `None` is rejected the
  same way.
- **Each rate has its own counter**, as upstream, so stacked decorators on one view are
  independent limits. A decorator without `rate=` raises `ConfigurationError`.
- **Outages** follow UniLimit's `fail_mode` (open by default) instead of
  `RATELIMIT_FAIL_OPEN`.

## Not supported

Other `RATELIMIT_*` settings are not read: `RATELIMIT_ENABLE`, `RATELIMIT_USE_CACHE`,
`RATELIMIT_EXCEPTION_CLASS`, `RATELIMIT_HASH_ALGORITHM` and `RATELIMIT_CACHE_PREFIX`.
`is_ratelimited`, `get_usage` and django-ratelimit's middleware are not provided; the
native [middleware](middleware.md) covers application-wide limits.
