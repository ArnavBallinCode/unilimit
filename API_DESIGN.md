# UniLimit — Public API Design

Status: **Approved at the M1 review, with revisions.** Signatures are normative for v0.1. Sections marked [M3]–[M6] describe later milestones. The core API (§2, §3) is implemented in Milestone 2.
Architecture rationale: [ARCHITECTURE.md](ARCHITECTURE.md).

Design rules applied throughout:

1. **One object, both worlds.** A single `Limiter` serves sync and async callers. Async methods use an `a` prefix (`consume` / `aconsume`), following Django's convention (`get` / `aget`).
2. **Strings for the common case, objects for the rest.** `"100/minute"` is enough to get started, while `Rule(...)` provides full control. A string argument is **always a rate**, never a rule name, so it is never ambiguous.
3. **Configuration errors surface at import or startup time**, never on the first request.
4. **Integrations add no decision logic.** They translate a request into a `RequestContext` and a `Decision` into a response.

---

## 1. Installation

```bash
pip install unilimit              # core + in-memory backend, zero dependencies
pip install "unilimit[redis]"     # + redis>=5.0
pip install "unilimit[django]"    # + Django>=5.2
pip install "unilimit[fastapi]"   # + fastapi, starlette
pip install "unilimit[all]"
```

`unilimit` was available on PyPI as of 2026-10-30, as were the close variants `uni-limit`, `unilimiter` and `unlimit`. Reserve the name before announcing the project.

---

## 2. Core API

### 2.1 Creating a limiter

```python
from unilimit import Limiter

limiter = Limiter.from_url("redis://localhost:6379/0", namespace="myapp")
limiter = Limiter.from_url("memory://")  # local dev / tests
limiter = Limiter.from_url("redis://node1:7000", cluster=True)  # Redis Cluster
```

Explicit construction, with every option shown at its default:

```python
from unilimit import Limiter, TrustedProxies
from unilimit.backends.redis import RedisBackend

limiter = Limiter(
    backend=RedisBackend.from_url(
        "rediss://redis.internal:6379/0",
        socket_timeout=0.25,
        socket_connect_timeout=0.25,
        cooldown=1.0,  # fail fast for 1s after a connection failure
        max_connections=None,
    ),
    namespace="unilimit",  # Redis key prefix; isolates apps sharing one Redis
    rules=(),  # named, reusable rules (registry)
    global_rules=(),  # rules applied to every request by the middleware
    fail_mode="open",  # "open" | "closed"; per-rule override available
    on_missing="deny",  # "deny" | "raise" | "skip"; per-rule override available
    trusted_proxies=None,  # TrustedProxies(...) — default trusts nothing
    ipv6_prefix=64,  # IPv6 clients are limited per /64 by default
    identity_secret=None,  # str | bytes; keyed hashing of identities (recommended)
    api_key_header="X-API-Key",
    tenant_header=None,
    allow=None,  # AccessList(networks=[...], users=[...], api_keys=[...])
    deny=None,  # AccessList(...); checked before `allow`, so deny wins
    exempt_paths=(),  # e.g. ("/health", "/metrics/*")
    exempt=None,  # Callable[[RequestContext], bool]
    response=ResponseConfig(),  # §4  [M6]
    hooks=(),  # §7  [M6]
)
```

`Limiter.from_url` supports `memory://` in M2, and `redis://`, `rediss://` and `unix://` from M3 onwards. An unsupported scheme raises `ConfigurationError`.

Users who already manage redis-py clients (Sentinel, custom TLS, shared pools) can pass them in:

```python
RedisBackend(client=redis.Redis(...), async_client=redis.asyncio.Redis(...))
```

Clients built from a URL are created **lazily**. The async client is created **once per event loop** (see ARCHITECTURE §9.5).

Lifecycle:

```python
limiter.close()  # sync apps / atexit
await limiter.aclose()  # async apps, e.g. inside a FastAPI lifespan
```

### 2.2 Rules

```python
from unilimit import Rule

Rule("free-tier", rate="100/minute", key="user")
Rule("free-tier", limit=100, window="1m", key="user")  # equivalent
Rule(
    name="search",
    rate="20/second",
    algorithm="token_bucket",  # "sliding_window" (default) | "fixed_window" | "token_bucket"
    burst=40,  # token_bucket only; default = limit
    key=["api_key", "ip"],  # fallback chain: first identity that resolves
    methods={"GET", "POST"},  # None = all
    paths=("/api/search*",),  # glob patterns (fnmatch; "*" also matches "/"); None = all
    when=lambda ctx: ctx.headers.get("x-plan") == "free",
    cost=1,  # int, or Callable[[RequestContext], int]
    on_missing=None,  # "deny" | "raise" | "skip"; None = limiter default ("deny")
    fail_mode=None,  # None = inherit limiter default
    action="reject",  # "reject" | "log" (shadow mode: count, never block)
)
```

- **Rate grammar:** `"<int>/<unit>"` or `"<int>/<n><unit>"`. Units are `s|sec|second`, `m|min|minute`, `h|hour`, `d|day`, with plurals accepted (`"10/5s"`, `"1000/day"`, `"5/30 seconds"`). `window` also accepts `int` seconds and `timedelta`.
- **Validation** happens at construction time and raises `ConfigurationError`. Cases: rate given together with limit/window; limit < 1; window < 1 ms; `W/limit` < 1 µs; `burst` set for a non-token-bucket rule; a static `cost` above capacity; an invalid name; an unknown key spec.
- Rules are **frozen, hashable dataclasses**, so they are safe to share across routes and threads.
- The rule name together with the algorithm identifies the counter (ARCHITECTURE §5). Using the same `Rule` object on two routes **shares** its counters. Two *different* rules with the same name and algorithm but different limits make `evaluate` raise `ConfigurationError` if both apply to the same request.

Key specs:

| Spec | Identity |
|---|---|
| `"ip"` | Client IP after trusted-proxy resolution. IPv6 is aggregated to /64 by default. |
| `"user"` | Authenticated user id (`RequestContext.user_id`; framework-specific resolution, ARCHITECTURE §11.2). |
| `"user_or_ip"` | Shorthand for `("user", "ip")`: user id if authenticated, else the trusted client IP. |
| `"api_key"` | Value of `limiter.api_key_header`. Hashed before storage. |
| `"tenant"` | Value of `limiter.tenant_header` (a `ConfigurationError` if it is not configured). |
| `"global"` | A single shared bucket for all callers. |
| `callable` | `(RequestContext) -> str \| int \| None` |
| sequence | Fallback chain of the above, tried in order. |

When no source in the chain resolves, `on_missing` decides:
- `"deny"` (default): reject with `Reason.MISSING_IDENTITY`; nothing is consumed.
- `"raise"`: raise `IdentityError`.
- `"skip"`: omit the rule for this request.

Every case logs a WARNING naming the rule. Shadow rules always skip.

Tier-specific limits are expressed as separate rules with `when`, not as dynamic limits. This keeps each counter's capacity well defined:

```python
def plan(ctx):
    return ctx.raw.state.plan  # set by your auth layer


limiter = Limiter.from_url(
    url,
    global_rules=[
        Rule("free", rate="100/minute", key="user", when=lambda c: plan(c) == "free"),
        Rule("pro", rate="2000/minute", key="user", when=lambda c: plan(c) == "pro"),
        Rule("anon", rate="20/minute", key="ip", when=lambda c: c.user_id is None),
    ],
)
```

### 2.3 Framework-independent calls

The M1 design target was `limiter.check(key=..., rule=...)`. The API uses two verbs that make side effects explicit:

| Method | Semantics |
|---|---|
| `consume` / `aconsume` | **Atomically** evaluates the rule and, only if it allows, commits the consumption. A rejected call changes nothing. |
| `peek` / `apeek` | Evaluates without **ever** mutating state. It is informational only, for example a "my quota" endpoint. Never use it as an admission check followed by `consume`; that is a check-then-act race. |
| `reset` / `areset` | Deletes the counter. |

```python
from unilimit import Identity

result = limiter.consume("api_limit", "user:123")  # consumes 1; sync
result = await limiter.aconsume("api_limit", "user:123")  # async
result = limiter.consume(Rule("x", rate="5/s"), "job-42", cost=3)
result = limiter.consume("api_limit", Identity("user", "123"))  # same counter as key="user"

if not result.allowed:
    print(result.retry_after)  # float seconds, or None if it can never succeed

limiter.peek("api_limit", "user:123")  # / apeek — never mutates
limiter.reset("api_limit", "user:123")  # / areset — clear the counter
```

Signature: `consume(rule: Rule | str, identity: str | Identity, *, cost: int = 1) -> RateLimitResult`.
- A `str` rule is a **name** looked up in `limiter.rules`. An unknown name raises `UnknownRuleError`, which is a `LookupError`.
- A `str` identity is treated as `Identity("custom", value)`.
- Passing `Identity("user", "123")` addresses the same counter that request evaluation uses for `key="user"`, so background jobs and HTTP requests can share quota.

Request-level evaluation is used by the integrations and is available to any other framework. All applicable rules are consumed **atomically, all or nothing** (ARCHITECTURE §6.2):

```python
from unilimit import RequestContext

ctx = RequestContext(
    method="GET",
    path="/api/x",
    peer="10.0.0.5",
    headers={"x-forwarded-for": "203.0.113.9"},
    user_id="42",
)
decision = limiter.evaluate(
    ctx, rules=[Rule("burst", rate="10/s", key="user"), Rule("daily", rate="1000/day", key="user")]
)  # / aevaluate
decision.allowed, decision.reason, decision.binding, decision.results
```

When the backend is unavailable, `consume`/`aconsume` follow `fail_mode`:
- `"open"`: `allowed=True, degraded=True`;
- `"closed"`: `allowed=False, degraded=True`.

`evaluate` reports the same situation as `Reason.BACKEND_UNAVAILABLE`. Unexpected backend errors propagate.

### 2.4 Results

```python
@dataclass(frozen=True, slots=True)
class RateLimitResult:
    rule: Rule
    allowed: bool  # this rule admits the request
    limit: int  # capacity (burst for token bucket)
    remaining: int  # after this request if it was committed, otherwise current
    reset_after: float  # seconds until full capacity is restored
    retry_after: float | None  # 0.0 if allowed; None if the cost can never fit
    enforced: bool = True  # False for shadow rules (action="log")
    degraded: bool = False  # produced by fail-open/closed, not by the backend


class Reason(StrEnum):
    ALLOWED, EXEMPT, RATE_LIMITED, DENIED, MISSING_IDENTITY, BACKEND_UNAVAILABLE


@dataclass(frozen=True, slots=True)
class Decision:
    allowed: bool
    reason: Reason
    results: tuple[RateLimitResult, ...]
    binding: RateLimitResult | None
    degraded: bool = False
    partial_commit: bool = False  # Redis Cluster cross-slot race only (ARCHITECTURE §6.2) [M3]
    rule: Rule | None = None  # the rule behind MISSING_IDENTITY

    @property
    def retry_after(self) -> float | None: ...
    @property
    def violations(self) -> tuple[RateLimitResult, ...]: ...  # shadow rules that would reject
    def headers(self) -> dict[str, str]: ...  # [M6]
    def response(self) -> ResponseSpec: ...  # [M6]
```

Results never contain the raw identity value.

### 2.5 Exceptions

```
UniLimitError
├── ConfigurationError(ValueError)     bad rule/limiter config
│   └── UnknownRuleError(LookupError)  rule name not registered
├── IdentityError                      on_missing="raise"
├── RateLimitExceeded                  carries .decision; raised by integrations [M4/M5]
└── BackendError                       unexpected backend failure (bug, WRONGTYPE, script error)
    └── BackendUnavailable             connection/timeout/cooldown — subject to fail_mode
```

The integrations subclass `RateLimitExceeded` with framework types where that buys working defaults. In FastAPI, `HTTPException` provides a correct 429 without a handler.

---

## 3. Trusted proxies

```python
TrustedProxies(
    networks=["10.0.0.0/8", "172.16.0.0/12"],  # or "cloudflare" preset later — not in v0.1
    header="X-Forwarded-For",  # | "X-Real-IP" | "Forwarded"
)
```

The walk goes from right to left and stops at the first untrusted hop. It runs only if the transport peer is trusted. Full rules are in ARCHITECTURE §11.1. There is **no** "trust all" shortcut, by design. `networks=["0.0.0.0/0", "::/0"]` must be written out explicitly and logs a startup warning.

---

## 4. HTTP response configuration

```python
from unilimit import ResponseConfig

ResponseConfig(
    status_code=429,
    denied_status_code=403,
    missing_identity_status_code=403,
    unavailable_status_code=503,
    body={"detail": "Too Many Requests"},  # dict | str | Callable[[Decision], dict | str]
    legacy_headers=True,  # X-RateLimit-*
    ietf_headers=False,  # RateLimit-Policy / RateLimit (draft-11)
    reset_format="delta",  # "delta" | "epoch"
    extra_headers=None,  # Callable[[Decision], Mapping[str, str]]
)
```

---

## 5. FastAPI / Starlette

### 5.1 Setup

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI
from unilimit import Limiter, Rule
from unilimit.integrations.fastapi import setup

limiter = Limiter.from_url(
    "redis://localhost:6379", global_rules=[Rule("global", rate="1000/minute", key="ip")]
)


@asynccontextmanager
async def lifespan(app):
    yield
    await limiter.aclose()


app = FastAPI(lifespan=lifespan)
setup(app, limiter)  # adds the ASGI middleware, the exception handler and app.state.unilimit
```

`setup(app, limiter, *, middleware=True)` is the only recommended entry point. The pieces stay public for advanced use: `UniLimitMiddleware` (in `unilimit.integrations.starlette`) and `rate_limit_exceeded_handler`.

The middleware is **pure ASGI**, not `BaseHTTPMiddleware`. It has four jobs:
- Apply global rules, matched by path, because routing has not happened yet.
- Store the per-request evaluation state.
- Add rate-limit headers to every response, including responses produced by route dependencies and endpoints that return a `Response` directly.

**Global and route rules in one atomic batch [M4, to be verified].** Global rules are not consumed in the middleware. `setup()` also registers an app-level dependency, which runs after routing. That dependency collects the global rules together with the `RateLimit` rules declared on the matched route and evaluates them as **one** batch. The route's `RateLimit` dependencies then reuse that decision instead of consuming again.

The exception is a `RateLimit` whose `identity=` comes from another dependency. Its value is not known until that dependency runs, so it is evaluated as a **separate** atomic batch after the first. This is documented: if it rejects, the global rules have already been consumed.

### 5.2 Route limits (dependency)

```python
from fastapi import Depends
from unilimit.integrations.fastapi import RateLimit

@app.get("/api", dependencies=[Depends(RateLimit("100/minute", key="user"))])
async def endpoint():
    return {"ok": True}

@app.get("/sync")                                    # sync endpoints work identically
def sync_endpoint(_=Depends(RateLimit("10/second"))):
    return {"ok": True}

# Shared counter across routes: pass a Rule (or the same name via scope=)
search = Rule("search", rate="30/minute", key="ip")
@app.get("/search/a", dependencies=[Depends(RateLimit(search))])
@app.get("/search/b", dependencies=[Depends(RateLimit(search))])

# Identity from your own auth dependency — the FastAPI-native way
@app.get("/me", dependencies=[Depends(RateLimit("60/minute", identity=get_current_user_id))])

# Router-wide
router = APIRouter(dependencies=[Depends(RateLimit("100/minute", scope="per-route"))])
```

```python
class RateLimit:
    def __init__(
        self,
        rule: str | Rule,  # rate string or Rule; may be a list for multiple
        *more: str | Rule,
        key: KeySpec = "ip",  # used only for rate-string rules
        identity: Callable[..., str | None] | None = None,  # a FastAPI dependency
        scope: Literal["per-route"] | str = "per-route",
        algorithm: Algorithm = "sliding_window",
        cost: int | Callable[[RequestContext], int] = 1,
        limiter: Limiter | None = None,  # default: app.state.unilimit
    ): ...
```

- **Naming of rate-string rules.** With `scope="per-route"`, the rule name is derived from `METHOD + route template`, for example `GET /items/{id}`. It is stable across workers and deploys, and every `/items/1`, `/items/2`, … shares one counter. Passing `scope="name"` makes all uses of that name share a counter.
- **Multiple rules in one dependency** (`RateLimit("10/second", "1000/day")`) are consumed atomically, all or nothing. This is the recommended way to stack limits.
- **Rejection** raises `unilimit.integrations.fastapi.RateLimitExceeded`, a subclass of both `HTTPException` and the core `RateLimitExceeded`. It carries the correct status, `Retry-After` and `X-RateLimit-*` headers, so it works even without `setup()`. `setup()` installs a handler that renders the custom body from `ResponseConfig`.
- **Without the middleware**, success headers are set through FastAPI's injected `Response`. This works unless the endpoint returns a `Response` object itself, a limitation verified in M1 and documented.

**Decorator: not provided in v0.1.** slowapi-style decorators either require a `request` parameter in the endpoint signature or rewrite `__signature__`. Both are surprising, and dependencies already cover the use case idiomatically, including router-level application. We will revisit this only if users ask for it.

**WebSockets:** the middleware can limit the `websocket` connect handshake (`websocket=True`). Per-message limits use `limiter.aconsume` directly.

---

## 6. Django

### 6.1 Settings

```python
# settings.py
UNILIMIT = {
    "LIMITER": "myproject.ratelimits.limiter",  # dotted path to a Limiter (recommended)
    # or, for simple setups, a URL plus options passed to Limiter.from_url:
    # "URL": "redis://localhost:6379/0", "OPTIONS": {"namespace": "myproject"},
    "RESPONSE": None,  # dotted path: Callable[[HttpRequest, Decision], HttpResponse]
}

MIDDLEWARE = [
    # after AuthenticationMiddleware so "user" keys work
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "unilimit.integrations.django.UniLimitMiddleware",
    ...,
]
```

Configuration is validated in `AppConfig.ready()` when `"unilimit.integrations.django"` is in `INSTALLED_APPS`. That gives early, startup-time errors and a Django system check (`unilimit.E001`, …). Installing the app is optional; without it, validation happens on first use.

`UniLimitMiddleware` supports both sync and async (`sync_capable = async_capable = True`). Under ASGI with an async chain it uses **only** the async backend. In async mode the user is resolved with `await request.auser()`.

**Global and view rules in one atomic batch [M5].** The middleware consumes in `process_view`, where the resolved view and the rules attached by `@ratelimit` are both known. Global rules and view rules therefore form a single batch. Without the middleware, `@ratelimit` evaluates its own rules as one batch.

### 6.2 View decorator

```python
from django.http import JsonResponse
from unilimit.integrations.django import ratelimit


@ratelimit("100/minute", key="user")
def endpoint(request):
    return JsonResponse({"ok": True})


@ratelimit("10/second", "1000/day", key="user_or_ip")
async def async_endpoint(request):  # async views detected automatically
    return JsonResponse({"ok": True})


# class-based views
from django.utils.decorators import method_decorator


@method_decorator(ratelimit("5/minute", key="ip", methods={"POST"}), name="dispatch")
class LoginView(View): ...
```

`ratelimit(*rules, key="ip", scope=None, methods=None, cost=1, limiter=None)`. The default scope is the view's `module.qualname`, which is stable across workers and deploys. On rejection the decorator **returns** a 429 `HttpResponse` (or the `RESPONSE` override) rather than raising, so it works without the middleware. When allowed, it adds headers to the view's response.

### 6.3 Django REST Framework

```python
from unilimit.integrations.django.drf import UniLimitThrottle


class ItemViewSet(viewsets.ModelViewSet):
    throttle_classes = [UniLimitThrottle]
    unilimit_rules = [Rule("items", rate="100/minute", key="user")]
```

DRF evaluates throttles **after authentication**, so `key="user"` sees token- and session-authenticated users. The `@ratelimit` decorator cannot see them. `UniLimitThrottle` maps rejections to DRF's own `Throttled` (429 with `Retry-After`). Success headers are added by `UniLimitMiddleware` when it is installed. It can also be configured globally through `DEFAULT_THROTTLE_CLASSES` together with `UNILIMIT["DRF_DEFAULT_RULES"]`.

---

## 7. Observability

```python
from unilimit import Hook


class Metrics(Hook):  # all methods optional, sync only, must be fast
    def on_decision(
        self, decision, ctx
    ): ...  # every evaluated request (not exempt ones unless opted in)
    def on_backend_error(self, error, rules): ...


limiter = Limiter.from_url(url, hooks=[Metrics()])
```

- Logging goes to the `unilimit` logger, which has a `NullHandler` attached by default.
- Rejections log at DEBUG.
- Fail-open events log at WARNING, at most one per cooldown period.
- Unexpected errors log at ERROR.
- Log records carry `extra={"unilimit_rule": ..., "unilimit_reason": ...}` and **never** include raw identities.

OpenTelemetry and Prometheus adapters are planned for v0.2 on top of `Hook`.

---

## 8. Migration sketches (fully tested examples land in M7 docs)

| From | To |
|---|---|
| slowapi `@limiter.limit("5/minute")` + `request: Request` param | `dependencies=[Depends(RateLimit("5/minute"))]`; remove the `request` param |
| slowapi `default_limits=["100/minute"]` | `Limiter(..., global_rules=[Rule("default", rate="100/minute")])` + `setup(app, limiter)` |
| fastapi-limiter `RateLimiter(limiter=Limiter(Rate(2, Duration.SECOND*5)))` | `RateLimit("2/5s")` |
| fastapi-limiter custom `identifier` | `RateLimit(..., identity=your_dependency)` or `key=callable` |
| django-ratelimit `@ratelimit(key="user_or_ip", rate="10/m", block=True)` | `@ratelimit("10/minute", key="user_or_ip")` (blocking is the default; use `action="log"` for `block=False`-style observation) |
| django-ratelimit `group="x"` | `scope="x"` |
| DRF `UserRateThrottle` with `"100/day"` | `UniLimitThrottle` + `unilimit_rules=[Rule("x", rate="100/day", key="user")]` |

---

## 9. Decisions (signed off at the M1 review)

| # | Question | Decision |
|---|---|---|
| D1 | Verbs | `consume()` (atomic evaluate and commit) and `peek()` (never mutates), plus `a`-prefixed async twins |
| D2 | Default algorithm | Sliding window counter |
| D3 | Multi-rule semantics | **Atomic evaluate-then-commit, with no compensating decrements.** Fully atomic on memory, Redis standalone and same-identity Cluster batches; two-phase with a documented residual race on cross-slot Cluster batches (ARCHITECTURE §6.2) |
| D4 | Missing identity | `on_missing` = `deny` (default) \| `raise` \| `skip`, plus fallback chains such as `"user_or_ip"` |
| D5 | Default fail mode | `"open"`, with a per-rule `"closed"` option for auth endpoints |
| D6 | IETF headers default | Off until the draft becomes an RFC |
| D7 | IPv6 aggregation default | /64 |
| D8 | FastAPI decorator | Not in v0.1 |
| D9 | Temporary bans | **Removed from v0.1** |
| D10 | Python / Django floor | 3.11 / 5.2 |
| D11 | License | MIT |
