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
        cluster=False,  # True for Redis Cluster (auto-detected for supplied clients)
        socket_timeout=0.5,
        socket_connect_timeout=1.0,
        max_connections=32,  # in-flight script calls per process (per event loop for async)
        pool_timeout=1.0,  # wait this long for a free slot before BackendUnavailable
        cooldown=0.5,  # breaker: fail fast, then let one request probe
        clock=None,  # tests only: overrides Redis server time
        client_options=None,  # extra keyword arguments for the redis-py clients
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
)
```

`Limiter.from_url` supports `memory://`, `redis://`, `rediss://` and `unix://`. Backend options are separated from limiter options automatically:

```python
Limiter.from_url("redis://cache:6379/0", cluster=True, socket_timeout=0.5, namespace="api")
Limiter.from_url("memory://", clock=ManualClock(), max_keys=10_000)
```

Redis-only options passed with `memory://`, and unsupported schemes, raise `ConfigurationError`. Redis requires the extra: `pip install "unilimit[redis]"`.

Users who already manage redis-py clients (Sentinel, custom TLS, shared pools) can pass them in:

```python
RedisBackend(client=redis.Redis(...), async_client=redis.asyncio.Redis(...))
```

Clients built from a URL are created **lazily**. The async client is created **once per event loop** (see ARCHITECTURE §9.5). Supplied clients are never closed by UniLimit and keep their own retry and timeout settings, so configure them for a rate limiter: short timeouts and few retries.

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
- **Several rates** (the `limits` / slowapi syntax): `Rule.many("api", "10/second;1000/day", key="user")` returns one rule per rate, named `"api:10/1s"` and `"api:1000/1d"`. Separators are `;`, `,` and `|`; `month` = 30 days and `year` = 360 days, as in `limits`. The FastAPI `RateLimit("api", rate="10/second;1000/day")` accepts the same strings and consumes the rules atomically.
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
    storage_key: str | None = None  # backend key (contains only an identity digest)


class Reason(StrEnum):
    ALLOWED, EXEMPT, RATE_LIMITED, DENIED, MISSING_IDENTITY, BACKEND_UNAVAILABLE


@dataclass(frozen=True, slots=True)
class Decision:
    allowed: bool
    reason: Reason
    results: tuple[RateLimitResult, ...]
    binding: RateLimitResult | None
    degraded: bool = False
    partial_commit: bool = False  # not admitted, yet some cluster slot groups were consumed (§6.2)
    rule: Rule | None = None  # the rule behind MISSING_IDENTITY

    @property
    def retry_after(self) -> float | None: ...
    @property
    def violations(self) -> tuple[RateLimitResult, ...]: ...  # shadow rules that would reject
```

Results never contain the raw identity value.

### 2.5 Exceptions

```
UniLimitError
├── ConfigurationError(ValueError)     bad rule/limiter config
│   └── UnknownRuleError(LookupError)  rule name not registered
├── IdentityError                      on_missing="raise"
├── RateLimitExceeded                  carries .decision; raised by integrations
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

## 4. HTTP responses (`unilimit.http`) — implemented in M4

Framework-independent; the core limiter does not depend on it.

```python
from unilimit.http import ResponseConfig, rate_limit_headers, rejection_response

ResponseConfig(
    rate_limited_status=429,
    denied_status=403,
    missing_identity_status=403,
    unavailable_status=503,
    body=None,  # dict | str | Callable[[Decision], dict | str]; None = generic per reason
    legacy_headers=True,  # X-RateLimit-Limit / -Remaining / -Reset, Retry-After on rejection
    ietf_headers=False,  # RateLimit-Policy / RateLimit (draft-11), opt-in
    reset_format="delta",  # "delta" (seconds) | "epoch" (Unix time)
    retry_after_format="seconds",  # "seconds" | "http-date"
)

rate_limit_headers(results, config, rejected=False)  # -> list[(name, value)]
rejection_response(decision, config)  # -> ResponseSpec(status_code, headers, body, media_type)
```

Changed from M1: response configuration is passed to the integration (`setup(app, limiter, response=...)`), not to `Limiter`, and header generation lives in this module rather than in `Decision` methods. This keeps the core free of HTTP concerns.

---

## 5. FastAPI — implemented in M4

User guides with executable examples: [`docs/fastapi/`](docs/fastapi/quickstart.md).

```python
from fastapi import Depends, FastAPI

from unilimit import Limiter, Rule
from unilimit.integrations.fastapi import RateLimit, setup

limiter = Limiter.from_url("redis://localhost:6379/0", global_rules=[Rule("site", "300/minute")])
app = FastAPI()
setup(app, limiter)  # middleware + 429 handler + client shutdown


async def current_user_id() -> int | None: ...


@app.get("/profile")
async def profile(
    decision=Depends(RateLimit("profile", rate="30/minute", identity=current_user_id)),
):
    return {"remaining": decision.results[0].remaining}
```

`RateLimit` is imported from `unilimit.integrations.fastapi`, not from `unilimit`, so that importing the core never imports FastAPI.

### 5.1 `setup(app, limiter, *, response=None, close_on_shutdown=True, user=None)`

Installs the pure-ASGI `UniLimitMiddleware` and the `RateLimitExceeded` handler, and stores the limiter on `app.state`. The middleware:
- applies exemptions, access lists and global rules **before routing**, so 404s count too;
- adds rate-limit headers at the ASGI `send` level, so they reach responses that endpoints construct themselves, as well as streaming responses;
- closes the limiter's clients on lifespan shutdown.

`user` resolves the user for global rules; the default is Starlette's `scope["user"]`.

### 5.2 `RateLimit` dependency

```python
RateLimit(
    *rules,  # "name" (inline with rate=, or registered on the Limiter) | Rule objects
    rate=None,
    limit=None,
    window=None,  # inline rule definition
    identity=None,  # a FastAPI dependency returning a user id (str | int) or None
    key=None,  # default "user" with identity=, else "ip"
    algorithm="sliding_window",
    burst=None,
    cost=1,
    methods=None,
    when=None,
    on_missing=None,
    fail_mode=None,
    action="reject",
    per_route=False,  # a counter per route template instead of one shared counter
    limiter=None,  # default: the limiter installed by setup()
)  # returns the Decision
```

Changed from M1: a string is a rule **name**, which mirrors `Rule(name, rate)`, rather than a rate string. Counters are shared by name; `per_route=True` replaces the M1 `scope=` option.

`identity` is resolved by FastAPI before the limit and cached for the request. Rejections raise `RateLimitExceeded`, a subclass of both `HTTPException` and the core `RateLimitExceeded`, so they work even without `setup()`.

### 5.3 Order of enforcement and duplicates

1. Middleware: exemptions, deny/allow lists, global rules.
2. `RateLimit` dependencies in FastAPI order: app-level, router-level (outer to inner), route `dependencies=[...]`, endpoint parameters.

Each step is one atomic batch. A counter (storage key) already consumed earlier in the request is skipped (`Limiter.evaluate(..., skip_keys=...)`), so overlapping policies never consume the same counter twice. A rejection ends the request; earlier steps stay consumed.

Changed from M1: the M1 plan merged global and route rules into a single batch through an app-level dependency. That is impossible in general. Route rules can depend on identities produced by dependencies that run later, and a request may match no `RateLimit` at all, in which case the global rules must still be enforced before the endpoint runs. Deduplication by storage key gives the "never twice" guarantee without that coupling.

**Decorator:** not provided (decision D8).

### 5.4 WebSockets

```python
connect = WebSocketRateLimit("chat-connect", rate="10/minute")  # same options as RateLimit
messages = WebSocketRateLimit("chat-message", rate="5/second;100/minute")


@app.websocket("/chat", dependencies=[Depends(connect)])  # rejected handshake: close 1008
async def chat(websocket: WebSocket):
    await websocket.accept()
    ...
    decision = await messages.consume(websocket, user_id=None)  # per message; never raises
```

The HTTP middleware passes WebSockets through, so global rules do not apply to them.

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
