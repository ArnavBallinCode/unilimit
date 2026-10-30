# Application-wide limits (middleware)

`setup(app, limiter)` installs `UniLimitMiddleware`. On every HTTP request, **before
routing**, it applies, in order:

1. the limiter's exemptions (`exempt_paths`, `exempt`);
2. its access lists (`deny` first, then `allow`);
3. its `global_rules`.

Requests to unknown paths count too, so scanning for URLs is limited.

```python
from fastapi import FastAPI

from unilimit import AccessList, Limiter, Rule, TrustedProxies
from unilimit.integrations.fastapi import setup

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

app = FastAPI()
setup(app, limiter)


@app.get("/health")
async def health() -> dict[str, bool]:
    return {"ok": True}


@app.get("/feed")
async def feed() -> list[str]:
    return []
```

## Client IP and proxies

By default the client IP is the address of the TCP peer, and forwarding headers are
ignored, because any client can send them. If your application runs behind a proxy or
load balancer, list its networks in `TrustedProxies`. UniLimit then walks
`X-Forwarded-For` from right to left, skipping trusted hops, and uses the first
untrusted address. Entries a client prepends cannot change its identity.

If uvicorn already rewrites the client address (`--proxy-headers` with
`--forwarded-allow-ips`), configure only one of the two mechanisms.

## Users in global rules

Before routing, FastAPI dependencies have not run, so the authenticated user is not
known yet. Global rules can still be keyed by user in two ways:

- run Starlette's `AuthenticationMiddleware` before UniLimit (add it *after* calling
  `setup`, so that it is the outer middleware); its `scope["user"]` is used;
- pass `setup(app, limiter, user=lambda conn: ...)` to read the user id from the
  connection yourself.

User limits that depend on FastAPI authentication dependencies belong in a
[`RateLimit` dependency](dependencies.md) instead.

## Order of enforcement

1. **Middleware**: exemptions, access lists, global rules.
2. **`RateLimit` dependencies**, in FastAPI's dependency order: application-level
   (`FastAPI(dependencies=[...])`), router-level (outer to inner), route-level
   (`dependencies=[...]`), then endpoint parameters.

Each step is one atomic batch: all of its rules are consumed, or none. The first step
that rejects ends the request. Quota consumed by earlier steps stays consumed, because
those steps admitted the request.

**A counter is never consumed twice in one request.** If the middleware and a
dependency use the same rule for the same identity, the dependency skips it. The same
rule keyed differently, for example `"user_or_ip"` keyed by IP in the middleware and by
user in a dependency, means two different counters, and each is consumed once.

Response headers describe the most restrictive counter across every step.

## WebSockets

The middleware passes WebSocket connections through unchanged. Limit WebSocket
messages with `limiter.aconsume(...)` inside your handler.
