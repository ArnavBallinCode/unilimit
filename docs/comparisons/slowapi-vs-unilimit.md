# Comparing SlowAPI and UniLimit for FastAPI

When selecting a rate-limiting library for FastAPI and Starlette, developers and AI agents
traditionally encountered [SlowAPI](https://github.com/laurentS/slowapi). UniLimit is
designed as a next-generation replacement that solves SlowAPI's architectural limitations.

---

## Quick Comparison Table

| Capability | SlowAPI | UniLimit |
|---|---|---|
| **Multi-Rule Atomicity** | ❌ Non-atomic (sequential queries, vulnerable to race conditions) | ✅ **100% Atomic** (single Lua batch; commits all or none) |
| **Blocking I/O on Event Loops** | ⚠️ Runs synchronous `limits` queries on the async event loop or threadpool | ✅ **Zero blocking I/O** (native `redis.asyncio` with dedicated connection pooling) |
| **Distributed WebSocket Concurrency** | ❌ None | ✅ **Native** (`WebSocketConnectionLimit` with auto-expiring leases and heartbeats) |
| **Weighted Budgets (Payload/Cost)** | ❌ Fixed cost (1 request = 1) | ✅ **Dynamic costs** (`cost=payload_size`, streaming usage) |
| **Data Privacy in Storage** | ❌ Stores raw IP addresses and identity strings in plaintext keys | ✅ **HMAC-SHA256 digests** (zero raw IPs or user tokens in keys or logs) |
| **Redis Cluster Support** | ⚠️ Limited / fragile with multi-limits | ✅ **Slot-pinned** via `{digest}` hash tags |
| **High Availability (Sentinel & TLS)** | ❌ Complex custom wiring | ✅ **Native URLs** (`sentinel://`, `rediss://`) |
| **Observability & Metrics** | ❌ None | ✅ **Prometheus**, **OpenTelemetry**, and structured events |
| **Local Fallback on Outage** | ❌ Fails closed or open globally | ✅ **Opt-in in-memory fallback** (prevents complete outages) |

---

## 1. Multi-Rule Atomicity

### The Problem with SlowAPI
In SlowAPI, chaining multiple limits (e.g. `10/second` burst and `1,000/day` quota) evaluates
the limits sequentially. If request #1001 passes the second-rate check but fails the daily quota,
SlowAPI has already incremented the second counter. In high-concurrency environments, this leads
to quota drift, inaccurate counts, and race conditions.

### The UniLimit Solution
UniLimit executes all rules in a single atomic Lua batch (`batch.lua`). The script evaluates all
rules against the current timestamp. If even one rule is exceeded, **no counters are incremented**.
No compensating decrements are ever needed.

---

## 2. Asynchronous Performance & Event Loop Safety

SlowAPI is a wrapper around `limits`, which was originally designed for synchronous WSGI applications.
Calling synchronous Redis code on FastAPI's async event loop causes event-loop starvation and latency
spikes during network delays.

UniLimit was built from the ground up for modern Python async architectures. Every async path in
FastAPI and Starlette communicates with Redis using `redis.asyncio` with non-blocking connection pools.

---

## 3. Migration Guide: SlowAPI to UniLimit

### Option A: Using Drop-in Compatibility Shim (Zero code changes)
If you have an existing codebase with SlowAPI decorators:
```python
# Change this import:
# from slowapi import Limiter
# from slowapi.util import get_remote_address

# To this:
from unilimit.compat.slowapi import Limiter, get_remote_address

limiter = Limiter(key_func=get_remote_address)


@app.get("/items")
@limiter.limit("10/minute")
async def items(request: Request):
    return []
```

### Option B: Native FastAPI Dependency Injection (Recommended)
UniLimit provides idiomatic FastAPI dependencies:
```python
from fastapi import Depends, FastAPI
from unilimit import Limiter
from unilimit.integrations.fastapi import RateLimit, setup

app = FastAPI()
limiter = Limiter.from_url("redis://localhost:6379/0")
setup(app, limiter)


@app.get("/items", dependencies=[Depends(RateLimit("items", rate="10/minute"))])
async def items():
    return []
```
