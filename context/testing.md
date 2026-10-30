# Testing

## Suites

| Directory | What | Needs Redis |
|---|---|---|
| `tests/unit/` | Pure logic: rates, rules, identity and proxies, keys and slots, policy, limiter (memory), HTTP headers, error classification | No |
| `tests/unit/algorithms/` | Example tests plus Hypothesis invariants for each algorithm | No |
| `tests/conformance/` | The backend contract on memory, Redis and Cluster, sync and async (`drivers.py`) | Yes |
| `tests/concurrency/` | Threads and asyncio (memory); 8 spawned OS processes (Redis and Cluster) | Partly |
| `tests/redis_backend/` | Lua vs Python differential, reliability (faults, breaker, OOM, auth, expiry, cancellation), real cluster and failover | Yes |
| `tests/fastapi_integration/` | Real FastAPI apps over HTTPX/ASGI, WebSockets via an ASGI driver, lifecycle, uvicorn workers over TCP, executable docs | Yes |
| `tests/unit/test_observability.py`, `test_exporters.py` | Events, observers, schema, privacy, metrics, throttled logging, fallback (fault-injecting backend), Prometheus and OpenTelemetry | No |
| `tests/redis_backend/test_observability.py`, `test_observability_docs.py` | Breaker transitions, outage and recovery, fallback, Cluster primary crash on real servers; executable observability docs | Yes |
| `tests/django_integration/` | Real Django and DRF request cycles: WSGI (`django.test.Client`) and ASGI (`ASGIHandler` over HTTPX), sessions and DRF authentication, outage and recovery, concurrency, uvicorn and gunicorn workers over TCP, executable docs | Yes |

## Infrastructure (`tests/_servers.py`, `tests/_proxy.py`, `tests/conftest.py`)

- `RedisProcess` starts `redis-server` (or `$UNILIMIT_REDIS_SERVER`) on a free port in a
  temp directory. It offers `stop()` (SIGKILL), `start()` and `restart()`.
- `RedisCluster` creates 3 primaries and 3 replicas with `redis-cli --cluster create`,
  then waits for `cluster_state:ok` and for replicas to sync.
- The session fixtures `redis_server` and `redis_cluster` start lazily. The function
  fixtures `redis_url` and `cluster_url` flush data before each test.
- `FaultProxy`: a TCP proxy that can `forward`, `blackhole` (for timeouts) or `reset()`
  every connection.
- `close_client()` fully closes redis-py clients, including cluster node pools.
- **Leak check:** an automatic `gc.collect()` after every test in `redis_backend`,
  `conformance`, `concurrency`, `fastapi_integration` and `django_integration`.
- **Hypothesis:** the default profile is derandomised (300 examples).
  `HYPOTHESIS_PROFILE=explore` uses 5,000 random examples. `UNILIMIT_DIFF_EXAMPLES`
  scales the differential tests.

## FastAPI helpers (`tests/fastapi_integration/conftest.py`)

- `make_limiter`: parametrised over memory, Redis and Cluster; closes async clients on
  the test's loop.
- `client(app, peer=…)`: an httpx `AsyncClient` over `ASGITransport`.
- `lifespan(app)`: drives the ASGI lifespan.
- `WebSocketSession`: drives the ASGI WebSocket protocol.
- `ratelimit_headers(response)`.

## Django helpers (`tests/django_integration/conftest.py`)

- Settings: `tests/django_integration/settings.py` (`DJANGO_SETTINGS_MODULE` in
  `pyproject.toml`; `pytest-django`). SQLite lives in a temp file because Django never
  closes in-memory connections and the leak check would report them.
- `make_limiter`: memory, Redis and Cluster; installs the limiter as `UNILIMIT["LIMITER"]`
  and closes its clients on the test's loop. `memory_limiter` for backend-independent tests.
- `wsgi(peer=...)`: Django's test `Client`. `asgi(peer=...)`: the real `ASGIHandler` over
  HTTPX. Use both for anything that must hold in sync and async chains.
- `_fresh_config` resets the cached `UNILIMIT` config around every test, and
  `_close_thread_connections` closes asgiref-thread database connections.
- Worker tests start `uvicorn` and `gunicorn` on `tests/django_integration/_e2e_app.py`.

## Observability helpers

- `FlakyBackend` (`tests/unit/test_observability.py`): a memory backend with `down` and
  `partial` switches that raises `BackendUnavailable`; use it for fault injection without
  Redis. Real outages use a private `RedisProcess` (`stop()` / `start()`).
- `Recorder`: an observer that stores events. `_fresh_log_throttle` (autouse in
  `tests/conftest.py`) resets the process-wide log throttle around every test.
- Benchmarks: `uv run --group bench python -m benchmarks.run --quick` is the smoke test.
  Benchmark numbers are not asserted in tests (they are machine-dependent); the harness is.

## Practices

- **Exact counts, not timings.** Use `ManualClock` (memory) or the `clock=` option of
  `RedisBackend` to freeze time.
- **Mutation-check important tests.** Break the code on purpose, confirm a test fails,
  then restore it. Back up the file first (see `lessons.md`).
- **Executable docs.** Every ```python block in `docs/fastapi/*.md` runs in
  `tests/fastapi_integration/test_docs.py`, and every block in `docs/django/*.md` and
  `docs/drf/*.md` in `tests/django_integration/test_docs.py`, which then checks the
  behaviour the page describes. A new page must be added to `test_every_page_has_examples`
  (`django/versions.md` is text only).
- **Never weaken a test** to make it pass. If a test was wrong, say why in the commit.

## Matrices

```bash
# Server versions (build binaries from source with `make -C <src>/src`)
UNILIMIT_REDIS_SERVER=/path/to/redis-6.2/src/redis-server uv run pytest tests/redis_backend tests/conformance tests/concurrency
# FastAPI / Starlette
uv run --with fastapi==0.115.0 pytest tests/fastapi_integration tests/unit/test_http.py
uv run --with fastapi==0.140.0 --with starlette==0.46.0 pytest tests/fastapi_integration
# Django / DRF
uv run --with django==5.2.* --with djangorestframework==3.16.* pytest tests/django_integration
uv run --with django==6.0.* --with djangorestframework==3.17.* pytest tests/django_integration
# Python versions
UV_PROJECT_ENVIRONMENT=.venv-3.11 uv run --python 3.11 pytest
```

CI runs all of these (`.github/workflows/ci.yml`).
