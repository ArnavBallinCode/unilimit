# Contributing to UniLimit

Read [AGENTS.md](AGENTS.md) and the relevant [context](context/README.md) before changing
code. Small reproductions, tested documentation fixes, and reports from real integrations
are particularly useful.

## Local setup

Install Python 3.11+, uv, and Redis server/CLI binaries, then run:

```bash
uv sync
uv run pytest tests/unit
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
```

Redis tests start isolated servers on random ports; they never use your development Redis.
Set `UNILIMIT_REQUIRE_REDIS=1` to fail rather than skip if Redis binaries are missing.
See [testing details](context/testing.md) for framework matrices and differential tests.

## A useful bug or adoption report

Include Python, UniLimit, framework and Redis versions; a minimal runnable example;
expected and actual responses; worker count; and whether storage is memory, Redis, or
Cluster. Redact credentials and identities. For performance reports include the command,
concurrency, algorithm, identity distribution, and raw measurements.

Open a [GitHub issue](https://github.com/ArnavBallinCode/unilimit/issues) before a substantial
feature so its semantics can be agreed. A pull request should explain the behavior change,
include regression tests, and update the relevant user guide and context. All full-suite,
Ruff, formatting, and mypy checks must pass. Do not claim a performance improvement without
reproducible benchmark data.
