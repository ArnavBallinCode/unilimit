# limits and UniLimit

Reviewed 2026-11-01. [limits](https://limits.readthedocs.io/en/stable/) provides
rate-limiting strategies and storage backends for applications or framework extensions.
Its Redis, Memcached, and MongoDB support makes it a strong option when storage choice
or a small low-level abstraction matters.

## Both support async applications

The [async API](https://limits.readthedocs.io/en/stable/async.html) mirrors its synchronous
API. Async Redis can use coredis or redis-py; describing it as coredis-only is outdated.
UniLimit uses redis-py for Redis and adds request identity resolution, framework responses,
observability hooks, and connection leases around the limiting algorithms.

## Different scope and costs

Independent `limits` hits are not a transaction spanning multiple rules. UniLimit adds
atomic batch evaluation for memory, standalone Redis, and same-slot Redis Cluster rules.
Cross-slot and middleware/route boundaries still apply. UniLimit's sliding-window counter
is an approximation, not a substitute for an exact moving-window log.

UniLimit's [published benchmark runs](../observability/benchmarks.md) include slower
library-level results than `limits`. Choose UniLimit for its integrated policies and
framework behavior when those justify the overhead; benchmark your own workload.
There is no universal performance advantage or import-compatible migration.
