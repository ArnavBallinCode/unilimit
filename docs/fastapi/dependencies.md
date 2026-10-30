# Endpoint and router limits (dependencies)

`RateLimit` is a FastAPI dependency. It can describe its rule inline, refer to rules
registered on the limiter by name, or take `Rule` objects.

```python
from fastapi import APIRouter, Depends, FastAPI

from unilimit import Decision, Limiter, Rule
from unilimit.integrations.fastapi import RateLimit, setup

reports = Rule("reports", "5/minute")  # a reusable policy

limiter = Limiter.from_url(
    "redis://localhost:6379/0",
    rules=[Rule("free-tier", "100/hour")],  # registered by name
)
app = FastAPI()
setup(app, limiter)


# Inline rule: a name and a rate.
@app.get("/search", dependencies=[Depends(RateLimit("search", rate="10/minute"))])
async def search() -> dict[str, bool]:
    return {"ok": True}


# A rule registered on the limiter, by name.
@app.get("/catalog", dependencies=[Depends(RateLimit("free-tier"))])
async def catalog() -> dict[str, bool]:
    return {"ok": True}


# The same Rule object on two routes: one shared counter.
@app.get("/reports/daily", dependencies=[Depends(RateLimit(reports))])
async def daily() -> dict[str, bool]:
    return {"ok": True}


@app.get("/reports/weekly", dependencies=[Depends(RateLimit(reports))])
async def weekly() -> dict[str, bool]:
    return {"ok": True}


# Several rules in one dependency are consumed atomically: all or nothing.
stacked = RateLimit(
    Rule("export-burst", "2/minute"),
    Rule("export-daily", "20/day"),
)

# The same, written in the multi-limit syntax of slowapi / Flask-Limiter / limits.
uploads = RateLimit("uploads", rate="2/minute;20/day")


@app.post("/upload", dependencies=[Depends(uploads)])
async def upload() -> dict[str, bool]:
    return {"ok": True}


@app.post("/export", dependencies=[Depends(stacked)])
async def export() -> dict[str, bool]:
    return {"ok": True}


# The dependency returns the Decision, if the endpoint wants it.
@app.get("/quota")
async def quota(decision: Decision = Depends(RateLimit("quota", rate="30/minute"))) -> dict:
    return {"remaining": decision.results[0].remaining}


# Router-level: every route of the router, each with its own counter.
admin = APIRouter(
    prefix="/admin",
    dependencies=[Depends(RateLimit("admin", rate="60/minute", per_route=True))],
)


@admin.get("/users")
async def users() -> list[str]:
    return []


@admin.get("/audit")
async def audit() -> list[str]:
    return []


app.include_router(admin)
```

## Rate strings

`"10/second"`, `"10 per second"`, `"5/30s"`, `"100/day"`, `"1/month"` (30 days) and
`"1/year"` (360 days). Several rates separated by `;`, `,` or `|`, as in
`"10/second;1000/day"`, create one rule per rate, each with its own counter, and they
are consumed together. This is the syntax of the `limits` library, so rate strings from
slowapi and Flask-Limiter configurations keep their meaning (UniLimit additionally
accepts compact windows such as `"5/30s"`).

## Sharing and naming

A rule's **name** identifies its counter. Two inline `RateLimit("search", rate=...)`
dependencies with the same name share one counter. `per_route=True` gives each route
template its own counter instead, which is useful on routers. Without it, a router-level
limit is shared by all the routes of the router.

## Inline rule options

Inline rules accept the options of `Rule`:

- `algorithm`: `"sliding_window"` (default), `"fixed_window"` or `"token_bucket"`;
- `burst`: the token bucket's capacity;
- `cost`: an integer, or a callable for weighted requests;
- `methods` and `when`: restrict when the rule applies;
- `on_missing`: what happens when no identity resolves;
- `fail_mode`: `"open"` or `"closed"` when Redis is unavailable;
- `action="log"`: shadow mode, which counts but never blocks.

`key` selects the identity. The default is `"ip"`, or `"user"` when `identity=` is given
(see [Authentication](authentication.md)).

```python
login = RateLimit(
    "login",
    rate="5/minute",
    algorithm="fixed_window",
    methods={"POST"},
    fail_mode="closed",  # refuse logins rather than allow unlimited guessing
)


@app.post("/login", dependencies=[Depends(login)])
async def do_login() -> dict[str, bool]:
    return {"ok": True}
```

## Without `setup()`

`RateLimit(..., limiter=limiter)` works without the middleware. Rejections still become
429 responses with `Retry-After`, through FastAPI's `HTTPException` handling. However,
success headers then reach only responses that FastAPI builds itself; endpoints that
return a `Response` object directly do not get them. Global rules are not enforced
either. Use `setup()` unless you have a reason not to.
