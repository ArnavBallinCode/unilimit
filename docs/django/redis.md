# Redis configuration and lifecycle

## From settings (recommended)

```python
# settings.py
UNILIMIT = {
    "URL": "redis://localhost:6379/0",  # rediss:// for TLS, unix://, or sentinel://...
    # Passed to Limiter.from_url:
    "OPTIONS": {
        "namespace": "shop",  # key prefix: isolates applications sharing a Redis
        "socket_timeout": 0.5,
        "socket_connect_timeout": 1.0,
        "max_connections": 32,  # in-flight Redis calls per process
        "pool_timeout": 1.0,  # extra calls wait this long before counting as unavailable
        "cooldown": 0.5,  # after a Redis failure, fail fast, then probe once
        "fail_mode": "open",
    },
}
```

Use `"LIMITER": limiter` (or a dotted path such as `"myproject.ratelimits.limiter"`)
instead of `URL` when you build the `Limiter` yourself, for instance to register rules,
set `trusted_proxies` or use `cluster=True`. Exactly one of `LIMITER` and `URL` is
required.

- **Redis Cluster:** pass `"cluster": True` in `OPTIONS`.
- **Redis Sentinel:** use a `sentinel://host1:26379,host2:26379/service_name/0` URL, or
  instantiate `RedisBackend.from_sentinel(...)` and pass `Limiter(backend)`.
- **TLS / SSL:** use `rediss://...` or supply `ssl_cert_reqs`, `ssl_ca_certs`, etc. in
  `OPTIONS["client_options"]`.

Everything is loaded on first use and cached; `override_settings(UNILIMIT=...)` in tests
rebuilds it.

## Clients and event loops

Clients are created lazily, on first use.

- **ASGI:** the async client is created once per event loop and reused by every
  request. Neither the middleware nor `@ratelimit` makes a blocking Redis call on the
  event loop.
- **WSGI:** the sync client is shared by all threads.
- **Async views under WSGI:** Django runs them in a short-lived event loop per request.
  Async Redis clients are bound to their loop, so UniLimit uses the sync client in a
  worker thread there instead of opening a connection per request.

## Shutdown

WSGI servers have no shutdown hook for applications, so the clients are released when the
process exits. Django's ASGI handler does not implement the lifespan protocol either; wrap
your application so the Redis clients close when the server stops:

```python
# asgi.py
from django.core.asgi import get_asgi_application

from unilimit.integrations.django.asgi import with_lifespan

application = with_lifespan(get_asgi_application())
```

If you create the `Limiter` yourself and share it, pass it explicitly:
`with_lifespan(app, limiter=limiter)`.

Clients you pass in yourself (`RedisBackend(client=..., async_client=...)`) are never
closed by UniLimit.

## When Redis is unavailable

| `fail_mode` | Behaviour |
|---|---|
| `"open"` (default) | The request is admitted. No rate-limit headers are sent. |
| `"closed"` | The request is rejected with 503. |

Set it on the limiter, and override it per rule. A wrong Redis password or a corrupt key
is an **error**, never a silent fail-open.

## Multiple workers and application instances

Every worker process has its own clients; all share the counters in Redis, so limits are
exact across workers. UniLimit's test suite checks this with several uvicorn and
gunicorn workers on standalone Redis and on a Redis Cluster.

```python
from unilimit import Limiter

limiter = Limiter.from_url("redis://localhost:6379/0", namespace="shop")
UNILIMIT = {"LIMITER": limiter}
```
