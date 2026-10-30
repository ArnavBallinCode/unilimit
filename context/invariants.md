# Invariants

Each invariant states what must hold, why, and which tests enforce it. If a change
needs to break one, stop and ask the maintainer.

## 1. Atomic batches; rejected requests consume nothing

`consume(ops)` commits every op or none. Commit happens iff every *enforcing* op allows;
shadow ops (`action="log"`) never block. A rejected batch leaves state byte-for-byte
unchanged.

**Why:** the maintainer rejected compensating decrements (refunds) in the M1 review.
Refunds race, fail silently, and are not exact for GCRA-style state.

**Enforced by:**
- `test_batch_is_all_or_nothing`, `test_batches_are_atomic` (conformance, every backend);
- `test_multi_rule_rejection_consumes_nothing`;
- `test_processes_multi_rule_standalone_is_fully_atomic` (8 OS processes);
- `test_rules_in_one_dependency_are_atomic`, `test_multi_rate_string_is_atomic`.

**Exception (documented):** Redis Cluster batches spanning several hash slots. Phase 1
evaluates read-only; phase 2 commits each slot group with an atomic re-evaluation. A
later group may reject after earlier groups committed, which sets `partial_commit=True`.
This never over-admits. Enforced by `test_commit_reevaluates_and_reports_partial_commit`
and `test_processes_multi_rule_cluster_cross_slot`, which checks the accounting identity
consumed = admitted + partial.

## 2. Lua equals the Python reference

`batch.lua` must produce the same results, stored state string and TTL as
`algorithms/*` with `evaluate_batch` / `peek_batch`, for every input.

**Why:** the memory backend and tests use Python; production uses Lua. Any divergence
means production behaves differently from what was tested.

**Rules:**
- Identical operation order for every floating-point expression (CPython and Lua 5.1 are
  both IEEE-754 doubles).
- `EPS = 1e-9`.
- Numbers stored or returned via `%.17g`, because Lua's `tostring` keeps only 14 digits.

**Enforced by:**
- `test_single_op_consume_matches_reference`, `test_batch_consume_matches_reference`,
  `test_peek_matches_reference`, `test_sequences_match_reference`;
- `test_eps_boundaries`: constructed inputs where a 1-ulp order change flips the decision;
- `test_numerical_boundaries`.

Mutation-checked: 8/8 deliberate Lua bugs were caught.

## 3. Never over-admit

**Enforced by:**
- `test_threads_same_identity_never_over_admit` (forced GIL switching);
- `test_asyncio_gather_never_over_admits`;
- `test_processes_never_over_admit_one_identity` (8 processes, standalone and Cluster);
- `test_concurrent_requests_never_over_admit` (ASGI);
- `test_multiple_workers_share_exact_limits` (3 uvicorn workers);
- `test_concurrent_cancellations_never_over_admit`;
- Django: `test_concurrent_asgi_requests_never_over_admit`, `test_concurrent_wsgi_threads_never_over_admit`,
  `test_two_application_instances_share_exact_limits` and `test_workers_share_exact_limits`
  (3 uvicorn and 3 gunicorn workers over TCP, standalone and Cluster).

## 4. No blocking I/O on async paths

Async methods use `redis.asyncio` clients, one per event loop (they are loop-bound).
FastAPI limiting always runs on the loop, before sync endpoints are dispatched to the
threadpool.

**Enforced by:** `test_async_path_never_uses_the_sync_client` and
`test_async_request_paths_never_create_a_sync_client`, which assert that the sync client
is never created.

## 5. No raw identities in storage or logs

Keys contain a 22-character BLAKE2b digest of `kind ‖ 0x00 ‖ value`, keyed with
`identity_secret`. Kinds cannot contain NUL, so the encoding is unambiguous. `Identity`
reprs and log messages redact values.

**Enforced by:** `test_identity_digest_properties`,
`test_identity_secret_keys_are_hashed_in_redis` and
`test_resolver_fallback_chain_and_malformed_values` (which checks that the log is redacted).

## 6. Failure semantics

| Condition | Result |
|---|---|
| Connection error, timeout, cluster down, `OOM`, `READONLY`, `TRYAGAIN`, `BUSY`, breaker open, pool exhausted | `BackendUnavailable`: `fail_mode` applies |
| Authentication or permission error | `BackendError`; never fails open |
| Corrupt or foreign state, algorithm mismatch, `WRONGTYPE` | `BackendError`; nothing written |

Pool exhaustion never starts the cooldown. Any reply from Redis closes the breaker.

**Enforced by:** `test_authentication_failure_is_an_error_not_an_outage`,
`test_malformed_state_raises_and_is_not_overwritten`,
`test_one_corrupt_key_aborts_the_batch_without_writes`,
`test_other_errors_are_never_subject_to_fail_open` and the reliability suite.

## 7. Time

- **Redis:** server `TIME`, read inside the script; never the application host's clock.
- **Memory backend:** a monotonic clock.
- Integer microseconds everywhere. Keys get `PX = ceil(ttl/1000) + 1`, so they never
  expire before the model's TTL.

**Enforced by:** `test_server_time_is_used_by_default`,
`test_keys_never_expire_before_the_model_ttl` (via `PEXPIRETIME`) and
`test_state_is_irrelevant_once_ttl_expires`.

## 8. The core stays framework-free and dependency-free

`core/`, `algorithms/`, `backends/` (except `backends/redis/`, which needs redis-py) and
`http.py` import no web framework. The core wheel has no runtime dependencies (checked by
CI's wheel job).

## 9. Versioned keys

Schema `v1`: `<ns>:v1:rl:<rule>:<algo>:{<digest>}`. Stored values are `fw:…`, `sw:…`
and `tb:…` strings. Any incompatible change bumps the version. The hash tag must stay
the only braces in the key; this is checked against a real server's `CLUSTER KEYSLOT`
(`test_hash_slots_match_redis`, `test_cluster_is_real_and_slots_match_the_server`).

## 10. A counter is consumed once per request (Django and DRF)

The middleware, each `@ratelimit` decorator and the DRF throttles are separate atomic
steps. Storage keys that an earlier step consumed are passed on as `skip_keys` and left
out of later steps. All UniLimit throttles of one DRF view are planned and consumed as
one batch (the first to run evaluates for the group; the others read the decision). A
UniLimit throttle and a DRF built-in throttle on the same scope would count a request
twice, so `ImproperlyConfigured` is raised at request time and `manage.py check` reports
`unilimit.E002`.

**Why:** the maintainer's M5 brief: "Do not silently run the same policy through both
UniLimit and DRF's built-in throttling", and no double consumption.

**Enforced by:**
- `test_a_rule_in_middleware_and_decorator_is_consumed_once`;
- `test_throttle_and_decorator_with_one_rule_consume_once`;
- `test_several_throttles_are_one_atomic_batch`;
- `test_builtin_and_unilimit_throttles_on_one_scope_are_refused`;
- `test_checks_run_through_django`.

## 11. Async-safe Django integration

Under ASGI the async backend only; under WSGI the sync backend only. Django runs async
views served by WSGI in a short-lived loop per request, and async Redis clients are bound
to their loop, so the integration uses the sync backend in a worker thread there instead
of creating (and leaking) a client per request. DRF throttles are synchronous by DRF's
design; under ASGI DRF runs them in a thread, which is why they use the sync client.

**Enforced by:** `test_wsgi_async_views_use_the_sync_client`,
`test_throttles_under_asgi` and the leak checks (`filterwarnings = error`, GC after
each test).

## 12. Observability never affects enforcement

Events, observers, metrics and logging are emitted after a decision is made. An observer
that raises (or an event that fails to build) is caught and logged by category; the
request proceeds exactly as without it. Events contain rule names, algorithms, backend
kind, outcomes and timings only: never identities, IPs, storage keys, paths, headers or
error messages. Without an observer no event is built. Failure logs are throttled per
category, process-locally, and logging errors are swallowed.

**Enforced by:** `test_observer_exceptions_never_change_decisions`,
`test_observer_exceptions_async`, `test_building_an_event_cannot_break_enforcement`,
`test_no_event_is_built_without_an_observer`, `test_events_never_contain_sensitive_data`,
`test_prometheus_never_exposes_identities_or_paths`, `test_event_schema_is_stable`,
`test_throttling_is_exact_under_concurrent_failures`,
`test_a_burst_of_failures_through_the_limiter_logs_once`,
`test_a_failing_log_handler_never_raises`, `test_metrics_are_exact_under_concurrency`.

Mutation-checked: letting observer exceptions propagate, and disabling suppression, each
fail tests.

## 13. The local fallback only tightens fail-open

`Limiter(fallback=...)` is off by default and used only when the backend raised
`BackendUnavailable` and **every enforcing rule's effective `fail_mode` is open**. A
closed rule (or closed limiter default) is never given to the fallback: the fallback
would admit what fail-closed rejects. Its counters are per process and start empty; they
are not merged into Redis on recovery and expire by TTL. `peek` never uses it. Its
decisions are marked `Decision.fallback` / `RateLimitResult.fallback`, logged and emitted.

**This is the one documented exception to invariant 3 (never over-admit):** with N
workers the effective limit during an outage is up to N times the configured one. It is
explicit opt-in, so the default behaviour is unchanged.

**Enforced by:** `test_fallback_never_loosens_a_fail_closed_rule`,
`test_fallback_default_fail_mode_closed_stays_closed`,
`test_fail_closed_rules_do_not_use_the_fallback_on_real_redis`,
`test_fallback_is_off_by_default`, `test_fallback_enforces_locally_and_is_visible`,
`test_fallback_deactivates_on_recovery_and_does_not_double_consume`,
`test_peek_never_uses_the_fallback`, `test_fallback_serves_during_a_real_outage_and_stops_on_recovery`,
`test_cluster_primary_crash_is_observable`.

Mutation-checked: removing the fail-closed check fails three tests.
