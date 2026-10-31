# Status

_Last updated: 2026-11-01, adoption documentation audit._

## Current audit

Local package version and latest GitHub release are `0.1.1` (release published 2026-10-30).
The CI/environment details below are historical records, not new verification of every
listed matrix. See [adoption-audit.md](adoption-audit.md) for current findings, documentation
improvements, compatibility gaps, and candidate application integrations.

Local validation for this audit: `UNILIMIT_REQUIRE_REDIS=1 uv run pytest -q` passed
**1,576 tests** (104 seconds); Ruff, formatting, mypy, and `git diff --check` passed.
This is local validation, not a new GitHub CI run.

## Milestones

| # | Scope | State |
|---|---|---|
| M1 | Architecture and API design, prior-art research | Done; revised after review (consume/peek, atomic batches, `on_missing`) |
| M2 | Core models, algorithms, memory backend, sync/async APIs | Done |
| M3 | Redis backend: Lua, standalone and Cluster, reliability | Done |
| M4 | FastAPI integration: middleware, `RateLimit`, headers, lifecycle, docs | Done |
| — | Prior-art adoptions: `limits` rate grammar, `WebSocketRateLimit`, HTTP-date `Retry-After` | Done |
| M5 | Django: middleware (sync/async), `@ratelimit`, DRF throttles, `header:` keys, `ipv4_prefix`, docs | **Done**; CI green on `45f4492` |
| M6 | Observability: events, metrics (Prometheus, OpenTelemetry), throttled failure logs, opt-in local fallback, benchmark tooling and results | **Done**; CI green on `e26a377` (PR #2) |
| M7 | WebSocket maturity: distributed concurrent connection limits, leases, crash recovery, weighted budgets, rejection policies; Starlette integration | **Done** |
| M8 | Backend & Core maturity: Redis Sentinel, TLS, custom backend contract & test runner | **Done** |
| M9 | Security & reliability audit, release documentation | **Done** |
| M10 | Packaging, distribution pipeline, TestPyPI and PyPI release | **Done** (0.1.0 release ready) |

Milestones start only when the maintainer asks.

## CI

GitHub Actions (`.github/workflows/ci.yml`), 19 jobs:
- lint (Ruff and mypy);
- tests on Python 3.11–3.14 (1,472 tests);
- Django 5.2–6.1 with DRF 3.16–3.18 (`django-versions` job);
- Redis 6.2.17 / 7.2.7 / stable (8.x) / Valkey 8.0.2, built from source;
- FastAPI 0.115–0.162 with Starlette 0.38–1.10;
- wheel install checks.

Last fully green run: commit `e26a377` (M6, PR #2): 20/20 checks, 1,472 tests on Python
3.11–3.14, Redis 6.2.17 / 7.2.7 / stable / Valkey 8.0.2, FastAPI x5, Django x3, the wheel job
(including the `prometheus`, `otel` and `all` extras) and the benchmark-tooling smoke job.
Before that: `45f4492` (M5, PR #1): 19/19 checks. Django jobs: 188 tests each on
Django 5.2.18/DRF 3.16.1, 6.0.9/3.17.2 and 6.1.2/3.18.3; the wheel job installs the
`django`, `drf` and `all` extras and runs them. Two CI-only failures of
`test_primary_crash_and_failover` (async replication race, see `lessons.md`) were fixed on the way.

## Tested environments

- **Python:** 3.11, 3.12, 3.13, 3.14.
- **Servers:** Redis 6.2.17, 7.2.7, 8.8.0 and 8.10.2, and Valkey 8.0.2; standalone and a 3-primary/3-replica cluster.
- **FastAPI / Starlette:**
  - 0.115.0 / 0.38.6
  - 0.120.0 / 0.48.0
  - 0.140.0 / 0.46.0
  - 0.140.0 / 1.10.0
  - 0.162.0 / 1.10.0
- **Django / DRF** (the full `tests/django_integration` suite, 188 tests, on Redis and
  Cluster):
  - 5.2.18 / 3.16.1 (oldest supported)
  - 6.0.9 / 3.17.2
  - 6.1.2 / 3.18.3 (the locked development versions)
- **Servers for Django:** uvicorn (ASGI) and gunicorn (WSGI, threaded), 3 workers each.
- **Not yet tested:** Sentinel, TLS, load beyond one machine, Django with a production
  database under load (tests use SQLite). The Django suite ran locally on Python 3.13
  only; CI runs it on 3.11–3.14 through the `test` job.

## Known open items

Accepted limitations and risks are tracked in ARCHITECTURE.md §16. The ones to keep in
mind day to day:

- Global (middleware) and route (dependency) limits are separate atomic steps. A route
  rejection does not refund global quota.
- On Redis Cluster, batches spanning several slots are not atomic across slots:
  `partial_commit` (~4–5% of rejections under heavy contention).
- Failure logs are throttled per category and **per process** (M6); with N workers expect
  up to N first-occurrence lines per interval.
- M6 limits:
  - Observers run inline on the request path; a slow observer slows requests.
  - The local fallback is per process: up to N workers × limit during an outage, and its
    counters are not merged back into Redis on recovery (invariant 13).
  - Benchmarks (see `docs/observability/benchmarks.md`): UniLimit is **slower than
    `limits`** at the library level (about 3–5× in memory, 1.5–1.7× on Redis) and its
    asyncio Redis throughput at 32 in flight (10–17k/s) is about half of `limits`
    (coredis, 18–36k/s). Not yet investigated: candidates are the keyed digest, the
    single-rule path through the batch machinery, and the per-call gate/timeout in the
    async Redis path. One laptop, two runs differing by up to 1.7×: not conclusive.
  - OpenTelemetry has no spans of its own (see `decisions.md`).
- Django/DRF limits (M5):
  - `request.user` is not set until DRF authenticates, so `@ratelimit(key="user")` cannot
    see DRF token or JWT users; use the DRF throttles for those.
  - DRF throttles are synchronous, so they use the sync Redis client (in a thread under
    ASGI).
  - `query:` and `post:` key sources are not implemented (callables cover them).
  - Streaming responses get headers, but a streaming body is never limited mid-stream.
