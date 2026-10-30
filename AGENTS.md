# AGENTS.md

Instructions for AI coding agents (and humans) working on **UniLimit**: a rate-limiting
library for Python with one core shared by FastAPI, Django (planned) and plain Python,
backed by Redis or memory. This file is the source of truth for agent instructions;
`CLAUDE.md` and `.github/copilot-instructions.md` point here.

Read this file first. Read the [`context/`](context/README.md) file that matches your
task before changing code.

## Status

Pre-alpha, built milestone by milestone. Done: core, algorithms, memory backend, Redis
backend (standalone and Cluster), FastAPI integration. Next: Django (M5). Details are
in [`context/status.md`](context/status.md).

## Commands

```bash
uv sync                                   # install everything (Python >= 3.11)
uv run pytest                             # full suite (~1,200 tests, ~90 s)
uv run pytest tests/unit                  # fast, no Redis needed
uv run ruff check . && uv run ruff format --check .
uv run mypy                               # strict, src and tests
```

Redis tests start their own `redis-server` processes on random ports and never touch
an existing Redis. They need `redis-server` (and `redis-cli` for clusters) on `PATH`,
or set:

- `UNILIMIT_REDIS_SERVER=/path/to/redis-server` (or `valkey-server`) to pick the binary;
- `UNILIMIT_REQUIRE_REDIS=1` to fail instead of skip when no binary is found (CI does).

Other useful runs:

```bash
uv run --with fastapi==0.115.0 pytest tests/fastapi_integration   # oldest supported FastAPI
HYPOTHESIS_PROFILE=explore uv run pytest tests/unit/algorithms    # deep property fuzzing
UNILIMIT_DIFF_EXAMPLES=5000 uv run pytest tests/redis_backend/test_lua_differential.py
```

A change is done only when the full suite, Ruff, `ruff format --check` and mypy pass.

## Repository map

```
src/unilimit/
  core/            Rule, Rate, Identity, RequestContext, policy (sans-I/O), Limiter
  algorithms/      pure reference implementations: fixed/sliding window, token bucket
  backends/        protocols + evaluate_batch (reference), memory, redis/ (Lua + client)
  http.py          framework-free headers and rejection responses
  integrations/    fastapi/ (middleware, RateLimit, WebSocketRateLimit, setup)
tests/             unit/, conformance/, concurrency/, redis_backend/, fastapi_integration/
docs/fastapi/      user guides; every ```python block is executed by the tests
context/           agent-oriented knowledge: status, decisions, lessons, map, testing
ARCHITECTURE.md    design and rationale (authoritative)
API_DESIGN.md      public API (authoritative)
```

Full module-by-module map: [`context/codebase-map.md`](context/codebase-map.md).

## Invariants: never break these

Each one is protected by tests. If a change seems to require breaking one, stop and
ask; do not weaken the test. Rationale is in [`context/invariants.md`](context/invariants.md).

1. **Atomic batches.** A backend `consume` commits every op or none. A rejected request
   consumes nothing. Never add compensating decrements.
2. **Lua equals Python.** `backends/redis/batch.lua` is a line-by-line port of
   `algorithms/*` and `backends/base.evaluate_batch`. Change both together, keep the
   same floating-point operation order, and run the differential tests.
3. **Never over-admit.** Not under concurrency, not across processes, not on Redis
   Cluster (where a multi-slot batch may over-consume and reports `partial_commit`).
4. **No blocking I/O on async paths.** Async code uses `redis.asyncio` only. Never
   call sync Redis from a coroutine.
5. **No raw identities in storage.** Keys contain keyed digests only. Never log identity
   values.
6. **Failure semantics.** Only `BackendUnavailable` is subject to `fail_mode`. Auth
   errors, corrupt state and bugs raise. A wrong password must never fail open.
7. **Time.** Redis uses server `TIME`; times are integer microseconds; stored floats use
   `%.17g`.
8. **Core stays framework-free.** `core/`, `algorithms/`, `backends/` and `http.py`
   never import FastAPI, Starlette or Django. Core has no runtime dependencies.
9. **Keys are versioned.** Changing the stored format or key schema requires a new
   schema version (`v1` today).

## Coding conventions

- Python 3.11+, full type hints, mypy strict, Ruff (line length 100). No `Any` leaks
  in public APIs.
- Frozen, slotted dataclasses for value types. Validate at construction and raise
  `ConfigurationError` early, never on the request path.
- Match the existing style. Comments explain *why*, not what. No commented-out code.
- Public API changes must update `API_DESIGN.md`, the user docs and `context/decisions.md`.
- Keep integrations thin: every decision goes through `Limiter` and the policy layer.

## Testing rules

Details: [`context/testing.md`](context/testing.md).

- **No feature without tests. Never weaken a test to make code pass.** Fix the code, or
  explain why the test was wrong.
- Use **real Redis** (the fixtures start servers) for anything touching Redis. Mocks are
  for fault injection only.
- Use `ManualClock` for time. Assert exact counts, not timings.
- New algorithm or Lua logic needs Hypothesis properties *and* differential tests, and
  should be mutation-checked (break the code and confirm a test fails).
- Every `docs/fastapi/*.md` page is executed by `tests/fastapi_integration/test_docs.py`.
  Examples must run as written, using `redis://localhost:6379/0`, which the test
  substitutes.
- Tests run a GC pass after each Redis or FastAPI test. A `ResourceWarning` means you
  leaked a client or socket: close it.
- Never name a test directory after a third-party package (`tests/redis/`,
  `tests/fastapi/`): it shadows the package for Ruff.

## Workflow

1. Read the relevant `context/` files and the code you will touch.
2. For anything non-trivial, check `context/decisions.md`: the question may already be
   settled.
3. Make the change with tests. Run the full checks.
4. Update docs and `context/` (status, decisions, lessons) in the same change.
5. Commit with a descriptive message (`feat:`, `fix:`, `docs:`, `test:`, `ci:`).

When you hit a non-obvious bug, add it to [`context/lessons.md`](context/lessons.md)
with its root cause, so the next agent does not repeat it.

## Borrowing from other projects

Reusing ideas or code from slowapi, limits, django-ratelimit, fastapi-limiter and others
is welcome, under these rules:

- Check the licence first. MIT: keep the copyright notice. Apache-2.0: keep the licence
  and NOTICE, and state changes.
- Record every adoption, idea or code, in [`context/prior-art.md`](context/prior-art.md).
  Copied code also goes in `THIRD_PARTY_NOTICES.md` with its licence text.
- Adapt to our architecture (atomic batches, async, sans-I/O policy). Never import their
  weaknesses (sync storage on async paths, blind `X-Forwarded-For`, non-atomic multi-limits).

## Do not

- Do not commit secrets, `.venv*`, `dist/` or build artefacts.
- Do not touch a developer's own Redis (port 6379); tests use their own servers.
- Do not start a new milestone's scope without being asked.
- Do not claim something works without running the tests that prove it.
