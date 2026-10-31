# Migrating from SlowAPI

Two paths:

1. **Change the imports.** `unilimit.compat.slowapi` keeps SlowAPI's `Limiter`,
   decorators, middleware and 429 handler, and enforces them with UniLimit. This page
   covers that path.
2. **Move to the native API** for new code: `Depends(RateLimit(...))` and
   `setup(app, limiter)`, described in the [quickstart](quickstart.md). It adds
   authenticated identities, WebSocket limits and configurable responses.

Counters already stored by SlowAPI are not carried over; budgets start fresh.

## Change the imports

```python
from fastapi import FastAPI, Request, Response

from unilimit.compat.slowapi import (
    Limiter,
    RateLimitExceeded,
    SlowAPIMiddleware,
    _rate_limit_exceeded_handler,
    get_remote_address,
)

limiter = Limiter(
    key_func=get_remote_address,
    default_limits=["10/minute"],
    storage_uri="redis://localhost:6379/0",
    headers_enabled=True,
)
app = FastAPI()
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)


@app.get("/search")
@limiter.limit("5/minute;100/day")
async def search(request: Request, response: Response) -> dict[str, str]:
    return {"status": "ok"}


@app.get("/items")  # no decorator: the default limit applies
async def items() -> list[str]:
    return []


@app.get("/health")
@limiter.exempt
async def health() -> dict[str, str]:
    return {"status": "ok"}
```

The sixth `/search` request in a minute returns 429 with
`{"error": "Rate limit exceeded: 5 per 1 minute"}` and `Retry-After`. Successful
responses carry `X-RateLimit-*` headers because `headers_enabled=True`; as in SlowAPI,
an endpoint that returns a plain value needs a `response: Response` parameter for them.

| SlowAPI import | Replacement |
|---|---|
| `slowapi.Limiter` | `unilimit.compat.slowapi.Limiter` |
| `slowapi.util.get_remote_address` | Same name. It becomes UniLimit's `"ip"` key: `trusted_proxies=` applies and IPv6 clients are grouped by /64. |
| `slowapi.util.get_ipaddr` | Not provided: it trusts `X-Forwarded-For` from anyone. Pass `trusted_proxies=TrustedProxies([...])` instead. |
| `slowapi.errors.RateLimitExceeded`, `slowapi._rate_limit_exceeded_handler` | Same names. |
| `slowapi.middleware.SlowAPIMiddleware`, `SlowAPIASGIMiddleware` | Same names (both pure ASGI). |

## What is supported

- `limit(...)` with a rate string (several rates with `;`), or a callable returning one,
  optionally taking `key`; `key_func`, `per_method`, `methods`, `error_message`,
  `exempt_when`, integer or callable `cost`, `override_defaults`.
- `shared_limit(..., scope="name")` with a string scope, and `exempt`.
- `default_limits` (one counter per endpoint, for endpoints without their own `limit`)
  and `application_limits` (one counter for the whole application), enforced by
  `SlowAPIMiddleware`.
- `headers_enabled`, `enabled` (also settable later: `limiter.enabled = False`),
  `retry_after="http-date"`, `storage_uri`, `storage_options` and
  `in_memory_fallback_enabled`.
- `strategy="fixed-window"` or `"sliding-window-counter"`.
- Any other keyword goes to [`Limiter.from_url`](redis.md), for example
  `trusted_proxies=`, `namespace=` or `identity_secret=`. Sync and async endpoints work.

## What changes

- **Atomic batches.** Stacked `limit` decorators on one endpoint, and every rate of a
  `"5/minute;100/day"` string, are consumed together: a rejected request uses none of
  them. Middleware limits and decorator limits are separate steps.
- **Default algorithm.** UniLimit's sliding window counter, which avoids twice the limit
  at window edges. Pass `strategy="fixed-window"` to keep SlowAPI's default.
- **Counters per endpoint**, not per URL. `key_style="url"` is rejected because varying
  a path parameter would give a client a fresh budget.
- **Empty keys are refused.** A `key_func` returning `""` or `None` answers 403; SlowAPI
  skips the limit.
- **Backend outages** follow UniLimit's `fail_mode` (open by default; pass
  `fail_mode="closed"` for 503) instead of `swallow_errors`.
- **Success headers** are `X-RateLimit-Limit`, `-Remaining` and `-Reset`; `Retry-After`
  is sent on rejections only.

## Not supported

These raise `ConfigurationError` when the `Limiter` is created, so nothing is silently
ignored: `strategy="moving-window"`, `key_style="url"`, `swallow_errors`,
`in_memory_fallback` (separate fallback limits), `key_prefix` (use `namespace=`),
`auto_check` and `config_filename`. Callable `shared_limit` scopes are rejected too.

Not provided at all: settings from `.env` files or `RATELIMIT_*` variables,
`limiter.reset()`, decorators on WebSocket endpoints (use
[`WebSocketRateLimit`](websockets.md)) and `get_ipaddr`.
