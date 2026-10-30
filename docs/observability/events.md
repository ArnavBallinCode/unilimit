# Events and observers

UniLimit reports what it does through **events**. An *observer* is a synchronous
callable that receives one event per notable occurrence. Nothing else is needed to
build logging, metrics or alerting on top.

```python
from unilimit import DecisionEvent, Event, Limiter, Rule

seen: list[dict] = []


def observe(event: Event) -> None:
    seen.append(event.to_dict())  # JSON-serialisable


limiter = Limiter.from_url(
    "redis://localhost:6379/0",
    rules=[Rule("api", "2/minute", key="user")],
    observer=observe,  # or a list of observers
)

limiter.consume("api", "alice")
limiter.consume("api", "alice")
rejected = limiter.consume("api", "alice")
assert not rejected.allowed

decisions = [e for e in seen if e["event"] == "decision"]
assert [d["allowed"] for d in decisions] == [True, True, False]
assert decisions[-1]["reason"] == "rate_limited"
assert decisions[-1]["binding_rule"] == "api"
limiter.close()
```

Observers can also be added later with `limiter.add_observer(...)` and removed with
`limiter.remove_observer(...)`.

## Guarantees

- **An observer never changes a decision.** Events are emitted after the decision is
  made. An `Exception` raised by an observer (or while building an event) is caught and
  logged (throttled, category `observer_error`, exception *class name* only); the
  request continues as if the observer had not been called.
- **Observers run inline**, in the calling thread or on the event loop. They must be
  fast and must not block. Hand slow work (network, disk) to a queue.
- **Nothing is built without an observer.** With none configured, the cost per decision
  is one truthiness check.
- **No sensitive data.** Events contain rule names and algorithms (your configuration),
  backend kind, outcomes and timings. They never contain identities, IP addresses,
  storage keys, paths, headers, credentials or error messages.

## Event schema

`to_dict()` returns a flat mapping with `event` (the name), `schema` (currently `1`),
`timestamp` (Unix seconds) and the fields below. Adding a field does not change
`schema`; renaming or removing one does. The schema is pinned by
`test_event_schema_is_stable`.

### `decision`: `DecisionEvent`

A request, or a single-rule `consume`, was decided. `peek` is not a decision.

| Field | Meaning |
|---|---|
| `allowed` | The outcome. |
| `reason` | A `unilimit.Reason` value: `allowed`, `exempt`, `rate_limited`, `denied`, `missing_identity`, `backend_unavailable`. |
| `source` | `backend` (normal), `policy` (decided before any backend call: exempt, denied, no identity, no rules), `degraded` (backend unavailable, `fail_mode` decided) or `fallback` ([process-local fallback](fallback.md)). |
| `backend` | `memory`, `redis` or `redis-cluster`. |
| `rules` | One `{name, algorithm, allowed, enforced}` per rule evaluated. |
| `binding_rule` | The rule that determines the response headers, or the rule whose identity was missing. |
| `fail_mode` | `open` or `closed` when `source == "degraded"`, otherwise `null`. |
| `partial_commit` | A Redis Cluster batch was rejected after part of it was consumed. |
| `duration` | Backend call time in seconds, `null` when there was no backend call. |

A fail-open decision is `allowed` and a fail-closed one is not; both have
`source == "degraded"`.

### `backend_unavailable`: `BackendUnavailableEvent`

A backend call raised `BackendUnavailable` (the only error `fail_mode` applies to).

| Field | Meaning |
|---|---|
| `backend` | As above. |
| `operation` | `consume` or `peek`. |
| `error` | Exception class name of the root cause, such as `ConnectionError`. |
| `partial_commit` | As above. |
| `duration` | Seconds the failed call took (near zero while the breaker is open). |

Auth errors, corrupt state and bugs are *not* events: they raise, as before.

### `breaker`: `BreakerEvent`

The Redis circuit breaker changed state.

| Field | Meaning |
|---|---|
| `state` | `open` (Redis failed; calls fail fast for `cooldown` seconds), `half_open` (one probe call goes to Redis) or `closed` (the probe succeeded: **Redis recovered**). |
| `error` | Failure class name for `open`, otherwise `null`. |
| `cooldown` | The configured cooldown in seconds. |

A failed probe produces `open` again. A healthy run never produces breaker events.

### `fallback`: `FallbackEvent`

The [process-local fallback](fallback.md) started (`activated`) or stopped
(`deactivated`) serving. `decisions` is the number it served in the period that ended.

## Ordering

For one failed call you receive `backend_unavailable`, then the `decision` it caused.
Breaker events are emitted as the transition happens, before the decision of the call
that triggered it.
