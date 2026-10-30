# Glossary

| Term | Meaning |
|---|---|
| **Rule** | A named, immutable policy: rate, algorithm, key spec, options. Its name and algorithm identify its counter. |
| **Rate** | `limit` events per `window` (µs). |
| **Capacity** | The most quota available at once: `limit`, or `burst` for the token bucket. |
| **Key spec / key source** | How a rule finds its identity: `"ip"`, `"user"`, `"api_key"`, `"tenant"`, `"global"`, a callable, or a fallback chain of these. |
| **Identity** | `(kind, value)`, for example `("user", "42")`. Never stored raw. |
| **Digest** | A 22-character keyed BLAKE2b of `kind ‖ 0x00 ‖ value`, used in storage keys. |
| **Storage key** | `<ns>:v1:rl:<rule>:<algo>:{<digest>}`. The braces are the Redis Cluster hash tag, so all of one identity's counters share a slot. |
| **ConsumeOp** | One (storage key, `LimitSpec`, cost, enforce) entry in a backend batch. |
| **Batch** | All ops of one evaluation, committed atomically (all or nothing). |
| **Enforcing / shadow** | `action="reject"` rules can block; `action="log"` (shadow) rules are counted and reported but never block. |
| **Commit** | Writing a batch's new states. It happens iff every enforcing op allows. |
| **Partial commit** | Redis Cluster only: a multi-slot batch where a later slot group rejected after earlier groups committed. The request is rejected; some quota was consumed. |
| **Peek** | Evaluation without mutation. Informational only; never an admission check. |
| **Decision** | A request's outcome: `allowed`, `reason`, per-rule results, binding result, `degraded`, `partial_commit`. |
| **Binding result** | The result that drives headers: on rejection the longest wait, otherwise the least remaining quota. |
| **Degraded** | A result produced by `fail_mode` because the backend was unavailable. It carries no real numbers, so no headers are sent. |
| **fail_mode** | `"open"` (admit) or `"closed"` (reject with 503) when the backend is unavailable. |
| **on_missing** | `"deny"`, `"raise"` or `"skip"` when no identity resolves. |
| **Global rules** | Limiter-level rules applied by the middleware to every HTTP request, before routing. |
| **Route rules** | Rules applied by a `RateLimit` dependency, after routing and authentication. |
| **skip_keys** | Storage keys already consumed earlier in the same request; skipped so a counter is never consumed twice. |
| **Reference model** | The pure Python algorithms and `evaluate_batch`; the Lua script must match them exactly. |
| **EPS** | `1e-9`: the tolerance in quota units for floating-point comparisons. |
| **Breaker / cooldown** | After a Redis outage error, fail fast for `cooldown` seconds, then let one request probe. |
| **Gate** | UniLimit's concurrency limit (`max_connections`) in front of redis-py pools; extra calls wait up to `pool_timeout`. |
| **TrustedProxies** | The configuration that allows reading `X-Forwarded-For` / `X-Real-IP` / `Forwarded`, only from listed networks. |
