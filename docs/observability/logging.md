# Failure logging

An outage affects every request, so logging every affected request would flood the logs
and slow the application. UniLimit logs failures through a **throttled logger**:

- the **first** occurrence of a *category* is logged immediately;
- repeats in the same category are **suppressed** for `interval` seconds (default 30);
- the next occurrence after the interval is logged with a summary:
  `... (4012 similar messages suppressed in the previous 30s)`;
- on **recovery**, on `Limiter.close()` and when the fallback deactivates, pending
  summaries are flushed, so a burst that ends is never left unaccounted for.

```python
import unilimit

unilimit.configure_logging(interval=10)  # seconds; 0 logs every occurrence
```

## Categories

Different failures never suppress each other:

| Category | Logged when |
|---|---|
| `backend_unavailable:open` | A request was admitted because the backend was down. |
| `backend_unavailable:closed` | A request was rejected because the backend was down. |
| `backend_unavailable:fallback` | A request was served by the local fallback. |
| `redis_unavailable` / `redis_recovered` | The Redis circuit breaker tripped / closed. |
| `fallback_activated` / `fallback_deactivated` | The local fallback started / stopped. |
| `missing_identity:<rule>` | A rule could not resolve an identity. |
| `malformed_identity:<rule>` | An identity value was malformed (the value is never logged). |
| `observer_error` | An observer raised. |

Categories are bounded by your configuration (failure kinds, rule names). Beyond 256
categories new ones share `other`.

## Scope and safety

- **Suppression is process-local.** Each worker process keeps its own state, logs its own
  first occurrence and its own summaries. There is deliberately no shared store, so no
  new distributed dependency; with 8 workers expect up to 8 first-occurrence lines per
  interval.
- It is thread-safe, and exact: every suppressed message is counted
  (`test_throttling_is_exact_under_concurrent_failures`).
- **Logging can never affect a decision.** The logger swallows its own errors, including
  a failing handler.
- Use the `unilimit` logger as usual (`logging.getLogger("unilimit")`) to route, filter or
  silence the output.
