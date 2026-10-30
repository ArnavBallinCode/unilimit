# Errors and response headers

## Statuses

| Situation | Default status |
|---|---|
| A rate limit is exceeded | 429 |
| The client is on the deny list | 403 |
| No identity resolved and `on_missing="deny"` | 403 |
| Redis unavailable and `fail_mode="closed"` | 503 |

Bodies never include internal details such as Redis errors.

## Headers

| Header | Meaning |
|---|---|
| `X-RateLimit-Limit` | Capacity of the most restrictive counter for this request. |
| `X-RateLimit-Remaining` | Requests left in that counter. |
| `X-RateLimit-Reset` | Seconds until that counter is completely refilled. |
| `Retry-After` | On 429 only: whole seconds until a retry can succeed. |

They are added to every response that passes the middleware, including streaming
responses and 404s. Headers your view sets itself are kept. Without the middleware,
`@ratelimit` adds them to the responses of the views it decorates.

## Customising the response

```python
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.urls import path

from unilimit import Decision, Limiter
from unilimit.http import ResponseConfig
from unilimit.integrations.django import ratelimit


def problem(decision: Decision) -> dict:
    """An RFC 9457-style body."""
    return {
        "type": "https://example.com/problems/rate-limited",
        "title": "Too many requests",
        "status": 429,
        "retry_after": decision.retry_after,
    }


def rate_limited(request: HttpRequest, decision: Decision) -> HttpResponse:
    """Your own rejection view: any response. Missing rate-limit headers are added."""
    return JsonResponse({"error": decision.reason.value}, status=429)


limiter = Limiter.from_url("redis://localhost:6379/0")

UNILIMIT = {
    "LIMITER": limiter,
    "RESPONSE": ResponseConfig(
        body=problem,  # a dict, a string, or a function of the Decision
        rate_limited_status=429,
        unavailable_status=503,
        reset_format="delta",  # or "epoch" for a Unix timestamp
        retry_after_format="seconds",  # or "http-date"
        legacy_headers=True,  # X-RateLimit-*
        ietf_headers=False,  # RateLimit-Policy / RateLimit (IETF draft), opt-in
    ),
    # "REJECTION_VIEW": rate_limited,  # takes over rendering entirely
}


@ratelimit("x", rate="1/minute")
def x(request: HttpRequest) -> JsonResponse:
    return JsonResponse({"ok": True})


urlpatterns = [path("x", x)]
```

`RESPONSE` and `REJECTION_VIEW` accept dotted paths too. With a `REJECTION_VIEW`, the
view builds the response; UniLimit still adds the rate-limit headers it is missing.

The `Decision` passed to your code carries the reason, the rule that rejected and
`retry_after`. Identity values are never in it.

## Inspecting the decision in a view

`request_state(request)` returns the `RequestState` of the current request, with every
`Decision` made so far, for logging or custom headers.
