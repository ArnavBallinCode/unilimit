# Custom identities

A rule's `key` decides *who* a counter belongs to.

| `key` | Identity |
|---|---|
| `"ip"` | The client IP after [trusted-proxy](middleware.md#client-ip-and-proxies) resolution. IPv6 addresses are grouped by /64. |
| `"user"` | The user id from `identity=` or from Starlette authentication. |
| `"user_or_ip"` | The user if known, otherwise the IP. |
| `"api_key"` | The `X-API-Key` header, or the header named by `Limiter(api_key_header=...)`. |
| `"tenant"` | The header named by `Limiter(tenant_header=...)`. |
| `"global"` | One counter for everyone. |
| a callable | Your function of the request context. |
| a list | A fallback chain: the first identity that resolves wins. |

Identity values are never stored in Redis in plain text. Keys contain a keyed hash
(set `identity_secret=` in production).

```python
from fastapi import Depends, FastAPI

from unilimit import Limiter, RequestContext, Rule
from unilimit.integrations.fastapi import RateLimit, setup

limiter = Limiter.from_url(
    "redis://localhost:6379/0",
    api_key_header="X-API-Key",
    tenant_header="X-Tenant",
    identity_secret="change-me",  # keyed hashing of identities in Redis
)
app = FastAPI()
setup(app, limiter)

# API key first, then the client IP for requests without a key.
by_key = RateLimit("partner-api", rate="600/minute", key=["api_key", "ip"])


@app.get("/partner/orders", dependencies=[Depends(by_key)])
async def orders() -> list[str]:
    return []


# Per tenant, from a header.
by_tenant = RateLimit("tenant", rate="1000/minute", key="tenant")


@app.get("/tenant/usage", dependencies=[Depends(by_tenant)])
async def usage() -> dict[str, bool]:
    return {"ok": True}


# Your own function: here, per organisation in the path.
def organisation(ctx: RequestContext) -> str | None:
    return ctx.raw.path_params.get("org")


by_org = RateLimit("org-reports", rate="20/minute", key=organisation)


@app.get("/orgs/{org}/reports", dependencies=[Depends(by_org)])
async def org_reports(org: str) -> dict[str, str]:
    return {"org": org}


# Rules can be mixed: a per-organisation limit plus one ceiling for everyone,
# consumed atomically.
per_org_exports = Rule("org-exports", "5/minute", key=organisation)
ceiling = Rule("all-exports", "500/minute", key="global")


@app.get("/orgs/{org}/exports", dependencies=[Depends(RateLimit(per_org_exports, ceiling))])
async def org_exports(org: str) -> dict[str, str]:
    return {"org": org}
```

Identity functions receive a `RequestContext`:

- `ctx.method`, `ctx.path` and `ctx.route` (the route template);
- `ctx.headers`, with lower-case names;
- `ctx.peer`, the transport address (use `key="ip"` for the proxy-resolved client IP);
- `ctx.user_id`;
- `ctx.raw`, the Starlette request, for path parameters and `request.state`.

They must be synchronous and fast. Do lookups in a dependency and store the result on
`request.state`. Returning `None` means "no identity": the next key in the chain is
tried, then `on_missing` applies. Values must be strings or integers of at most 1,024
characters. Anything else is treated as missing and logged, without the value.
