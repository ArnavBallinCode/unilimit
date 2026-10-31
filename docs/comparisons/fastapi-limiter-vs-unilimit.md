# FastAPI-Limiter and UniLimit

Reviewed 2026-11-01 against the
[upstream README](https://github.com/long2ice/fastapi-limiter).
FastAPI-Limiter provides FastAPI dependencies, custom identifiers and callbacks, and
WebSocket rate limiting. Its current README uses PyrateLimiter; older Redis-based
initialization examples describe a different API generation. Inspect your installed
version before migrating or comparing results.

UniLimit also uses FastAPI dependencies, but shares policy and identity handling with
Django, DRF, Starlette, and plain Python. It has separate active-connection leases and
message-rate budgets, plus native multi-rule batch evaluation. A WebSocket rate limiter
alone should not be confused with a distributed open-connection limit.

The upstream README's default identifier uses the first forwarded IP when present.
UniLimit offers explicit trusted-proxy configuration; applications using either library
must verify how their server and proxies handle forwarding headers.

Choose the narrower library when its API and PyrateLimiter behavior fit your application.
Evaluate UniLimit when cross-framework policy, batch semantics, or connection leases
matter. See the [migration guide](../migration.md) and [WebSocket guide](../fastapi/websockets.md).
