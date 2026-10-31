# UniLimit

**Rate limiting for FastAPI, Starlette, Django, DRF, and plain Python.**

[![CI](https://github.com/ArnavBallinCode/unilimit/actions/workflows/ci.yml/badge.svg)](https://github.com/ArnavBallinCode/unilimit/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/unilimit)](https://pypi.org/project/unilimit/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Use one rule model for synchronous and asynchronous applications, with memory for local
experiments and Redis for shared counters across workers. Python 3.11+.

[Documentation](docs/README.md) · [How it works](docs/concepts.md) ·
[Deployment checklist](docs/deployment.md) · [Comparisons](docs/comparisons/README.md)

## Try it in two minutes

Install FastAPI support and an application server:

```bash
pip install "unilimit[fastapi]" uvicorn
```

Save this as `app.py`. No Redis is needed for this single-process example:

```python
from fastapi import Depends, FastAPI

from unilimit import Limiter, MemoryBackend
from unilimit.integrations.fastapi import RateLimit, setup

app = FastAPI()
limiter = Limiter(MemoryBackend())
setup(app, limiter)


@app.get("/search", dependencies=[Depends(RateLimit("search", rate="2/minute"))])
async def search() -> dict[str, str]:
    return {"status": "ok"}
```

Run `uvicorn app:app`, then in another terminal:

```bash
curl -i http://127.0.0.1:8000/search
curl -i http://127.0.0.1:8000/search
curl -i http://127.0.0.1:8000/search
```

Run these requests together: the first two return **200**, the third **429 Too Many
Requests** with a `Retry-After` header. The default key is the client IP. Restarting
this demo resets its counters; multiple workers each have their own budget.

## Use Redis across workers

Install `pip install "unilimit[fastapi,redis]" uvicorn` and replace the limiter above
with `Limiter.from_url("redis://localhost:6379/0")`, pointing at a Redis instance you
operate. Keep `setup(app, limiter)` for middleware and shutdown cleanup.

The default outage policy is **fail-open**: an unavailable backend permits requests.
Choose `fail_mode="closed"` when availability must yield to enforcement, and read the
[deployment guide](docs/deployment.md) before protecting sensitive endpoints.

## Choose your integration

- **FastAPI:** [quickstart](docs/fastapi/quickstart.md),
  [authenticated identities](docs/fastapi/authentication.md),
  [WebSocket message and connection limits](docs/fastapi/websockets.md).
- **Django:** install `"unilimit[django,redis]"`; follow the
  [settings, middleware, and view guide](docs/django/quickstart.md).
- **DRF:** install `"unilimit[drf,redis]"`; follow the
  [throttle and rate settings guide](docs/drf/quickstart.md). Throttles run after DRF
  authentication; configure `DEFAULT_THROTTLE_RATES` for named scopes.
- **Starlette:** install `"unilimit[starlette,redis]"` (FastAPI is not needed); use the
  ASGI middleware and WebSocket helpers from `unilimit.integrations.starlette`.
- **Plain Python:** install `unilimit`; use the example below.
- **Coming from SlowAPI or django-ratelimit?** Change the imports first with the
  [SlowAPI](docs/fastapi/migrating-from-slowapi.md) or
  [django-ratelimit](docs/django/migrating-from-django-ratelimit.md) adapter guide.

```python
from unilimit import Limiter, MemoryBackend, Rule

limiter = Limiter(MemoryBackend())
rule = Rule("checkout", "5/minute")
decision = limiter.consume(rule, identity="user:123")
print(decision.allowed, decision.remaining)
limiter.close()
```

Use `await limiter.aconsume(...)` and `await limiter.aclose()` in asynchronous code.

## Why consider UniLimit?

- Apply several rules in one batch: a rejected batch consumes nothing in memory,
  standalone Redis, or a single Redis Cluster slot.
- Share rule names, identity handling, and outage policies across frameworks.
- Choose fixed windows, sliding window counters, or token buckets.
- Use async Redis I/O, trusted-proxy handling, and identity digests in storage keys.
- Observe decisions with [Prometheus, OpenTelemetry, or custom observers](docs/observability/metrics.md).
- Limit WebSocket message rates and active connections separately.

## Know the boundaries

Redis Cluster batches spanning slots can partially consume quota (`partial_commit`).
Middleware and endpoint rules are separate steps: endpoint rejection does not refund
middleware quota. Memory storage and outage fallback are per process, and memory
capacity eviction can reset a live counter. Sliding windows are estimates, not exact
logs of every request. Rate limiting does not replace authentication or edge DDoS protection.

UniLimit is a young project. The [published benchmarks](docs/observability/benchmarks.md)
include workloads where it is slower than `limits`; there is no blanket speed advantage.
Compatibility adapters accept the upstream options they can honour, reject the rest, and
list their differences: read the [migration guide](docs/migration.md) before switching
imports.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup, checks, and useful issue reports.
For API details, see [API_DESIGN.md](API_DESIGN.md); for coding assistants, start with
[llms.txt](llms.txt). Bugs and adoption feedback belong in
[GitHub issues](https://github.com/ArnavBallinCode/unilimit/issues).

## License

[MIT](LICENSE). [Prior art](context/prior-art.md) and
[third-party notices](THIRD_PARTY_NOTICES.md) document attribution.
