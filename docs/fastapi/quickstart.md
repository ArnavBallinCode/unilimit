# FastAPI quickstart

UniLimit's FastAPI integration has two parts:

- **`setup(app, limiter)`** installs a pure ASGI middleware. It applies
  application-wide rules before routing, adds rate-limit headers to every response
  (including responses your endpoints build themselves), and closes Redis clients when
  the application shuts down.
- **`RateLimit(...)`** is a FastAPI dependency for endpoint-level and router-level
  limits. It runs after FastAPI has resolved your authentication dependencies.

## Install

```bash
pip install "unilimit[fastapi,redis]"
```

## A complete application

```python
from fastapi import Depends, FastAPI

from unilimit import Limiter, Rule
from unilimit.integrations.fastapi import RateLimit, setup

limiter = Limiter.from_url(
    "redis://localhost:6379/0",
    # Applied to every request by the middleware, keyed by client IP.
    global_rules=[Rule("site", "100/minute")],
)

app = FastAPI()
setup(app, limiter)


@app.get("/search", dependencies=[Depends(RateLimit("search", rate="10/minute"))])
async def search(q: str = "") -> dict[str, str]:
    return {"q": q}


@app.get("/status")
def status() -> dict[str, bool]:  # sync endpoints work the same way
    return {"ok": True}
```

Every response carries the headers of the most restrictive counter that applied:

```text
X-RateLimit-Limit: 10
X-RateLimit-Remaining: 9
X-RateLimit-Reset: 60
```

The eleventh `/search` request within the window is rejected:

```text
HTTP/1.1 429 Too Many Requests
Retry-After: 54
X-RateLimit-Limit: 10
X-RateLimit-Remaining: 0
X-RateLimit-Reset: 54

{"detail": "Too Many Requests"}
```

## What to read next

- [Middleware](middleware.md): application-wide limits and the order of enforcement.
- [Dependencies](dependencies.md): endpoint, router and shared limits.
- [Authentication](authentication.md): limiting by authenticated user.
- [Custom identities](identity.md): API keys, tenants and your own functions.
- [WebSockets](websockets.md): connection and message limits.
- [Redis and lifecycle](redis.md): connections, clusters and shutdown.
- [Errors and headers](errors-and-headers.md): status codes, bodies and headers.
