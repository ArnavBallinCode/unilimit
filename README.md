# UniLimit

One consistent, production-grade rate-limiting core for Django, FastAPI and plain Python.

> **Status: pre-alpha, under active development. Not on PyPI yet.**
> Implemented so far (Milestones 2–4):
> - the framework-independent core;
> - three algorithms: fixed window, sliding window counter and token bucket;
> - atomic multi-rule evaluation;
> - the in-memory backend;
> - the Redis backend (standalone and Cluster, sync and async), tested against real Redis 6.2–8.8 and Valkey 8;
> - the FastAPI integration (middleware, `RateLimit` dependency, headers, lifecycle): [docs/fastapi](docs/fastapi/quickstart.md).
>
> The Django integration is not implemented yet.
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

## Development

```bash
uv sync
uv run pytest                     # Redis tests use redis-server from PATH
UNILIMIT_REDIS_SERVER=/path/to/valkey-server uv run pytest   # any other server binary
uv run ruff check . && uv run ruff format --check .
uv run mypy
```

Licensed under MIT.
