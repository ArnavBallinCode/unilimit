# Codebase map

## Request flow

```
framework request
  └─ integration builds RequestContext            integrations/fastapi/_state.py
       └─ Limiter.evaluate / aevaluate            core/limiter.py
            ├─ policy.plan(ctx, rules, config)    core/policy.py      (pure)
            │    exemptions → deny → allow → select rules → resolve identity
            │    (core/identity.py) → storage key (core/keys.py) → ConsumeOp batch
            ├─ backend.consume / aconsume(ops)    backends/memory.py | backends/redis/
            │    = evaluate_batch semantics       backends/base.py    (reference)
            │      per op: algorithms/*.evaluate                     (pure)
            └─ policy.decide(plan, batch)         core/policy.py      → Decision
  └─ integration renders Decision                 http.py → ResponseSpec / headers
```

The only I/O is the backend call. Sync and async drivers in `Limiter` wrap the same
pure `plan` and `decide`.

## Modules

| Path | Responsibility | Notes |
|---|---|---|
| `core/rate.py` | `Rate`, duration and rate-string parsing (`"10/second;100/day"`) | `month` = 30 d and `year` = 360 d, as in `limits` |
| `core/rule.py` | `Rule` (frozen; validated at construction), `Rule.many` | Holds the validated `LimitSpec` as `rule.spec` |
| `core/context.py` | `RequestContext`, header normalisation | `raw` = framework request |
| `core/identity.py` | `Identity`, key sources and fallback chains, `TrustedProxies`, IP normalisation, `IdentityResolver` | Trust-nothing default |
| `core/keys.py` | Key schema `<ns>:v1:rl:<rule>:<algo>:{<digest>}`, BLAKE2b digests, CRC16 hash slots | Braces only around the digest |
| `core/policy.py` | `plan()`, `decide()`, `decide_unavailable()`, `AccessList` | Sans-I/O; `skip_keys` for request dedupe |
| `core/limiter.py` | `Limiter`: `consume`/`peek`/`reset`/`evaluate` and their `a…` twins, `from_url` | Thin drivers |
| `core/result.py` | `RateLimitResult`, `Decision`, `Reason` | Results carry `storage_key` |
| `core/errors.py` | Exception hierarchy | Only `BackendUnavailable` triggers `fail_mode` |
| `algorithms/_base.py` | `Algorithm`, `LimitSpec` (validated invariants), `Evaluation`, `EPS` | Full-refill bound: 10 years |
| `algorithms/{fixed_window,sliding_window,token_bucket}.py` | Pure `evaluate`/`peek` | **Reference model for Lua** |
| `backends/base.py` | `ConsumeOp`, `OpResult`, `BatchResult`, protocols, `evaluate_batch`, `peek_batch` | Defines the atomic contract |
| `backends/memory.py` | In-process backend, one lock, LRU `max_keys` | Per process only |
| `backends/redis/batch.lua` | Consume and peek for every algorithm, one script | Port of the Python reference |
| `backends/redis/_protocol.py` | Script arguments and reply parsing, state codec, slot grouping, commit order | Pure |
| `backends/redis/_errors.py` | redis-py exception → `BackendUnavailable` / `BackendError` | Auth errors never transient |
| `backends/redis/backend.py` | `RedisBackend`: clients (per-loop async), concurrency gate, circuit breaker, cluster two-phase | Most operational logic |
| `http.py` | `ResponseConfig`, `rate_limit_headers`, `rejection_response`, `ResponseSpec` | Framework-free |
| `integrations/fastapi/__init__.py` | `setup()`, exception handler, exports | |
| `integrations/fastapi/middleware.py` | Pure ASGI middleware: global rules, headers at `send`, lifespan close | Not `BaseHTTPMiddleware` |
| `integrations/fastapi/dependency.py` | `Policies` base, `RateLimit`, `RateLimitExceeded` | Injects `identity=` via `__signature__` |
| `integrations/fastapi/websocket.py` | `WebSocketRateLimit` | Handshake close 1008; `consume()` per message |
| `integrations/fastapi/_state.py` | Request-scoped state in `scope["state"]["unilimit"]`, context building | |
| `testing.py` | `ManualClock` | Public test helper |

## Where does my change go?

- **New algorithm:**
  - `algorithms/<name>.py`;
  - register it in `algorithms/__init__.py`;
  - add a code in `Algorithm.code`;
  - port it to `batch.lua`;
  - add property tests and differential tests.
- **New key source** (such as `header:`): `core/identity.py`, `normalise_key_spec` and `IdentityResolver._raw`.
- **New HTTP behaviour:** `http.py`, never the core.
- **New framework:** `integrations/<name>/`. Build a `RequestContext`, call `Limiter`, render with `http.py`.
- **Redis operational behaviour** (timeouts, breaker, pools): `backends/redis/backend.py`.
