# UniLimit — Architecture

Status: **Approved at the Milestone 1 review, with revisions (R1–R5 below).** Milestone 2 implements the core, the algorithms and the memory backend. Redis and the framework integrations are not implemented yet.

Companion document: [API_DESIGN.md](API_DESIGN.md).

### Revision log (M1 review)

| Rev | Change |
|---|---|
| R1 | `hit()` renamed to `consume()`. `consume()` evaluates and commits atomically; `peek()` never mutates state. |
| R2 | Compensating refunds **removed**. Multi-rule evaluation is now atomic evaluate-then-commit: quota is consumed only if every enforcing rule allows the request (§6). The Redis Cluster key schema has been verified against a real server and the notation corrected (§9.2). |
| R3 | Missing identity is configurable as `raise`, `deny` or `skip`. The default is `deny`, so rate limiting is never bypassed silently. Fallback chains are first-class (§11). |
| R4 | Temporary bans removed from v0.1 scope. |
| R5 | Python ≥ 3.11 and Django ≥ 5.2 confirmed. |

---

## 1. Goals and non-goals

**Goals (v0.1.0)**

1. One rate-limiting core, driven identically by Django, FastAPI/Starlette and plain Python.
2. Correct distributed limits on Redis: atomic operations, no over-admission under concurrency.
3. Real sync/async parity: async code never touches a blocking socket; sync code never needs an event loop.
4. Safe defaults:
   - no blind trust of proxy headers;
   - no raw secrets in Redis keys;
   - no internal errors in HTTP bodies;
   - no silent bypass when an identity is missing.
5. Explicit, documented behaviour, especially for multiple rules and backend failure.

**Non-goals (v0.1.0)**

- Hosted service, dashboard, control plane, dynamic rule distribution.
- Backends other than Redis (and Redis-protocol-compatible servers such as Valkey) and in-memory.
- Temporary bans, concurrency (in-flight) limiting, calendar-aligned quotas, auto-ban escalation. The design leaves room for them (§15).
- Flask, Litestar, Sanic. The core is framework-free so they can be added later.

---

## 2. Prior art evaluation

| Library (version checked) | Strengths worth learning from | Limitations UniLimit addresses |
|---|---|---|
| **limits** 5.8.0 | Clean split between *strategy* and *storage*; `hit` vs `test` distinction; rate-string grammar; fixed, moving and sliding-window-counter strategies; many storages. | No request/identity layer and no multi-limit semantics (the caller decides). Async Redis goes through `coredis`/`valkey`, not `redis.asyncio`. No token bucket. Identities appear in plaintext in storage keys. |
| **slowapi** 0.1.10 | Familiar decorator UX for Starlette; header injection; per-route and default limits. | Built on the **sync** `limits` API and calls it from its async wrappers (`extension.py`), so Redis I/O blocks the event loop. The decorator requires a `request: Request` parameter, and header injection requires `response: Response`. `redis` extra pinned `<4.0`. Limits are hit one by one and earlier limits stay consumed when a later one fails. |
| **django-ratelimit** 4.1.0 | Uses Django's cache framework; `user_or_ip` key; hashed cache keys; per-key window jitter (`crc32(value) % period`). | Fixed window only. Sync decorators only. No response headers. The rate is part of the cache key, so changing a limit silently resets every counter. Decorating a DRF view sees `request.user` **before** DRF authentication. |
| **fastapi-limiter** 0.2.0 | Dependency-based API (idiomatic FastAPI); `skip_limiter`; WebSocket support. | 0.2 is a thin layer over `pyrate-limiter`. Its default identifier takes the first `X-Forwarded-For` entry **without any trust check**, so it is spoofable. Multiple limiters are independent dependencies, and the docs ask users to order them by hand. |
| **pyrate-limiter** 4.5.1 / **throttled-py** 3.5.0 | Pluggable algorithms and backends. | General-purpose; no Django/FastAPI identity, proxy or header story. |

**Concepts reused (re-implemented, no code copied):** strategy/storage separation, the `peek`/`consume` distinction, rate-string grammar, `user_or_ip`-style fallback, hashed storage keys, and the dependency-based FastAPI API.

**Deliberate differences:**
- Server-side time (Redis `TIME`) as the single clock.
- Atomic all-or-nothing evaluation across rules.
- One Redis round trip per request on standalone Redis.
- Proxy handling that defaults to "trust nothing".
- One `Limiter` object for both sync and async callers.

---

## 3. Layered architecture

```
 ┌─────────────────────────────────────────────────────────────────────────┐
 │ Integrations (optional extras)                       [M4, M5]           │
 │   integrations/starlette.py  ASGI middleware (FastAPI reuses it)        │
 │   integrations/fastapi.py    RateLimit dependency, setup()              │
 │   integrations/django/       middleware (sync+async), @ratelimit, DRF   │
 │   Job: build RequestContext, call Limiter, render Decision → response   │
 └───────────────────────────────┬─────────────────────────────────────────┘
                                 │ RequestContext / Decision (plain data)
 ┌───────────────────────────────▼─────────────────────────────────────────┐
 │ Core (no framework imports, no I/O of its own)       [M2]               │
 │   Limiter      sync + async drivers (thin)                              │
 │   policy       plan(): exemptions, access lists,  ─┐                    │
 │                selection, identity, dedupe         │ sans-I/O, shared   │
 │                decide(): batch result → Decision   │ by sync & async    │
 │   identity     key sources, trusted proxies, IPs   │                    │
 │   keys         key schema, identity digests       ─┘                    │
 └───────────────────────────────┬─────────────────────────────────────────┘
                                 │ Sequence[ConsumeOp] → BatchResult
 ┌───────────────────────────────▼─────────────────────────────────────────┐
 │ Backends: SyncBackend / AsyncBackend protocols                          │
 │   MemoryBackend   evaluates batches with algorithms/*   [M2]            │
 │   RedisBackend    Lua batch script, redis-py sync+async [M3]            │
 └───────────────────────────────┬─────────────────────────────────────────┘
                                 │
 ┌───────────────────────────────▼─────────────────────────────────────────┐
 │ Algorithms: pure state-transition functions (reference model)  [M2]    │
 │   fixed_window · sliding_window (counter) · token_bucket                │
 │ Batch semantics: backends/base.evaluate_batch (reference model) [M2]    │
 └─────────────────────────────────────────────────────────────────────────┘
```

### Sans-I/O core: the parity mechanism

Everything except the backend call is pure, synchronous computation. `Limiter.evaluate()` and `Limiter.aevaluate()` are thin drivers around the same two functions:

```python
plan = policy.plan(ctx, rules, config)  # pure; may short-circuit to a Decision
batch = backend.consume(plan.ops)  # sync  | await backend.aconsume(plan.ops)
decision = policy.decide(plan, batch)  # pure
```

The only code that exists twice is the I/O layer, and the shared conformance suite (§13) runs against both paths.

---

## 4. Package layout

```
src/unilimit/
  __init__.py            public re-exports (Limiter, Rule, Decision, …)
  py.typed
  _version.py
  _logging.py            the "unilimit" logger
  testing.py             ManualClock (public test helper)
  core/
    clock.py             Clock protocol, MonotonicClock
    rate.py              Rate / duration parsing ("100/minute", "1m", timedelta)
    rule.py              Rule + validation
    context.py           RequestContext
    identity.py          Identity, key sources, TrustedProxies, IP normalisation
    keys.py              key schema, identity digests, hash-slot computation
    result.py            RateLimitResult, Decision, Reason
    policy.py            plan() / decide(), AccessList (sans-I/O)
    limiter.py           Limiter (sync + async drivers)
    errors.py            exception hierarchy
  algorithms/
    _base.py             Algorithm, LimitSpec, Evaluation
    fixed_window.py      pure reference implementations
    sliding_window.py
    token_bucket.py
  backends/
    base.py              ConsumeOp, OpResult, BatchResult, protocols, evaluate_batch()
    memory.py            MemoryBackend (both protocols)
    redis/               [M3]
  http.py, telemetry.py  [M6]
  integrations/          [M4, M5]
tests/  unit/ conformance/ concurrency/  (redis/ fastapi/ django/ integration/ in later milestones)
```

Two refinements of the suggested layout:
- `headers/` and `telemetry/` each become a single module, because they are small.
- `integrations/starlette.py` is kept separate from `fastapi.py`, so plain Starlette users can use the middleware.

---

## 5. Core concepts

| Concept | Responsibility |
|---|---|
| **Rate** | `limit` events per `window`. Parsed from `"100/minute"`, `"10/5s"`, or `limit=` plus `window=` (`"1m"`, `60`, `timedelta`). Stored as integer microseconds. |
| **Rule** | Immutable, named policy: rate, algorithm, key sources, optional `burst`, `cost`, `methods`, `paths`, `when`, `on_missing`, `fail_mode`, `action` (`reject` \| `log`). |
| **RequestContext** | Framework-neutral request view: method, path, route template, transport peer, normalised headers, user id, raw request. |
| **Identity** | `(kind, value)`, for example `("ip", "2001:db8::/64")` or `("user", "42")`. Stored only as a keyed digest (§9.3). |
| **RateLimitResult** | One rule's outcome: `allowed`, `limit`, `remaining`, `reset_after`, `retry_after`, `enforced`, `degraded`. |
| **Decision** | A request's outcome: `allowed`, `reason`, all results, the **binding** result (drives headers), `degraded`. |

The rule name together with the algorithm identifies a counter. **Changing a rule's limit or window keeps its counters.** Changing its algorithm starts fresh state, because the algorithm code is part of the key.

---

## 6. Request evaluation and multi-rule semantics

### 6.1 Deterministic pipeline

1. **Exemptions.** `exempt_paths`, the `exempt(ctx)` predicate and the allowlist. A match returns `EXEMPT`, with no backend I/O.
2. **Denylist.** A match returns `DENIED` (403 by default), with no backend I/O.
3. **Selection.** Global rules first, then route rules, each in declaration order. A rule applies if its `methods`, `paths` and `when(ctx)` all match.
4. **Identity resolution.** The rule's key sources are tried in order (a fallback chain). If none resolves, `on_missing` applies:
   - `deny` (default) → `MISSING_IDENTITY`, nothing consumed;
   - `raise` → `IdentityError`;
   - `skip` → the rule is omitted and a WARNING is logged.

   Shadow rules (`action="log"`) always skip, because they must never block.
5. **Deduplication.** The same storage key appearing twice with an identical spec collapses into one op. The same key with a **different** spec, meaning two rules that share a name and algorithm but have different limits, raises `ConfigurationError`.
6. **Atomic consume.** All ops go to the backend as **one batch** (§6.2).
7. **Decide.**
   - On rejection: the binding result is the rejecting enforced result with the largest `retry_after`, where `None` (never) counts as the largest and ties go by declaration order.
   - On allow: the binding result is the enforced result with the smallest `remaining`, then the smallest `reset_after`, then declaration order.

### 6.2 Atomic evaluate-then-commit

**Contract of `consume(ops)` (every backend):**

1. Each op is evaluated against the current state. Ops are evaluated in batch order, and the batch is applied as one atomic step.
2. If **every enforcing op allows**, the batch commits:
   - every allowed op's new state is written;
   - shadow ops (`enforce=False`) that would reject are not written, because they never exceed capacity.
3. If **any enforcing op rejects, nothing is written.** State is byte-for-byte unchanged.
4. Results for an uncommitted batch use peek semantics: `remaining` reflects the unconsumed state, and `allowed` says whether that rule alone would have admitted the request.
5. Duplicate keys in one batch are a caller error (`ValueError`).

No compensating decrements exist anywhere in the design. Rejected requests never consume quota, from any rule.

**How each deployment achieves the contract:**

| Deployment | Mechanism | Guarantee |
|---|---|---|
| Memory | `evaluate_batch()` under one process-wide lock | **Fully atomic** across all rules and identities |
| Redis standalone / Sentinel | **One Lua script per batch.** A non-cluster Redis lets one script touch any keys, so the script evaluates all ops and then writes all of them. | **Fully atomic** across all rules and identities, in one round trip |
| Redis Cluster, all ops for one identity | Keys share a hash tag, so they are in one slot and one script runs | **Fully atomic** |
| Redis Cluster, ops for several identities (for example a user rule plus an IP rule) | Ops are grouped by slot. **Phase 1:** read-only evaluation of every group in one pipelined round trip; any rejection returns immediately with nothing written. **Phase 2:** each group runs the atomic evaluate-then-commit script, groups in a deterministic order. | **Never over-admits.** Without contention nothing is consumed on rejection. The one residual case: if another request changes a later group's state *between phase 1 and phase 2* and that group now rejects, the earlier groups stay committed. This is over-consumption (conservative), never over-admission, and it is reported as `Decision.partial_commit=True` and logged. |
| Redis Cluster with `cluster_hash_tag="namespace"` (opt-in, M3) | Every key lives in one slot | Fully atomic. Trades away sharding for strictness. |

Why there is no cross-slot transaction: Redis Cluster offers no atomic primitive across slots, and `MULTI`/`WATCH` across nodes is impossible. Faking one with locks would add latency and new failure modes, such as lock expiry during a GC pause. The two-phase approach above is the strongest cross-slot behaviour available without compensation.

### 6.3 Rejected attempts never consume

All algorithms reject **without** charging. Hammering does not extend a lockout. Punitive behaviour belongs to explicit bans (post-v0.1).

### 6.4 `cost` larger than capacity

A request whose `cost` exceeds a rule's capacity can never succeed. It is rejected with `retry_after=None`, and no `Retry-After` header is sent. A static `cost` above capacity is a `ConfigurationError` at rule construction. A cost of 0 or less, or a non-integer cost, raises `ValueError`.

---

## 7. Algorithms

Each algorithm is a pure module exposing:

```python
evaluate(state | None, now_us, spec, cost) -> Evaluation   # would-consume
peek(state | None, now_us, spec, cost)     -> Evaluation   # never produces new state
```

`Evaluation` holds `allowed`, `remaining`, `reset_after_us`, `retry_after_us` (`0` when allowed, `None` when the cost exceeds capacity), `new_state` (present only when `allowed`, for `evaluate`) and `ttl_us`.

**Rejection never yields new state.** That is the structural basis of §6.3.

The Python modules are the **reference model**. `MemoryBackend` executes them directly, and the M3 Lua script must match them exactly; differential property tests enforce this.

**Numerics (parity with Lua):** times are integer µs. Floating-point arithmetic is written with explicit `float` operands and a fixed operation order, so CPython and Lua 5.1 (both IEEE-754 doubles) produce identical bits. Comparisons use `EPS = 1e-9` in quota units, so a value like `1.9999999999999998` is not treated as below `2`.

### 7.1 Fixed window

- **State:** `(start_us, count)`. Windows are **anchored per key**: a window starts at the first request after the previous window expired, which avoids synchronised resets.
- **Rule:** reject if `count + cost > limit`, with `retry_after = start + W − now`.
- **Reported values:** `reset_after = start + W − now`; TTL is the same value.
- **Known property:** up to `2×limit` can pass across a window boundary. This is documented and is why fixed window is not the default.

### 7.2 Sliding window counter (default)

- **State:** `(window_index, current, previous)`, with epoch-aligned windows `i = ⌊now/W⌋`.
- **Rotation:** if `stored_i = i−1`, then `previous ← current` and `current ← 0`; if `stored_i < i−1`, both are cleared.
- **Estimate:** `est = previous·((W−e)/W) + current`, where `e = now − i·W`.
- **Rule:** reject if `est + cost > limit + EPS`.
- **Reported values:**
  - `remaining = ⌊limit − est − cost + EPS⌋` after consuming;
  - `reset_after` is the time until the estimate reaches 0: `2W − e` if `current > 0`, `W − e` if only `previous > 0`, else 0.
  - `retry_after` is solved in closed form:
    - **inside the current window** (`current + cost ≤ limit`): `t = (W−e) − (limit−current−cost)/previous·W`;
    - **otherwise, after rotation:** `t = (W−e) + max(0, W − (limit−cost)/current·W)`;
    - in both cases rounded up to µs.
- **TTL:** `2W − e`.
- O(1) memory. The trade-off: it assumes uniform arrivals in the previous window. An exact sliding log (O(limit) memory) is excluded on purpose.

### 7.3 Token bucket

- **State:** `(tokens, last_us)`. Refill rate `r = limit/W` tokens per µs; capacity `burst` (default `limit`).
- **Refill:** `tokens = min(burst, tokens + (now − last)·r)`.
- **Rule:** admit if `tokens + EPS ≥ cost`.
- **Reported values:**
  - `remaining = ⌊tokens + EPS⌋`;
  - `retry_after = ⌈(cost − tokens)/r⌉`;
  - `reset_after = ⌈(burst − tokens)/r⌉`.

**Why `(tokens, last)` and not GCRA's absolute TAT.** GCRA is the same algorithm, but it stores the theoretical arrival time as an absolute µs timestamp, about 1.8×10¹⁵. In a double, that value has a resolution of 0.25 µs. Adding a fractional emission interval such as `60e6/7 µs` then rounds on every request. At high rates the error accumulates into visible off-by-one `remaining` values. Storing tokens, which are bounded by `burst`, plus an integer `last` keeps full precision. It is equivalent to GCRA with the TAT stored relative to `last`.

---

## 8. Time and clocks

| Backend | Clock | Rationale |
|---|---|---|
| Redis [M3] | Redis `TIME` read inside the script | One clock for all workers and hosts |
| Memory | `time.monotonic_ns() // 1000`, injectable | Single process; immune to wall-clock jumps |

Both backends accept an injected clock for tests. In M2, `unilimit.testing.ManualClock` provides one. The M3 script will take an explicit `now_us` argument (`0` means use `TIME`). If a clock goes backwards, as after a Redis failover to a replica with a skewed clock, elapsed time is clamped at 0. Limits stay safe; they are briefly stricter.

---

## 9. Backends

### 9.1 Protocols

```python
class SyncBackend(Protocol):
    def consume(self, ops: Sequence[ConsumeOp]) -> BatchResult: ...
    def peek(self, ops: Sequence[ConsumeOp]) -> tuple[OpResult, ...]: ...
    def reset(self, keys: Sequence[str]) -> None: ...
    def close(self) -> None: ...


class AsyncBackend(Protocol):
    async def aconsume(self, ops: Sequence[ConsumeOp]) -> BatchResult: ...
    async def apeek(self, ops: Sequence[ConsumeOp]) -> tuple[OpResult, ...]: ...
    async def areset(self, keys: Sequence[str]) -> None: ...
    async def aclose(self) -> None: ...
```

The async methods carry an `a` prefix so that one object implements both protocols. A backend signals transient unavailability by raising `BackendUnavailable`. Any other exception propagates as a bug.

`backends/base.evaluate_batch()` is the reference implementation of the §6.2 contract. It is shared by the memory backend and used as the oracle in the M3 Lua differential tests.

### 9.2 Redis key schema (v1), verified

```
<ns>:v1:rl:<rule>:<algo>:{<digest>}
```

Angle brackets are placeholders. **The only literal braces are the ones around the digest.** Redis hashes the substring between the first `{` and the next `}` when that substring is non-empty. These were checked with `CLUSTER KEYSLOT` on Redis 8.8 during M1 review:

| Key | Slot |
|---|---|
| `unilimit:v1:rl:api:sw:{Zm9vYmFyYmF6cXV4MTIzNA}` | 15182 |
| `unilimit:v1:rl:login:tb:{Zm9vYmFyYmF6cXV4MTIzNA}` | 15182 (same identity, different rule and algorithm, **colocated**) |
| `Zm9vYmFyYmF6cXV4MTIzNA` | 15182 (the tag alone) |
| `{ns}:v1:rl:{rule}:{algo}:{Zm9v…}` (the M1 notation taken literally) | 9937 (**wrong**: hashes on `ns`, so every key lands in one slot) |

The M1 document wrote `{ns}:…:{rule}`, which looks like literal braces and would have put every key into slot 9937. The notation is corrected above, and the following invariants are now enforced in code and tests:

- `ns` must match `[A-Za-z0-9_.-]{1,32}`: no braces, no colons.
- `rule` is the rule name if it matches `[A-Za-z0-9_.:-]{1,64}`; otherwise it is `~` plus 16 hex characters of SHA-256. Names containing braces are always hashed, and `~` is outside the safe charset, so a hashed segment cannot collide with a literal name.
- `algo` is one of `fw`, `sw`, `tb`.
- `digest` is unpadded base64url (`[A-Za-z0-9_-]`, 22 characters), so it contains no braces.
- `unilimit.core.keys.key_hash_slot()` implements Redis's CRC16/XMODEM and hash-tag rules. Unit tests check it against the server-verified slots above and the edge cases in the Redis spec (`foo{}{bar}` = 8363, `foo{{bar}}zap` = 4015, `foo{bar}{zap}` = 5061, `123456789` = 12739). They also check that every key built for one identity maps to one slot. In M3 the same property is checked against a live cluster.

### 9.3 Identity digests

`digest = base64url(BLAKE2b(kind ‖ 0x00 ‖ value, digest_size=16, key=identity_secret))`.

- The `kind` prefix keeps `user:"1"` and `ip:"1"` apart.
- Without `identity_secret`, low-entropy identities such as IPv4 addresses can be brute-forced from a Redis dump, so the production docs recommend setting it.
- Rotating the secret resets all counters.
- Identity values must be `str` or `int`, non-blank, and at most 1024 characters. Malformed values are treated as unresolved: the next source in the fallback chain is tried, then `on_missing` applies. A warning is logged that never includes the value.

### 9.4 Memory backend

- An `OrderedDict` of `key → (state, expires_at_us)` behind a `threading.Lock`.
- The async methods run the same code. The critical section is CPU-only and never contains an `await`, so it is safe for the event loop.
- Expiry is lazy, plus an amortised sweep. A `max_keys` cap (default 100 000) evicts in least-recently-used order to bound memory. Evicting a live key resets that identity's counter, which is documented.
- **Per-process only.** N workers mean N independent limits.

### 9.5 Redis backend [M3]

- **Script:** one Lua script, `consume_batch`, takes `KEYS` and per-op `ARGV` (algorithm, limit, window, capacity, cost, enforce, now). It runs phase 1 (evaluate everything) and then phase 2 (write only if all enforcing ops allow). A `peek_batch` script is the read-only variant used for `peek` and for cluster phase 1. Both are loaded with `register_script`, which uses `EVALSHA` and falls back on `NOSCRIPT`.
- **Clients:** a sync `redis.Redis`/`RedisCluster`, and async `redis.asyncio` clients created **per event loop** (`WeakKeyDictionary`), because async pools are bound to a loop. User-supplied clients are used as-is.
- **Timeouts and cooldown:** 0.25 s connect and socket timeouts by default. After a connection failure the backend fails fast for `cooldown` seconds (default 1.0).
- **Supported servers:** Redis ≥ 6.2 and Valkey ≥ 7.2: standalone, Sentinel (user-supplied client), Cluster, TLS.
- **Cancellation:** if a task is cancelled mid-script, the script either ran completely or not at all, because Lua scripts are atomic. redis-py ≥ 5 discards the connection. UniLimit never shields backend calls from cancellation.

---

## 10. Failure semantics

| Error | Behaviour |
|---|---|
| `BackendUnavailable` (connection, timeout, cooldown) | The `fail_mode` of the rules in the batch decides:<br>• If any **enforcing** rule is `closed`, the request is rejected with reason `BACKEND_UNAVAILABLE` (HTTP 503 by default in M6).<br>• Otherwise the request is allowed with `degraded=True`.<br>Logged at WARNING. |
| Any other backend exception | Propagates (wrapped as `BackendError` by the Redis backend). Never swallowed, never subject to fail-open. |
| Bad configuration | `ConfigurationError` at construction time where possible; conflicting rules are detected at evaluation time. |
| `on_missing="raise"` | `IdentityError` |

HTTP bodies never include exception text (M6).

---

## 11. Identity and trusted proxies

**Key sources:**
- built-ins: `"ip"`, `"user"`, `"user_or_ip"` (shorthand for `("user", "ip")`), `"api_key"` (header configured on the limiter, default `X-API-Key`), `"tenant"` (configured header), `"global"`;
- any callable `(RequestContext) -> str | int | None` (identity kind `custom`);
- a sequence of the above, which forms a **fallback chain**. For example, `("user", "ip")` means "authenticated user id, else the trusted client IP". Each link produces its own identity kind, so anonymous IP-based counters never mix with user counters.

**Missing identity.** `on_missing` is set on the limiter (default `"deny"`) and can be overridden per rule:

| Value | Effect |
|---|---|
| `deny` | The request is rejected with `MISSING_IDENTITY`. Nothing is consumed. Logged at WARNING. |
| `raise` | `IdentityError`. Use this when a missing identity can only mean a programming error, such as a user-keyed rule on an endpoint that requires authentication. |
| `skip` | The rule is omitted for this request. Logged at WARNING. This is opt-in only. |

### 11.1 Client IP resolution

- **The default trusts nothing.** The client IP is the transport peer.
- `TrustedProxies(networks, header=…)` reads `X-Forwarded-For`, `X-Real-IP` or RFC 7239 `Forwarded`, and **only when the transport peer is trusted**.
  - It walks the header from right to left, skipping trusted addresses. The first untrusted address is the client.
  - A malformed or obfuscated entry stops the walk, and the last trusted hop is used.
  - Ports and brackets are stripped. Multiple header lines are joined in order.
- A peer that is not an IP address, such as a Unix socket or a test client, is used as an opaque identity. It comes from the transport, not from the client.
- IPv4-mapped IPv6 addresses are normalised to IPv4. IPv6 addresses are aggregated to `/64` by default (`ipv6_prefix`, 1–128).

### 11.2 User identity [M4/M5]

- **Django:** `request.user.pk` in sync code, `await request.auser()` in async code.
- **Starlette:** `scope["user"]`.
- **FastAPI:** `RateLimit(identity=Depends(...))`.
- **DRF:** a dedicated throttle class, because DRF authenticates after middleware and decorators run.

Custom key callables are synchronous in v0.1, for parity.

---

## 12. HTTP layer [M6]

- Default headers: `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset` (delta seconds), and `Retry-After` on rejection.
- The IETF draft-11 `RateLimit-Policy`/`RateLimit` fields are opt-in, and the `pk` parameter is never sent.
- Configurable status codes (429 / 403 for `DENIED` / 403 for `MISSING_IDENTITY` / 503 for unavailable), body, headers and hooks.

---

## 13. Testing strategy

| Suite | Milestone | Proves |
|---|---|---|
| `unit/` | M2 | Rate parsing, rule validation, identity resolution, adversarial proxy headers, key schema and hash slots, policy planning and decisions, limiter pipeline, fail modes. |
| `unit/algorithms` | M2 | Hypothesis invariants on random schedules with a manual clock:<br>• **safety:** fixed window ≤ limit per window; sliding window has no admission once the estimate would exceed the limit; token bucket admits at most `burst + r·Δt` in any interval;<br>• rejection yields no state;<br>• `peek` agrees with `evaluate`;<br>• `retry_after` is sufficient. |
| `conformance/` | M2 (memory), M3 (+Redis, +Cluster) | Every backend × {sync, async}: the atomic batch contract, peek purity, reset, expiry. |
| `concurrency/` | M2 (threads/asyncio, memory), M3 (multi-process, Redis) | Exact admission count under contention. |
| `redis/differential` | M3 | Lua equals `evaluate_batch` on random batches. |
| `redis/failure` | M3/M6 | Real Redis behind a fault-injecting proxy. |
| `fastapi/`, `django/` | M4/M5 | End to end. |
| wheel install | M7 | The built wheel works with each extra. |

**Determinism:**
- All time-dependent tests use `ManualClock`.
- Hypothesis runs with `derandomize=True` by default (fixed seed). A separate `explore` profile allows deeper randomised fuzzing.
- Real-thread concurrency tests assert exact counts, not timings.

---

## 14. Benchmark methodology [M7]

- **Micro:** `consume`/`aconsume` against local Redis, compared with `limits` on the same algorithm.
- **HTTP:** identical FastAPI apps (UniLimit, slowapi, fastapi-limiter) and Django apps (UniLimit, django-ratelimit) against a no-limiter baseline, with a fixed worker count and an out-of-process load generator. Reported as RPS, added latency, p50/p95/p99 and RSS.
- Every result file records hardware, OS, Python, Redis and library versions, and the exact command used.

---

## 15. Roadmap

| Feature | Status |
|---|---|
| Static allow/deny lists, shadow mode, weighted cost | v0.1 (M2 core) |
| Temporary bans (`ban`/`unban`) | **Post-v0.1.** A `<ns>:v1:ban:{<digest>}` key in the identity's slot can be read inside `consume_batch` with no extra round trip. |
| Concurrency (in-flight) limits | Post-v0.1 (lease keys + release hook) |
| Calendar-aligned daily/monthly quotas | Post-v0.1 |
| OpenTelemetry/Prometheus adapters | Post-v0.1, on the M6 `Hook` protocol |

---

## 16. Risk register

| # | Risk | Mitigation |
|---|---|---|
| R1 | Lua and Python algorithm implementations diverge | `evaluate_batch` is the oracle; differential Hypothesis tests (M3); floats written in a fixed operation order |
| R2 | Cross-slot batches in Redis Cluster are not fully atomic | Two-phase evaluate then commit; `partial_commit` reported; opt-in single-slot mode; documented |
| R3 | Async Redis client used across event loops | Per-loop clients (M3) |
| R4 | Sync Redis call in an async path | Separate code paths; guard fixture (M3) |
| R5 | Django async `request.user` access | `auser()` (M5) |
| R6 | DRF authenticates after middleware and decorators | `UniLimitThrottle` (M5) |
| R7 | Starlette middleware runs before routing | Global rules match on paths (M4) |
| R8 | FastAPI endpoints returning a `Response` directly lose dependency-set headers (confirmed in an M1 prototype) | Middleware adds the headers (M4) |
| R9 | Proxy misconfiguration | Trust-nothing default; explicit networks only; a warning for `0.0.0.0/0` |
| R10 | A Redis outage adds a timeout to every request | Short timeouts plus cooldown (M3) |
| R11 | Key cardinality explosion | TTL ≤ 2W on every key; memory `max_keys`; IPv6 /64 aggregation |
| R12 | Hot key for a `global` rule | Documented |
| R13 | Floating-point precision | Token-bucket representation (§7.3); EPS comparisons; validation that `W/limit ≥ 1 µs` |
| R14 | Clock skew on failover | Elapsed time clamped at 0 (stricter, never looser) |
| R15 | `deny` default rejects anonymous traffic on user-keyed rules | Intended (no silent bypass). Fallback chains (`"user_or_ip"`) are the documented fix, and every denial logs a WARNING naming the rule. |
| R16 | Live-key eviction under `max_keys` resets counters in the memory backend | The memory backend is for development and single-process use only; documented |

---

## 17. Compatibility and versioning

- **Python ≥ 3.11.** The core has **no runtime dependencies.** The extras `redis`, `django`, `fastapi` and `all` are added in the milestones that implement them.
- **Django ≥ 5.2**, FastAPI ≥ 0.115, Starlette ≥ 0.40, redis-py ≥ 5.0, DRF ≥ 3.15 (tested).
- **SemVer.** The public API is `unilimit.__all__` plus the documented modules. Deprecations warn one minor release ahead. The Redis key schema is versioned separately (`v1`).
- Hatchling build, src layout, `py.typed`, Ruff, mypy `--strict`, MIT licence.
