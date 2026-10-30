# Status

_Last updated: 2026-10-30, after Milestone 5 (Django and DRF)._

## Milestones

| # | Scope | State |
|---|---|---|
| M1 | Architecture and API design, prior-art research | Done; revised after review (consume/peek, atomic batches, `on_missing`) |
| M2 | Core models, algorithms, memory backend, sync/async APIs | Done |
| M3 | Redis backend: Lua, standalone and Cluster, reliability | Done |
| M4 | FastAPI integration: middleware, `RateLimit`, headers, lifecycle, docs | Done |
| — | Prior-art adoptions: `limits` rate grammar, `WebSocketRateLimit`, HTTP-date `Retry-After` | Done |
| M5 | Django: middleware (sync/async), `@ratelimit`, DRF throttles, `header:` keys, `ipv4_prefix`, docs | **Done**; CI green on `45f4492` |
| M6 | Observability hooks, log throttling, security/reliability hardening | Planned |
| M7 | Benchmarks, MkDocs site, packaging, release | Planned |

Milestones start only when the maintainer asks.

## CI

GitHub Actions (`.github/workflows/ci.yml`), 18 jobs:
- lint (Ruff and mypy);
- tests on Python 3.11–3.14;
- Django 5.2–6.1 with DRF 3.16–3.18 (`django-versions` job);
- Redis 6.2.17 / 7.2.7 / stable (8.x) / Valkey 8.0.2, built from source;
- FastAPI 0.115–0.162 with Starlette 0.38–1.10;
- wheel install checks.

Last fully green run: commit `45f4492` (M5, PR #1): 19/19 checks. Django jobs: 188 tests each on
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
- The limiter logs a warning on every fail-open request; throttling is M6.
- Planned adoptions from prior art (see `prior-art.md`): `in_memory_fallback`-style
  degraded mode (M6).
- Django/DRF limits (M5):
  - `request.user` is not set until DRF authenticates, so `@ratelimit(key="user")` cannot
    see DRF token or JWT users; use the DRF throttles for those.
  - DRF throttles are synchronous, so they use the sync Redis client (in a thread under
    ASGI).
  - `query:` and `post:` key sources are not implemented (callables cover them).
  - Streaming responses get headers, but a streaming body is never limited mid-stream.
