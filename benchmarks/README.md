# Benchmarks

Reproducible comparison of UniLimit with `limits`, SlowAPI, PyrateLimiter and
fastapi-limiter. Methodology, results and caveats are published in
[docs/observability/benchmarks.md](../docs/observability/benchmarks.md); the raw JSON of
every run is in [results/](results/).

```bash
uv sync --group bench                              # competitor libraries (not part of dev)
uv run --group bench python -m benchmarks.run --n 20000
uv run --group bench python -m benchmarks.run --quick          # 30-second smoke test
uv run --group bench python -m benchmarks.run --suites decision --only unilimit
python -m benchmarks.report benchmarks/results/*.json > /tmp/tables.md
```

It starts its own `redis-server` (or `valkey-server`) on a free port and never touches
an existing Redis. `UNILIMIT_REDIS_SERVER` or `--redis-binary` selects the binary.

| Module | What |
|---|---|
| `contenders.py` | One adapter per library, algorithm, backend and sync/async mode. |
| `decision.py` | Library-level: one decision per call, throughput, p50/p95/p99, CPU time, Redis round trips and server commands per decision. |
| `http.py` | A FastAPI endpoint guarded by each library, driven through ASGI in-process. |
| `workers.py` | 1/2/4 processes on one Redis: scaling, and exactness of the limit on a shared key. |
| `report.py` | Raw results to markdown tables. |

## Rules of the comparison

- Each library is configured as its documentation recommends for **per-key** limits.
  (PyrateLimiter's built-in limiters share one bucket across all names; per-key limits use
  its `BucketFactory`, with one bucket per key.)
- Algorithms are compared by **semantics**, not by name (see the methodology page).
  Unequal semantics are labelled, never silently compared.
- `allow` scenario: limit far above the request count, 1,000 rotating keys.
  `reject` scenario: limit 1, one key. Both are measured; the tables show `allow`.
- Round trips are counted client-side by wrapping redis-py's `send_packed_command`
  (a pipeline counts once). `limits`' asyncio Redis storage uses coredis, which this
  cannot see: reported as `n/a`; the server-side command counts in the JSON still apply.
- CPU time is the process's `time.process_time()` per decision, measured around the loop.
- A desktop is a noisy machine. Treat differences under ~20% as noise; the report shows
  every run side by side.
