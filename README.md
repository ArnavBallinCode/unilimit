# UniLimit

**One rate limiter. Every Python backend.**

[![CI](https://github.com/ArnavBallinCode/unilimit/actions/workflows/ci.yml/badge.svg)](https://github.com/ArnavBallinCode/unilimit/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/unilimit.svg)](https://pypi.org/project/unilimit/)
[![Python](https://img.shields.io/pypi/pyversions/unilimit.svg)](https://pypi.org/project/unilimit/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)

UniLimit is a unified rate-limiting, concurrency-limiting, and quota-enforcement library for
Python. It provides one battle-tested, framework-independent core shared seamlessly across
**FastAPI**, **Starlette**, **Django**, **Django REST Framework (DRF)**, and plain Python.

Backed by **Redis** (standalone, Cluster, Sentinel, TLS) or **in-memory storage**.

---

## Highlights

- **Atomic Multi-Rule Batches:** Evaluate multiple rules (e.g. 10/second burst + 10,000/day quota) in a single atomic operation. If any limit is exceeded, **none** are consumed. No compensating decrements.
- **Identical Python & Lua Semantics:** Our Redis Lua engine is a line-by-line mirror of the Python algorithms, validated with differential property-based fuzzing.
- **Distributed WebSocket Concurrency:** Cap active concurrent connections across distributed worker nodes with auto-expiring leases, heartbeats, and crash recovery.
- **Never Over-Admits:** Zero race conditions under concurrency or across processes.
- **No Blocking I/O on Async Paths:** True `asyncio` execution using `redis.asyncio`.
- **Zero Raw Identities in Storage:** Keys contain keyed HMAC-SHA256 digests. Raw IPs, usernames, and tokens are never written to storage or leaked in logs.
- **Production Observability:** Low-cardinality Prometheus metrics, OpenTelemetry integration, structured events, and throttled failure logging.
- **Fail-Open or Fail-Closed:** Fine-grained resilience strategies with optional local in-memory fallback during Redis outages.

---

## Installation

The core library has **zero external runtime dependencies**. Install extras for your stack:

```bash
# Core with in-memory backend
pip install unilimit

# Redis backend (standalone, Cluster, Sentinel, TLS)
pip install "unilimit[redis]"

# Framework integrations
pip install "unilimit[fastapi]"
pip install "unilimit[starlette]"
pip install "unilimit[django]"
pip install "unilimit[drf]"

# Observability exporters
pip install "unilimit[prometheus]"
pip install "unilimit[otel]"

# Or everything together
pip install "unilimit[all]"
```

---

## Quickstart

### 1. FastAPI

```python
from fastapi import Depends, FastAPI
from unilimit import Limiter, Rule
from unilimit.integrations.fastapi import RateLimit, setup

app = FastAPI()

# Configure limiter with global and route rules
limiter = Limiter.from_url(
    "redis://localhost:6379/0",
    global_rules=[Rule("site", rate="300/minute")],
)
setup(app, limiter)


# Rate limit specific route: 10 requests per minute
@app.get("/search", dependencies=[Depends(RateLimit("search", rate="10/minute"))])
async def search() -> dict[str, str]:
    return {"status": "ok"}
```

### 2. WebSockets & Connection Concurrency (FastAPI & Starlette)

```python
from fastapi import FastAPI, WebSocket
from unilimit import Limiter
from unilimit.integrations.fastapi import WebSocketConnectionLimit, WebSocketRateLimit

app = FastAPI()
limiter = Limiter.from_url("redis://localhost:6379/0")

# Limit concurrent connections per IP across all workers:
concurrency = WebSocketConnectionLimit(limiter, max_connections=5, key="ip")

# Limit message throughput: 20 messages per 10 seconds:
msg_limit = WebSocketRateLimit(limiter, rate="20/10s")


@app.websocket("/chat")
async def chat(ws: WebSocket):
    async with concurrency.connection(ws):
        await ws.accept()
        while True:
            text = await ws.receive_text()
            if not await msg_limit.consume(ws):
                await ws.send_text('{"error": "rate_limited"}')
                continue
            await ws.send_text(f"echo: {text}")
```

### 3. Django & Django REST Framework

In your `settings.py`:

```python
# settings.py
INSTALLED_APPS = [
    ...,
    "unilimit.integrations.django",
]

MIDDLEWARE = [
    ...,
    "unilimit.integrations.django.UniLimitMiddleware",
]

UNILIMIT = {
    "URL": "redis://localhost:6379/0",
    "OPTIONS": {
        "namespace": "myproject",
        "fail_mode": "open",
    },
}
```

Decorate Django views (sync or async):

```python
from django.http import HttpRequest, HttpResponse
from unilimit.integrations.django import ratelimit


@ratelimit("search", rate="10/minute", key="ip")
def search(request: HttpRequest) -> HttpResponse:
    return HttpResponse("search results")
```

Or use DRF Throttles:

```python
from rest_framework.response import Response
from rest_framework.views import APIView
from unilimit.integrations.drf import UniLimitUserRateThrottle


class ProfileView(APIView):
    throttle_classes = [UniLimitUserRateThrottle]

    def get(self, request):
        return Response({"user": request.user.username})
```

### 4. Plain Python (Framework-Free)

```python
from unilimit import Limiter, MemoryBackend, Rule

# In-memory or Redis backend
limiter = Limiter(MemoryBackend())
rule = Rule("checkout", rate="5/minute")

# Synchronous evaluation
decision = limiter.consume(rule, identity="user:123")
if decision.allowed:
    print(f"Admitted! Remaining: {decision.remaining}")
else:
    print(f"Rate limited. Retry after {decision.retry_after:.1f} seconds")

# Asynchronous evaluation
# decision = await limiter.aconsume(rule, identity="user:123")
```

---

## Documentation

Comprehensive guides and architectural specifications are available in the repository:

- **Framework Guides:**
  - [FastAPI Guide](docs/fastapi/quickstart.md)
  - [Django Guide](docs/django/quickstart.md)
  - [DRF Guide](docs/drf/quickstart.md)
  - [WebSockets & Concurrency](docs/fastapi/websockets.md)
- **Deployment & Operations:**
  - [Redis Configuration & Sentinel](docs/fastapi/redis.md)
  - [Observability & Metrics](docs/observability/events.md)
  - [Local Fallback](docs/observability/fallback.md)
  - [Security Hardening Guide](docs/security.md)
  - [Custom Storage Backends Contract](docs/custom_backends.md)
- **Engineering & Architecture:**
  - [Architecture Rationale](ARCHITECTURE.md)
  - [API Design Specification](API_DESIGN.md)
  - [Security Audit Report](context/security-audit.md)
  - [Benchmark Methodology](docs/observability/benchmarks.md)

---

## Testing & Quality Assurance

UniLimit enforces strict invariants tested against real Redis binaries:

```bash
uv sync
uv run pytest tests/unit                          # Fast in-memory unit tests (~2s)
uv run pytest                                     # Full suite (~1,500 tests across Redis & Cluster)
uv run ruff check . && uv run ruff format --check .
uv run mypy                                       # Strict typing, 0 errors
```

---

## License

UniLimit is open-source software licensed under the [MIT License](LICENSE).
Attributions and prior-art adaptations are catalogued in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)
and [context/prior-art.md](context/prior-art.md).
