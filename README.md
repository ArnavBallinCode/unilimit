# UniLimit

One consistent, production-grade rate-limiting core for Django, FastAPI and plain Python.

> **Status: pre-alpha, under active development. Not on PyPI yet.**
> Implemented so far (Milestone 2):
> - the framework-independent core;
> - three algorithms: fixed window, sliding window counter and token bucket;
> - atomic multi-rule evaluation;
> - the in-memory backend, with sync and async APIs.
>
> The Redis backend and the Django/FastAPI integrations are not implemented yet.
> See [ARCHITECTURE.md](ARCHITECTURE.md) and [API_DESIGN.md](API_DESIGN.md).

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
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run mypy
```

Licensed under MIT.
