# Status

_Last updated: 2026-10-30, after Milestone 4 plus the prior-art adoptions._

## Milestones

| # | Scope | State |
|---|---|---|
| M1 | Architecture and API design, prior-art research | Done; revised after review (consume/peek, atomic batches, `on_missing`) |
| M2 | Core models, algorithms, memory backend, sync/async APIs | Done |
| M3 | Redis backend: Lua, standalone and Cluster, reliability | Done |
| M4 | FastAPI integration: middleware, `RateLimit`, headers, lifecycle, docs | Done |
| — | Prior-art adoptions: `limits` rate grammar, `WebSocketRateLimit`, HTTP-date `Retry-After` | Done |
| M5 | Django: middleware (sync/async), `@ratelimit`, DRF throttle | **Next — not started** |
| M6 | Observability hooks, log throttling, security/reliability hardening | Planned |
| M7 | Benchmarks, MkDocs site, packaging, release | Planned |

Milestones start only when the maintainer asks.

## CI

GitHub Actions (`.github/workflows/ci.yml`), 15 jobs:
- lint (Ruff and mypy);
- tests on Python 3.11–3.14;
- Redis 6.2.17 / 7.2.7 / stable (8.x) / Valkey 8.0.2, built from source;
- FastAPI 0.115–0.162 with Starlette 0.38–1.10;
- wheel install checks.

Last fully green run: commit `4914ab3` (M4).

## Tested environments

- **Python:** 3.11, 3.12, 3.13, 3.14.
- **Servers:** Redis 6.2.17, 7.2.7, 8.8.0 and 8.10.2, and Valkey 8.0.2; standalone and a 3-primary/3-replica cluster.
- **FastAPI / Starlette:**
  - 0.115.0 / 0.38.6
  - 0.120.0 / 0.48.0
  - 0.140.0 / 0.46.0
  - 0.140.0 / 1.10.0
  - 0.162.0 / 1.10.0
- **Not yet tested:** Sentinel, TLS, Django, load beyond one machine.

## Known open items

Accepted limitations and risks are tracked in ARCHITECTURE.md §16. The ones to keep in
mind day to day:

- Global (middleware) and route (dependency) limits are separate atomic steps. A route
  rejection does not refund global quota.
- On Redis Cluster, batches spanning several slots are not atomic across slots:
  `partial_commit` (~4–5% of rejections under heavy contention).
- The limiter logs a warning on every fail-open request; throttling is M6.
- Planned adoptions from prior art (see `prior-art.md`):
  - `in_memory_fallback`-style degraded mode (M6);
  - `header:` / `query:` keys and IPv4 masks (M5).
