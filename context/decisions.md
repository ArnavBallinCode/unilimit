# Decision log

Settled decisions, newest last. Before changing one, read its rationale; reversing a
decision needs the maintainer's agreement and a new entry here.

## M1 review (signed off by the maintainer)

| # | Decision | Rationale |
|---|---|---|
| D1 | Verbs are `consume()` (atomic evaluate and commit) and `peek()` (never mutates), with `a`-prefixed async twins on the same `Limiter` | `check` reads as side-effect free and invites check-then-act races; one object serves sync and async |
| D2 | Default algorithm: sliding window counter | O(1) memory, no 2× burst at window edges |
| D3 | Multi-rule evaluation is atomic evaluate-then-commit; **no compensating decrements** | Refunds race and fail silently. Cluster cross-slot uses two-phase with `partial_commit` |
| D4 | `on_missing` = `deny` (default) / `raise` / `skip`; fallback chains such as `"user_or_ip"` | Rate limiting is never bypassed silently |
| D5 | Default `fail_mode="open"`, with per-rule `"closed"` | Availability by default; strictness where it matters (login) |
| D6 | IETF RateLimit headers off by default | Still a draft (rev 11) |
| D7 | IPv6 clients aggregated to /64 | One subscriber usually controls a /64 |
| D8 | No FastAPI decorator | Decorators need a `request` parameter or signature rewriting; dependencies are idiomatic |
| D9 | Temporary bans out of v0.1 | Scope |
| D10 | Python ≥ 3.11, Django ≥ 5.2 | 3.10 and Django 4.2 are past end of life |
| D11 | MIT licence | Ecosystem convention |

## M2

| Decision | Rationale |
|---|---|
| Token bucket stores `(tokens, last)`, not GCRA's absolute TAT | An absolute µs TAT in a double has 0.25 µs resolution, and the rounding accumulates; tokens are bounded and precise |
| Token bucket snaps to full capacity within EPS | Otherwise expiring a key at its TTL changed later decisions; found by deep fuzzing |
| Fixed windows are anchored per key; sliding windows are epoch-aligned | Avoids synchronised resets; the sliding estimate smooths edges |
| Deny list checked before allow list | Deny wins |
| Python algorithms are the reference model | One spec; Lua is tested against it |

## M3

| Decision | Rationale |
|---|---|
| One Lua script (consume and peek) for all algorithms; state stored as strings | Single `EVALSHA`; easy corruption detection |
| Script returns the written state and TTL | Lets differential tests compare exactly, without racing real-time expiry |
| Server time is used when the `now` argument is `''`; `0` is a valid clock value | A `0` sentinel was ambiguous (bug found by the tests) |
| `PX = ceil(ttl_ms) + 1` | Redis counts PX from script start at ms resolution; keys must never expire early |
| Full token-bucket refill ≤ 10 years (validated in `LimitSpec`) | Keeps all values < 2^53 and TTLs valid |
| One immediate retry, on connection errors only | Recovers stale pooled connections; timeouts are never retried; a double consume is conservative |
| Concurrency gate (`max_connections`, `pool_timeout`) in front of redis-py | redis-py pools raise rather than wait; bursts would fail open and trip the breaker |
| Half-open circuit breaker with a single probe; pool exhaustion never trips it | Avoids herds of timeouts and amplifying brief stalls |
| Defaults: `socket_timeout` 0.5, connect 1.0, `max_connections` 32, cooldown 0.5 | Measured under connection storms |
| Async cluster clients initialise the topology under a lock before first use | redis-py's async cluster races its own cold-start initialisation |
| Strict single-slot cluster mode not implemented | Kills sharding; revisit only on demand |

## M4

| Decision | Rationale |
|---|---|
| `RateLimit` is imported from `unilimit.integrations.fastapi`, not `unilimit` | Importing the core must never import FastAPI |
| A string in `RateLimit(...)` is a rule *name* (as in `Rule(name, rate)`); inline rules use `rate=` | Consistency with `Rule`; replaces the M1 "string = rate" design |
| Counters are shared by name; `per_route=True` gives per-route counters | Explicit sharing; replaces the M1 `scope=` option |
| Global (middleware) and route (dependency) rules are separate atomic steps, deduplicated by storage key | A single combined batch is impossible when route identities come from later dependencies and some routes have no `RateLimit` |
| Pure ASGI middleware adds headers at `http.response.start` | Headers reach directly returned, streaming and 204 responses |
| `ResponseConfig` lives in `unilimit.http` and is passed to `setup()`, not `Limiter` | Keeps HTTP concerns out of the core |
| The middleware closes the limiter's clients on lifespan shutdown (`close_on_shutdown`) | No lifespan code needed; shared limiters opt out |
| `RateLimitExceeded` subclasses `HTTPException` | Correct 429 even without `setup()` |

## Prior-art adoptions (after M4)

| Decision | Rationale |
|---|---|
| Rate grammar is a superset of `limits` (multi-rate `;`/`,`/`|`, `month` = 30 d, `year` = 360 d); `Rule.many` | slowapi / Flask-Limiter configurations migrate verbatim; checked against the real `limits` parser |
| Multi-rate inline `RateLimit` creates rules named `"{name}:{rate}"`, consumed atomically | Each rate keeps its own counter |
| `WebSocketRateLimit`: handshake closes with 1008; `consume()` never raises | The application decides how to treat a rejected message |
| `ResponseConfig.retry_after_format="http-date"` | slowapi parity |
| `in_memory_fallback`-style degraded mode → M6; `header:`/`query:` keys and IPv4 masks → M5 | Larger semantic changes, belonging with those milestones |
