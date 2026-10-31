# Choosing a Python rate limiter

Reviewed 2026-11-01 against the linked upstream documentation and source. These are
selection notes, not claims that one library wins every workload. Check the version you
install: upstream implementations and APIs change.

- [SlowAPI](slowapi-vs-unilimit.md): familiar FastAPI/Starlette decorators backed by limits.
- [django-ratelimit](django-ratelimit-vs-unilimit.md): Django cache-based decorators.
- [limits](limits-vs-unilimit.md): lower-level strategies with a broader storage selection.
- [FastAPI-Limiter](fastapi-limiter-vs-unilimit.md): focused FastAPI dependencies and WebSockets.

UniLimit is worth evaluating when you need consistent framework APIs, explicit identity
and outage policies, or multiple rules consumed together. Batch atomicity covers memory,
standalone Redis, and a single Cluster slot. Cross-slot batches may partially consume;
middleware and endpoint stages are separate batches. Its compatibility adapters are
[limited](../migration.md), and [benchmarks](../observability/benchmarks.md) show workloads
where `limits` is faster. Choose on measured behavior and integration needs.
