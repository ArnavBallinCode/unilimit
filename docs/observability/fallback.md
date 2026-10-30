# Process-local fallback

When the shared backend is unavailable, `fail_mode="open"` admits everything. If that is
too permissive, you can opt in to a **local fallback**: while Redis is down, each
process enforces the same limits against its own in-memory counters.

```python
from unilimit import Limiter, Rule

limiter = Limiter.from_url(
    "redis://localhost:6379/0",
    rules=[Rule("api", "100/minute", key="user")],
    fallback="memory",  # off by default; also accepts a configured MemoryBackend
)
```

It is never silent: a warning is logged when it starts, `fallback` events are emitted on
start and stop, `Decision.fallback` and `RateLimitResult.fallback` are `True` on every
decision it makes, and `limiter.fallback_active` tells you the current state.

## Semantics

| Question | Answer |
|---|---|
| When is it used? | Only when the backend raises `BackendUnavailable` (including the circuit breaker being open) **and** every enforcing rule's effective `fail_mode` is `open`. |
| Can it make a security policy more permissive? | No. A `fail_mode="closed"` rule (or a closed limiter default) is never given to the fallback: that request is still rejected with 503. One closed rule in a batch keeps the whole batch closed. The fallback only *tightens* fail-open. |
| What counters does it use? | New, empty, per-process counters. They do not know what Redis counted before the outage. |
| Do workers share them? | **No.** With *N* workers the effective limit during an outage is up to *N* times the configured limit. This is the documented, opt-in relaxation of "never over-admit". |
| What happens on recovery? | The first successful Redis call deactivates it. Local counters are **not** merged into Redis and are not reset: they expire by their own TTL. Redis keeps the count it had when it failed, so a client can briefly get its quota from both. |
| Double consumption? | A request the fallback serves is never also counted in Redis, except when a Redis timeout left the original operation in doubt (it may have executed). Then the request can be counted in both: conservative, never more permissive. |
| `peek`? | Never uses the fallback; it is advisory and follows `fail_mode`. |
| Headers | Responses carry the figures of the local counter. |
| Sync and async | Both: the fallback is an in-process `MemoryBackend`, so using it from async code does no I/O. |
| Cluster | Works the same. A `partial_commit` is still reported on the decision. |

## Why not merge back into Redis?

Merging local counts after recovery would need extra Redis writes at the moment Redis is
most fragile, and cannot be made atomic with respect to other workers' merges. The
design chooses a bounded, documented over-admission (N × limit while Redis is down)
over unbounded fail-open and over complexity at recovery.

## Choosing

| You want | Use |
|---|---|
| Never block users because Redis is down | `fail_mode="open"` (default) |
| Never admit anything unverified (login, payment) | `fail_mode="closed"` on those rules |
| Some protection while Redis is down, accepting N × limit | `fallback="memory"` with `fail_mode="open"` |
