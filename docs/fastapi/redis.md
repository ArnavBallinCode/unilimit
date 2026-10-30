# Redis configuration and application lifecycle

## From a URL (recommended)

```python
from fastapi import Depends, FastAPI

from unilimit import Limiter, Rule
from unilimit.integrations.fastapi import RateLimit, setup

limiter = Limiter.from_url(
    "redis://localhost:6379/0",  # rediss:// for TLS, unix:///path/to/redis.sock, or sentinel://...
    namespace="shop",  # key prefix: isolates applications sharing a Redis
    global_rules=[Rule("site", "300/minute")],
    # Backend options (defaults shown):
    socket_timeout=0.5,
    socket_connect_timeout=1.0,
    max_connections=32,  # in-flight Redis calls per process; extra calls wait...
    pool_timeout=1.0,  # ...up to this long before counting as unavailable
    cooldown=0.5,  # after a Redis failure, fail fast, then probe once
)

app = FastAPI()
setup(app, limiter)


@app.get("/items", dependencies=[Depends(RateLimit("items", rate="60/minute"))])
async def items() -> list[str]:
    return []
```

Clients are created lazily, on first use. Async clients are created **once per event
loop** and reused by every request. Sync endpoints also use the async client, because
rate limiting runs on the event loop before your sync endpoint is called. UniLimit
never makes a blocking Redis call on an async request path.

For **Redis Cluster**, pass `cluster=True`. Keys are hash-tagged by identity, so all
of one identity's counters live in the same slot.

For **Redis Sentinel**, pass a `sentinel://` URL or construct a backend with
`RedisBackend.from_sentinel(...)`:
- URL format: `sentinel://sentinel1:26379,sentinel2:26379/service_name/0`
- Factory: `RedisBackend.from_sentinel(sentinels=[("sentinel1", 26379), ("sentinel2", 26379)], service_name="mymaster")`

For **TLS / SSL**, use the `rediss://` URL scheme or pass SSL options (such as
`ssl_cert_reqs`, `ssl_ca_certs`, `ssl_certfile`, `ssl_keyfile`) in `client_options`.

## Startup and shutdown

There is nothing to add to your lifespan. When the application shuts down, the
middleware closes the clients the limiter created. Your own `lifespan` keeps working
unchanged.

If one limiter serves several applications in the same process, stop each application
from closing the shared clients and close them yourself:

```python
from contextlib import asynccontextmanager

shared_limiter = Limiter.from_url("redis://localhost:6379/0")


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await shared_limiter.aclose()


public_api = FastAPI(lifespan=lifespan)
setup(public_api, shared_limiter, close_on_shutdown=False)
admin_api = FastAPI()
setup(admin_api, shared_limiter, close_on_shutdown=False)
```

## Bringing your own clients

If you already manage redis-py clients (Sentinel, custom TLS, shared pools), pass
them in. UniLimit uses them as they are and **never closes them**: their lifecycle
stays yours. Configure them for a rate limiter: short timeouts and no long retry
loops.

```python
import redis
import redis.asyncio

from unilimit.backends.redis import RedisBackend

sync_client = redis.Redis.from_url("redis://localhost:6379/0", socket_timeout=0.5)
async_client = redis.asyncio.Redis.from_url("redis://localhost:6379/0", socket_timeout=0.5)

own_clients = Limiter(RedisBackend(client=sync_client, async_client=async_client))
```

## When Redis is unavailable

| `fail_mode` | Behaviour |
|---|---|
| `"open"` (default) | The request is admitted. No rate-limit headers are sent, because no real numbers are known. |
| `"closed"` | The request is rejected with 503 `{"detail": "Service Unavailable"}`. |

Set it on the limiter, and override it per rule. A common setup is a fail-open site with
fail-closed login and password-reset rules. A wrong Redis password or a corrupt key is
an **error**, never a silent fail-open. Bursts larger than `max_connections` wait for a
connection instead of failing.

## Multiple workers

Each worker process has its own clients and pools, and all of them share the counters
in Redis. Limits are exact across workers: there is no per-worker share of the quota.
UniLimit's test suite checks this with several uvicorn workers serving concurrent
requests.
