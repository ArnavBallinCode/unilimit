# SlowAPI and UniLimit

Reviewed 2026-11-01. [SlowAPI](https://slowapi.readthedocs.io/en/latest/) offers
FastAPI/Starlette decorators and builds on `limits`. It is a reasonable choice when an
existing application relies on its decorator and configuration conventions.

## What SlowAPI already offers

Its [API documentation](https://slowapi.readthedocs.io/en/latest/api/) includes default,
application, and shared limits, exemptions, and in-memory fallback. The
[current source](https://github.com/laurentS/slowapi/blob/master/slowapi/extension.py)
also supports integer and callable costs. These are not exclusive UniLimit features.
The documentation notes that handlers need an explicit request argument and that
WebSocket endpoints are not supported.

## Why evaluate UniLimit?

UniLimit's native FastAPI integration uses dependencies and async Redis I/O. It also
provides Django/DRF integration and WebSocket message and connection limiters.
Several rules in a single native batch consume all or none in memory, standalone Redis,
or one Cluster slot. Independent upstream limit checks should not be confused with a
race inside a single storage operation: the distinction is transactional scope.

UniLimit does not make cross-slot Cluster batches atomic, and global and endpoint rules
remain separate steps. Its additional policy work has a cost; consult the
[benchmarks](../observability/benchmarks.md) rather than assuming it is faster.

## Migration

Use the [native FastAPI guide](../fastapi/quickstart.md) for new code, or change imports
first with the [SlowAPI adapter](../fastapi/migrating-from-slowapi.md). The adapter
supports decorators, shared limits, exemptions, default and application limits, and
success headers; options it cannot honour, such as `moving-window` or URL-scoped
counters, raise an error rather than being ignored.
