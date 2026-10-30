# Lessons learned

Pitfalls found while building UniLimit, each with its root cause and the fix. Read this
before writing Redis, async or test-infrastructure code. Add to it whenever something
surprises you.

## Redis and redis-py

- **redis-py 8 retries 10 times by default** on connection errors and timeouts. For a
  rate limiter that multiplies outage latency, and retrying a script whose reply was
  lost can double-consume. → Clients built from URLs use `Retry(NoBackoff(), 1,
  supported_errors=(ConnectionError,))`.
- **`AuthenticationError` is a subclass of `ConnectionError`.** Classifying by
  `ConnectionError` alone made a wrong password *fail open*. → Auth and permission errors
  are checked first and always raise.
- **The default pool raises `MaxConnectionsError` past 100 connections** instead of
  waiting. A 400-request burst failed open *and* tripped the outage breaker, turning
  limiting off. → UniLimit's own concurrency gate, plus "pool exhaustion is not an
  outage".
- **The async cluster client used cold by many tasks** raced its own topology discovery
  (~15% of a burst failed). → Initialise once under a lock.
- **`RedisCluster.close()` leaks one socket per node**: node `Redis` clients wrap
  supplied pools, which `close()` does not disconnect. → Disconnect the node pools first
  (`RedisBackend.close`, `tests/_servers.close_client`).
- **Redis 6.2 wraps errors raised inside scripts:** `Error running script … -OOM command
  not allowed`. → `_errors` also looks for `: -CODE ` inside messages.
- **Lua's `tostring` uses `%.14g`** (lossy for µs timestamps), and **numbers returned
  from scripts are truncated to integers.** → Format every number with `%.17g`; return
  only integral numbers or strings.
- **`PX` is counted in milliseconds from the script's start**, which can be before
  `TIME`. → `ceil(ttl/1000) + 1`; verified with `PEXPIRETIME`.
- **A `0` "use server time" sentinel** collided with a legitimate clock value of 0. →
  The empty string means server time.
- **Unsynced replicas are never promoted.** With `repl-diskless-sync-delay` (default
  5 s) a freshly created test cluster had no failover-capable replicas. → Test clusters
  use delay 0 and wait for `master_link_status:up`.
- **Cluster nodes learn new topology asynchronously:** the client may already be
  redirected while the node you query still reports the old primary. → Poll in tests.
- **Redis 8's top-level `make` builds bundled modules** that need Rust and other
  toolchains. → Build from `src/`.

## Numerics

- Python `int * int / int` is exact-then-rounded; Lua computes in doubles. → Python
  reference code uses explicit `float(...)` operands in the same order as the Lua.
- An operation-order change (`p*(W-e)/W` vs `p*((W-e)/W)`) differs by 1 ulp and is
  invisible to random fuzzing; it only flips results within EPS of an integer. →
  Constructed EPS-boundary test family.
- Unbounded configurations (huge burst, slow rate) produced reset times beyond int64,
  which Redis saturated. → `LimitSpec` bounds the full refill to 10 years.
- The token bucket refilled to `2.9999999999999996`, not `3.0`, after exactly its reset
  time. → Snap to capacity within EPS.

## Async and concurrency

- `redis.asyncio` clients are bound to the event loop that created them. → One client
  per loop; discard clients of closed loops.
- The GIL hides races in short critical sections. → Concurrency tests set
  `sys.setswitchinterval(1e-6)`; removing the lock must make them fail.
- Under CPU starvation (8 processes plus Redis on one laptop), tight timeouts cause real
  fail-open. → Exactness tests use generous timeouts; breaker and timeout behaviour have
  their own tests.

## FastAPI and Starlette

- Headers set on FastAPI's injected `Response` are lost when an endpoint returns its own
  `Response`. → Add headers in pure ASGI middleware at `http.response.start`.
- Dependency order is app → router → route `dependencies=` → endpoint parameters, and a
  sub-dependency shared with the endpoint is called once per request (verified on
  FastAPI 0.115 and 0.162).
- Starlette's `SimpleUser` does not implement `.identity`. → Fall back to `display_name`.
- `starlette.testclient` is deprecated with httpx and warns (an error under our
  settings). → Use `httpx.AsyncClient(transport=ASGITransport(app))`, and a small ASGI
  driver for WebSockets.

## Test infrastructure

- **Resource leaks surface at random GC times** (CI failed at session teardown while
  local runs passed). → GC after every Redis or FastAPI test, so the leaking test fails.
- **A crashed worker before a `multiprocessing.Barrier` hangs the suite**, and
  non-daemon children block exit. → Barrier timeouts, daemon processes, errors reported
  through the queue, `terminate()` in `finally`.
- **`tests/redis/` shadowed the `redis` package** for Ruff's import sorting. → Never
  name test directories after third-party packages.
- **Ruff formats Python code blocks inside Markdown.** Expect doc diffs after
  `ruff format`.
- **Scripted search-and-replace edits break silently after reformatting.** Assert the
  pattern exists before replacing; prefer real edits.
- **`git checkout -- file` restores the index, not your working copy.** Commit or back
  up before mutation experiments.
- **A property test can pass without the fix it is meant to guard** (a leak test passed
  because GC closed the sockets). → Always run a regression test against the unfixed
  code once.
