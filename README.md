# UniLimit

One consistent, production-grade rate-limiting core for Django, FastAPI and plain Python.

> **Status: pre-alpha, under active development. Not on PyPI yet.**
> Implemented so far (Milestones 2–6):
> - the framework-independent core;
> - three algorithms: fixed window, sliding window counter and token bucket;
> - atomic multi-rule evaluation;
> - the in-memory backend;
> - the Redis backend (standalone and Cluster, sync and async), tested against real Redis 6.2–8.8 and Valkey 8;
> - the FastAPI integration (middleware, `RateLimit` dependency, headers, lifecycle): [docs/fastapi](docs/fastapi/quickstart.md);
> - the Django integration (middleware, `@ratelimit`, sync and async) and Django REST Framework throttles: [docs/django](docs/django/quickstart.md), [docs/drf](docs/drf/quickstart.md);
> - observability: structured events, Prometheus and OpenTelemetry exporters, throttled failure logging, an opt-in local fallback, and [reproducible benchmarks](docs/observability/benchmarks.md): [docs/observability](docs/observability/events.md).
>
> See [ARCHITECTURE.md](ARCHITECTURE.md) and [API_DESIGN.md](API_DESIGN.md).

FastAPI:

```python
from fastapi import Depends, FastAPI
from unilimit import Limiter, Rule
from unilimit.integrations.fastapi import RateLimit, setup

app = FastAPI()
setup(app, Limiter.from_url("redis://localhost:6379/0", global_rules=[Rule("site", "300/minute")]))


@app.get("/search", dependencies=[Depends(RateLimit("search", rate="10/minute"))])
async def search(): ...
```

Django:

```python
# settings.py: UNILIMIT = {"URL": "redis://localhost:6379/0"}, and add
# "unilimit.integrations.django.UniLimitMiddleware" to MIDDLEWARE
from unilimit.integrations.django import ratelimit


@ratelimit("search", rate="10/minute")
def search(request): ...
```

Django REST Framework: replace `rest_framework.throttling.UserRateThrottle` with
`unilimit.integrations.drf.UniLimitUserRateThrottle` and keep your settings.

Framework-independent:

```python
from unilimit import Limiter, Rule

limiter = Limiter.from_url("memory://", rules=[Rule("api", rate="100/minute")])

result = limiter.consume("api", "user:123")  # atomic evaluate-and-consume
if not result.allowed:
    print(f"retry in {result.retry_after:.1f}s")

result = await limiter.aconsume("api", "user:123")  # async twin
limiter.peek("api", "user:123")  # never mutates state
```

## Acknowledgements

UniLimit builds on ideas from [limits](https://github.com/alisaifee/limits),
[slowapi](https://github.com/laurentS/slowapi),
[django-ratelimit](https://github.com/jsocol/django-ratelimit) and
[fastapi-limiter](https://github.com/long2ice/fastapi-limiter). What was adopted, and
why some things were not, is recorded in [context/prior-art.md](context/prior-art.md).

## Contributing (humans and AI agents)

Start with [AGENTS.md](AGENTS.md); deeper project knowledge is in [context/](context/README.md).

## Development

```bash
uv sync
uv run pytest                     # Redis tests use redis-server from PATH
UNILIMIT_REDIS_SERVER=/path/to/valkey-server uv run pytest   # any other server binary
uv run ruff check . && uv run ruff format --check .
uv run mypy
```

Licensed under MIT.
