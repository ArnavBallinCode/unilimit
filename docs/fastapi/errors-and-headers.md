# Errors and response headers

## Statuses

| Situation | Default status | Default body |
|---|---|---|
| A rate limit is exceeded | 429 | `{"detail": "Too Many Requests"}` |
| The client is on the deny list | 403 | `{"detail": "Forbidden"}` |
| No identity resolved and `on_missing="deny"` | 403 | `{"detail": "Forbidden"}` |
| Redis unavailable and `fail_mode="closed"` | 503 | `{"detail": "Service Unavailable"}` |

Bodies never include internal details such as Redis errors.

## Headers

| Header | Meaning |
|---|---|
| `X-RateLimit-Limit` | Capacity of the most restrictive counter for this request. |
| `X-RateLimit-Remaining` | Requests left in that counter. |
| `X-RateLimit-Reset` | Seconds until that counter is completely refilled. |
| `Retry-After` | On 429 only: whole seconds until a retry can succeed. |

They are added to every response, including responses your endpoints construct
themselves, streaming responses and 404s.

## Customising

```python
from fastapi import Depends, FastAPI

from unilimit import Decision, Limiter
from unilimit.http import ResponseConfig
from unilimit.integrations.fastapi import RateLimit, setup


def problem(decision: Decision) -> dict:
    """An RFC 9457-style body."""
    return {
        "type": "https://example.com/problems/rate-limited",
        "title": "Too many requests",
        "status": 429,
        "retry_after": decision.retry_after,
    }


limiter = Limiter.from_url("redis://localhost:6379/0")
app = FastAPI()
setup(
    app,
    limiter,
    response=ResponseConfig(
        body=problem,  # a dict, a string, or a function of the Decision
        rate_limited_status=429,
        unavailable_status=503,
        reset_format="delta",  # or "epoch" for a Unix timestamp
        legacy_headers=True,  # X-RateLimit-*
        ietf_headers=False,  # RateLimit-Policy / RateLimit (IETF draft-11), opt-in
    ),
)


@app.get("/x", dependencies=[Depends(RateLimit("x", rate="1/minute"))])
async def x() -> dict[str, bool]:
    return {"ok": True}
```

The IETF `RateLimit` and `RateLimit-Policy` fields are still an Internet-Draft, so
they are off by default. When enabled, they list every enforced rule. Partition keys
are never sent.

## Handling rejections in your own code

A `RateLimit` dependency raises `unilimit.integrations.fastapi.RateLimitExceeded`. It is
a FastAPI `HTTPException` carrying the full `Decision`, so you can register your own
handler, for example to add logging:

```python
import logging

from fastapi import Request
from fastapi.responses import JSONResponse

from unilimit.integrations.fastapi import RateLimitExceeded

log = logging.getLogger("app.ratelimit")


async def on_rate_limited(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RateLimitExceeded)
    log.info("rejected %s: %s", request.url.path, exc.decision.reason)
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.decision.reason.value},
        headers=exc.headers,
    )


app.add_exception_handler(RateLimitExceeded, on_rate_limited)
```

Rejections from the middleware (global rules) are rendered directly from
`ResponseConfig`, before your application runs.
