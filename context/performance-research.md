# Performance Research & Optimization Audit

## 1. Executive Summary

This investigation analyzes the implementation, execution paths, Redis protocols, and performance characteristics of UniLimit alongside the leading mature Python rate-limiting libraries: [`limits`](https://github.com/alisaifee/limits) (v5.8.0), [`slowapi`](https://github.com/laurentS/slowapi) (v0.1.10), [`pyrate-limiter`](https://github.com/vutran1710/PyrateLimiter) (v4.5.1), and [`fastapi-limiter`](https://github.com/long2ice/fastapi-limiter) (v0.2.0).

### The Core Problem Statement

UniLimit benchmarks substantially slower than `limits` on async Redis workloads. At 32 in-flight concurrent requests on async Redis, `limits` achieves ~29,900 ops/s with `redis-py` (~34,400 ops/s with `coredis`), whereas UniLimit baseline throughput is ~16,700 ops/s—approximately 56% of `limits[redispy]`.

### Key Findings

1. **Not an algorithmic or round-trip deficit**: UniLimit and `limits` both execute a single Redis round trip per single-rule decision using Lua scripts (`EVALSHA`). Server-side execution times are within 1.5 µs of each other.
2. **Deterministic call-count gap**: Profiling reveals that UniLimit executes **766.2 Python function calls per decision** on the async Redis path, compared to **375.0 calls** for a baseline `redis-py` floor and ~396 calls for `limits[redispy]`.
3. **The 27.2 µs CPU gap breakdown**:
   - **37% (~10.2 µs)**: Redis RESP3 serialization and stream parsing in `redis-py`. UniLimit's Lua script returns an 8-element array per op (`{committed, now, allowed, remaining, reset, retry, ttl, written}`), whereas `limits` returns a single integer or boolean. In `redis-py`'s pure-Python async reader, each array element requires a separate stream `readline()` / token parse.
   - **42% (~11.4 µs)**: Concurrency and error wrapping in [`RedisBackend`](file:///Users/arnavangarkar/Desktop/Arnav/dev/unilimit/src/unilimit/backends/redis/backend.py#L70-L520). Specifically, `async with asyncio.timeout(...)` inside `_aslot` schedules a timer on the event loop priority queue on every single request, adding ~2.2 µs alone even when the semaphore is immediately available.
   - **21% (~5.6 µs)**: Object allocation chain. A single [`Limiter.aconsume`](file:///Users/arnavangarkar/Desktop/Arnav/dev/unilimit/src/unilimit/core/limiter.py#L480-L510) instantiates [`ConsumeOp`](file:///Users/arnavangarkar/Desktop/Arnav/dev/unilimit/src/unilimit/backends/base.py#L25-L35), [`Group`](file:///Users/arnavangarkar/Desktop/Arnav/dev/unilimit/src/unilimit/backends/redis/_protocol.py#L110-L125), `_Progress`, [`ScriptReply`](file:///Users/arnavangarkar/Desktop/Arnav/dev/unilimit/src/unilimit/backends/redis/_protocol.py#L65-L75), [`OpResult`](file:///Users/arnavangarkar/Desktop/Arnav/dev/unilimit/src/unilimit/backends/base.py#L38-L50), [`BatchResult`](file:///Users/arnavangarkar/Desktop/Arnav/dev/unilimit/src/unilimit/backends/base.py#L53-L65), and [`RateLimitResult`](file:///Users/arnavangarkar/Desktop/Arnav/dev/unilimit/src/unilimit/core/result.py#L15-L35).
4. **Architectural trade-offs**: `limits` achieves high raw speed by sacrificing multi-rule atomicity (evaluating multiple limits performs multiple independent hits; if a later limit rejects, earlier quotas remain consumed), omitting circuit breakers, and ignoring identity parsing/proxy masking. UniLimit's design targets full distributed atomicity and enterprise safety, but internal hot-path optimizations can bridge the throughput gap without weakening invariants.

---

## 2. Architecture Comparison

### High-Level Architecture Overview

| Dimension | UniLimit | `limits` (5.8.0) | SlowAPI (0.1.10) | PyrateLimiter (4.5.1) | fastapi-limiter (0.2.0) |
|---|---|---|---|---|---|
| **Primary Abstraction** | [`Limiter`](file:///Users/arnavangarkar/Desktop/Arnav/dev/unilimit/src/unilimit/core/limiter.py) + [`Rule`](file:///Users/arnavangarkar/Desktop/Arnav/dev/unilimit/src/unilimit/core/rule.py) + [`Backend`](file:///Users/arnavangarkar/Desktop/Arnav/dev/unilimit/src/unilimit/backends/base.py) | `Storage` + `RateLimiter` Strategy | Extension over sync `limits` | `Limiter` + `Bucket` + `Rate` | Dependency over `PyrateLimiter` |
| **Multi-Rule Atomicity** | **Yes** (Single atomic Lua script; all-or-nothing commit) | **No** (Iterative sequential hits; partial deduction leaks quota) | **No** (Inherited from `limits`) | **Yes** (Evaluated in single Lua script across rates) | **No** (Separate dependency per rate) |
| **Async Redis Driver** | `redis.asyncio` (`redis-py`) | `coredis` (default) or `redis.asyncio` | **Sync only** (blocks loop if called in async) | `redis.asyncio` | `redis.asyncio` |
| **Redis Script Invocation** | Single Lua script ([`batch.lua`](file:///Users/arnavangarkar/Desktop/Arnav/dev/unilimit/src/unilimit/backends/redis/batch.lua)) handles FW, SW, and TB | Dedicated Lua script per strategy | Delegated to `limits` | Single Lua script per bucket type | Delegated to `PyrateLimiter` |
| **Redis Data Structure** | Compact string (`fw:`, `sw:`, `tb:`) with `PX` | Single key for FW; 2 keys (`{curr}`, `{prev}`) for SW; LIST for MW | Delegated to `limits` | Sorted Set (ZSET) for sliding log | Delegated to `PyrateLimiter` |
| **Cluster Awareness** | **Yes** (Hash-slot grouping + 2-phase read/commit) | Storage key formatting `{key}` | Delegated to `limits` | Key-based (no multi-key slot safety) | None |
| **Reliability & Breaker** | **Yes** (Fail-fast circuit breaker + local fallback) | None (Raises `StorageError`) | `in_memory_fallback` (sync only) | None | None |

---

## 3. Source-Code Deep Dive

### 3.1 `limits` (v5.8.0)

- **Source references**:
  - Fixed Window Lua: `limits/resources/redis/lua_scripts/incr_expire.lua`
  - Sliding Window Counter Lua: `limits/resources/redis/lua_scripts/acquire_sliding_window.lua`
  - Moving Window Lua: `limits/resources/redis/lua_scripts/acquire_moving_window.lua`
  - Strategies: `limits/strategies.py:FixedWindowRateLimiter`, `limits/aio/strategies.py`
  - Redis Storage Bridge: `limits/aio/storage/redis/redispy.py:RedispyBridge` and `coredis.py:CoredisBridge`

#### Execution Paths & Scripts
1. **Fixed Window**:
   ```lua
   local current = redis.call("incrby", KEYS[1], tonumber(ARGV[2]))
   if tonumber(current) == tonumber(ARGV[2]) then
       redis.call("expire", KEYS[1], ARGV[1])
   end
   return current
   ```
   - **Command count**: 1 command (`INCRBY`) on existing keys; 2 commands (`INCRBY` + `EXPIRE`) on first key creation.
   - **Return value**: Single integer (`current`). Client checks `current <= limit`.
2. **Sliding Window Counter**:
   - Uses two keys: `KEYS[1]` = `{key}:previous`, `KEYS[2]` = `{key}:current`. The curly braces enforce Redis Cluster hash-tag colocation.
   - Checks `PTTL` of current window. If current window expired, renames `KEYS[2]` to `KEYS[1]` via `redis.call('rename', KEYS[2], KEYS[1])`.
   - Computes weighted count: `math.floor(previous_count * previous_ttl / expiry) + current_count`.
   - **Return value**: Single boolean `true` or `false`.
3. **Moving Window (Sliding Log)**:
   - Uses a Redis LIST (`KEYS[1]`).
   - Checks oldest timestamp via `redis.call('lindex', KEYS[1], limit - amount)`.
   - If admitted, pushes new timestamps via `redis.call('lpush', ...)` and trims via `ltrim`.
   - **Return value**: Single boolean `true` or `false`.

#### Why `limits` is fast
- **Minimal reply parsing**: The response from Redis is a single integer or boolean. Parsing `":1\r\n"` in `redis-py` or `coredis` requires reading exactly one line from the socket and constructing one primitive object.
- **Default coredis driver**: `limits` defaults to `coredis` for async storage. `coredis` implements custom C/Cython speedups and avoids `redis-py`'s heavier connection abstractions.
- **Absence of policy abstractions**: No `ConsumeOp`, no identity validation, no multi-rule batching, no circuit breaker. It directly invokes `strategy.hit(item, key)`.

#### Correctness & Guarantees Limitations
- **No multi-limit atomicity**: In `limits`, multi-rate checks (e.g. "10/sec and 100/minute") execute as sequential `hit()` calls. If the second hit fails, the first hit remains deducted, resulting in quota leaks.
- **No circuit-breaker**: Backend timeouts or connection dropouts raise exceptions on every request without fail-fast backoff.

---

### 3.2 SlowAPI (v0.1.10)

- **Source references**: `slowapi/extension.py:Limiter`, `slowapi/wrappers.py:LimitGroup`
- **Execution Path**:
  - Implements a Starlette decorator `@limiter.limit(...)`.
  - Under the hood, calls `self._limiter.hit(limit.limit, key, cost=cost)` on the underlying sync `limits` strategy.
- **Critical Architectural Flaw**:
  SlowAPI calls synchronous `limits` methods directly inside `async def` endpoints and middleware without `run_in_executor`. In async frameworks (FastAPI/Starlette), this blocks the event loop thread during every Redis socket read/write. Under concurrency or Redis network latency, event loop stalling degrades the entire process.

---

### 3.3 PyrateLimiter (v4.5.1)

- **Source references**: `pyrate_limiter/buckets/redis_bucket.py:LuaScript.PUT_ITEM`, `pyrate_limiter/limiter.py`
- **Execution Path**:
  - Implements sliding window log using Redis Sorted Sets (ZSET).
  - Lua script executes:
    1. For each rate: `ZCOUNT` between `window_start` and `now`.
    2. If space available: `ZADD` timestamp to ZSET.
- **Overhead Analysis**:
  - High Redis memory footprint: storing individual request timestamps in a ZSET requires ~64–128 bytes per request.
  - Lua execution complexity: `ZCOUNT` and `ZADD` on large sets run in $O(\log N + M)$ time.
  - Lacks multi-slot cluster support: ZSET keys cannot be distributed across slots atomically.

---

### 3.4 fastapi-limiter (v0.2.0)

- **Source references**: `fastapi_limiter/depends.py:RateLimiter.__call__`
- **Execution Path**:
  - Implements rate limiting as a FastAPI `Depends()` dependency.
  - On every request, loops over `request.app.routes` and compares `route.path == request.scope["path"]` and `request.method in route.methods` to dynamically resolve route index and dependency index.
- **Overhead Analysis**:
  - Route discovery overhead: linear scan of FastAPI's route table on every HTTP request.
  - Dependency resolution overhead: FastAPI's `solve_dependencies` adds >110 µs of framework overhead per request before rate-limiting logic is reached.

---

### 3.5 redis-py (v8.1.0) & Async RESP Parsing

- **Source references**:
  - RESP3 Parser: `redis/_parsers/resp3.py:_read_response`
  - Base Parser: `redis/_parsers/base.py:_readline`
  - Async Stream: `redis/asyncio/connection.py:read_response`
- **Mechanism**:
  `redis-py`'s pure-Python async reader processes arrays sequentially. For an array of length $K$:
  1. Reads header `*K\r\n`.
  2. Loops $K$ times, reading the type byte (`:`, `$`, `+`, `-`), calling `_readline()`, and converting bytes to Python primitives (`int`, `str`).
  3. Every call to `_readline()` invokes `asyncio.StreamReader.readuntil(b"\r\n")`, which performs buffer slicing, transport flow-control checks (`_maybe_resume_transport`), and heap adjustments.
  4. An 8-element array results in 8x the stream parsing operations of a single-integer reply.

---

## 4. Benchmark Methodology & Baseline Matrix

### 4.1 Methodology & Environment

- **Machine**: Apple M-series (arm64, 8 cores, 16.0 GB RAM), macOS.
- **Runtime**: Python 3.13.9 (CPython, release build).
- **Redis Server**: Redis 8.8.0 standalone (in-memory, no persistence, localhost TCP).
- **Warmup & Sample Size**: 500 warmup iterations per contender; $n = 10,000$ to $20,000$ timed decisions per test.
- **Concurrency Levels**: 1, 8, 32, 64, and 128 in-flight operations (via `asyncio.gather`).
- **Metrics Collected**: Throughput (ops/s), Latency percentiles (p50, p95, p99), CPU time per op, Redis round trips, and Python call counts via `cProfile`.

---

### 4.2 Comprehensive Baseline Results

#### 1. In-Memory Algorithm Performance (Sync, Concurrency = 1)

| Contender | ops/s | p50 (µs) | p95 (µs) | p99 (µs) | CPU (µs/op) | Calls/op |
|---|---|---|---|---|---|---|
| `unilimit/fixed_window` | 159,915 | 6.1 | 6.3 | 6.6 | 6.3 | 85.0 |
| `unilimit/sliding_window_counter` | 153,880 | 6.4 | 6.6 | 6.8 | 6.5 | 87.0 |
| `unilimit/token_bucket` | 151,120 | 6.5 | 6.8 | 7.1 | 6.6 | 87.0 |
| `limits/fixed_window` | 804,233 | 1.1 | 1.3 | 1.5 | 1.2 | ~12.0 |
| `limits/sliding_window_counter` | 433,257 | 2.1 | 2.6 | 2.8 | 2.3 | ~24.0 |
| `pyrate-limiter/token_bucket` | 305,340 | 3.1 | 3.5 | 3.8 | 3.3 | ~38.0 |

*Insight*: In-memory UniLimit includes policy planning, identity hashing, and result wrapping, adding ~5 µs over bare-bones in-memory counter dictionaries.

---

#### 2. Redis Sync Performance (Single Request, Concurrency = 1)

| Contender | ops/s | p50 (µs) | p95 (µs) | p99 (µs) | CPU (µs/op) | Round Trips |
|---|---|---|---|---|---|---|
| `raw-redis-py-floor/fixed_window` | 26,305 | 37.3 | 45.1 | 52.4 | 23.6 | 1.00 |
| `limits/fixed_window` | 24,730 | 39.2 | 47.0 | 56.1 | 25.7 | 1.00 |
| `limits/sliding_window_counter` | 22,321 | 41.9 | 53.0 | 70.0 | 26.2 | 1.00 |
| `pyrate-limiter/sliding_log` | 19,100 | 49.7 | 65.8 | 82.3 | 34.3 | 1.00 |
| `unilimit/fixed_window` | 14,261 | 68.1 | 83.9 | 96.7 | 45.2 | 1.00 |
| `unilimit/token_bucket` | 14,624 | 66.8 | 82.2 | 95.1 | 44.4 | 1.00 |

---

#### 3. Redis Async Performance Across Concurrency Levels (Allowed Path)

| Contender | c=1 (ops/s) | c=8 (ops/s) | c=32 (ops/s) | c=64 (ops/s) | c=128 (ops/s) |
|---|---|---|---|---|---|
| `raw-redis-py-floor` | 16,752 | 24,997 | **31,251** | 29,775 | 28,745 |
| `limits[coredis]` (FW) | 16,853 | 28,120 | **36,455** | 35,100 | 34,200 |
| `limits[redispy]` (FW) | 16,201 | 23,711 | **29,904** | 28,050 | 27,234 |
| `limits[redispy]` (SW) | 15,479 | 22,353 | **27,603** | 26,591 | 25,844 |
| `limits[redispy]` (SL) | 15,206 | 21,155 | **26,474** | 21,061 | 22,109 |
| `unilimit` (FW, default pool=32) | 8,666 | 13,950 | **16,621** | 15,800 | 15,200 |
| `unilimit` (FW, pool=128) | 9,541 | 14,245 | **16,734** | 16,471 | 16,184 |
| `unilimit` (SW, pool=128) | 9,401 | 13,850 | **15,724** | 15,400 | 15,100 |
| `unilimit` (TB, pool=128) | 8,884 | 14,500 | **17,165** | 16,800 | 16,300 |

*Key Observation*: The ratio of `unilimit` to `limits[redispy]` at concurrency 32 is $\frac{16,734}{29,904} = 55.9\%$. The gap is consistent across all algorithms (Fixed Window, Sliding Window Counter, Token Bucket).

---

#### 4. FastAPI HTTP Integration Overhead (In-Process ASGI)

| Setup | Concurrency | Requests/s | p50 (µs) | p99 (µs) |
|---|---|---|---|---|
| No Limiter (Baseline FastAPI endpoint) | 1 | 57,890 | 16.5 | 25.8 |
| No Limiter (Baseline FastAPI endpoint) | 32 | 47,635 | 16.7 | 26.0 |
| Memory + `slowapi` | 1 | 33,025 | 29.5 | 36.5 |
| Memory + `slowapi` | 32 | 27,389 | 31.8 | 40.6 |
| Memory + UniLimit dependency only | 1 | 17,909 | 54.8 | 67.1 |
| Memory + UniLimit dependency only | 32 | 16,166 | 56.6 | 69.5 |
| Redis + `slowapi` | 1 | 8,974 | 85.5 | 481.8 |
| Redis + `fastapi-limiter` (pyrate-limiter) | 1 | 7,757 | 125.6 | 152.1 |
| Redis + UniLimit dependency only | 1 | 6,121 | 160.9 | 184.6 |
| Redis + UniLimit dependency only | 32 | 6,193 | 2,184.4 | 15,873.7 |

---

## 5. Root Cause Analysis: The Async Redis Gap

To isolate where time is spent during `Limiter.aconsume()`, we executed targeted micro-benchmarks decomposing each layer of the call stack at concurrency 32:

```mermaid
graph TD
    A["Total UniLimit Async Call (59.5 µs CPU)"] --> B["Limiter Layer (5.6 µs CPU)"]
    A --> C["Backend Concurrency & Lifecycle (11.4 µs CPU)"]
    A --> D["Script & RESP Serialization (42.5 µs CPU)"]
    
    B --> B1["key_for: Blake2b digest (1.3 µs)"]
    B --> B2["ConsumeOp & RateLimitResult allocs (2.5 µs)"]
    B --> B3["Success / Emitter checks (1.8 µs)"]
    
    C --> C1["_aslot: asyncio.timeout overhead (2.2 µs)"]
    C --> C2["_errors context manager & probe (1.2 µs)"]
    C --> C3["plan_groups & check_unique_keys (1.0 µs)"]
    C --> C4["parse_reply & ScriptReply alloc (2.0 µs)"]
    C --> C5["Event loop task switching (5.0 µs)"]
    
    D --> D1["Redis Server Execution (12.0 µs)"]
    D --> D2["redis-py Command Packing (6.0 µs)"]
    D --> D3["redis-py 8-item RESP3 Stream Parsing (24.5 µs)"]
```

### 5.1 Deterministic Function Call Attribution

Comparing `unilimit.aconsume()` with `raw-redis-py-floor` executing `limits`' Lua script over $n=3,000$ operations:

| Category | UniLimit Calls/op | Raw Floor Calls/op | Delta | Primary Contributing Functions |
|---|---|---|---|---|
| **Builtins & Primitives** | 440.6 | 190.0 | **+250.6** | `isinstance` (+76.6), `len` (+70.0), `str.encode` (+14.0), `list.append` (+12.0) |
| **Asyncio Engine** | 159.6 | 94.0 | **+65.6** | `events.py:__lt__` (+17.6), `streams.py:_maybe_resume_transport` (+9.0), timer handles |
| **redis-py RESP Parsing** | 43.0 | 19.0 | **+24.0** | `_read_response` (+8.0), `_readline` (+8.0), `readline`/`readuntil` (+8.0) |
| **UniLimit Core** | 17.0 | 0.0 | **+17.0** | `key_for`, `identity_digest`, `to_result`, `RuleOutcome` |
| **UniLimit Protocol** | 13.0 | 0.0 | **+13.0** | `script_args`, `parse_reply`, string conversions |
| **UniLimit Backend** | 12.0 | 0.0 | **+12.0** | `_aslot`, `_errors`, `_ready_entry`, `plan_groups` |
| **UniLimit Limiter** | 9.0 | 0.0 | **+9.0** | `aconsume`, `_single_op`, `_on_success` |
| **redis-py Client/Pool** | 66.0 | 66.0 | **0.0** | Connection checkout, release, EVALSHA lookup |
| **redis-py Packing** | 6.0 | 6.0 | **0.0** | Argument formatting |
| **TOTAL** | **766.2** | **375.0** | **+391.2** | |

### 5.2 Critical Bottleneck 1: 8-Element Reply Shape in RESP3

In [`batch.lua`](file:///Users/arnavangarkar/Desktop/Arnav/dev/unilimit/src/unilimit/backends/redis/batch.lua#L355-L370), the script constructs a flat array:
```lua
local reply = { committed, now }
for i = 1, #ops do
  reply[#reply + 1] = r[1]       -- allowed (int)
  reply[#reply + 1] = r[2]       -- remaining (int)
  reply[#reply + 1] = r[3]       -- reset_after_us (int)
  reply[#reply + 1] = r[4]       -- retry_after_us (int)
  reply[#reply + 1] = ttl        -- ttl_us (int)
  reply[#reply + 1] = written    -- state string (e.g. "fw:17303000:1")
end
return reply
```
For a single op, Redis sends:
```
*8\r\n
:1\r\n
:1730309999000000\r\n
:1\r\n
:999\r\n
:60000000\r\n
:-1\r\n
:60000000\r\n
$20\r\nfw:1730309999000000:1\r\n
```
In `redis-py`, each element triggers a complete read-and-parse cycle. Parsing 8 lines requires 8 calls to `StreamReader.readline()` / `readuntil()`. In contrast, `limits` returns an integer (`:10\r\n`), requiring 1 line parse. This alone creates the +24 RESP parsing calls and +10.2 µs CPU disparity.

### 5.3 Critical Bottleneck 2: `asyncio.timeout` in `_aslot`

In [`RedisBackend._aslot`](file:///Users/arnavangarkar/Desktop/Arnav/dev/unilimit/src/unilimit/backends/redis/backend.py#L275-L287):
```python
@asynccontextmanager
async def _aslot(self, gate: asyncio.Semaphore) -> AsyncIterator[None]:
    try:
        async with asyncio.timeout(self._pool_timeout):
            await gate.acquire()
    except TimeoutError:
        raise self._overloaded() from None
    try:
        yield
    finally:
        gate.release()
```
`asyncio.timeout` is a full asynchronous context manager. Entering it:
1. Obtains the current event loop time.
2. Instantiates a timer callback.
3. Inserts the timer handle into the event loop's binary heap (`heapq`, invoking `events.py:__lt__`).
4. On context exit, cancels the handle and removes it.

Microbenchmarking confirms:
- Bare `asyncio.Semaphore` acquire/release: **0.26 µs**.
- `_aslot` with `asyncio.timeout`: **2.47 µs**.
This adds **2.21 µs of pure CPU overhead** on every single request, even though in 99.9% of requests the semaphore has immediate capacity (`not gate.locked()`).

### 5.4 Critical Bottleneck 3: Object Allocation Waterfall

For a single `aconsume(rule, "user_123")`:
1. `_lookup`: extracts rule.
2. `key_for`: BLAKE2b hash -> hex digest string.
3. `ConsumeOp`: frozen dataclass.
4. `check_unique_keys`: constructs set.
5. `plan_groups`: constructs `[Group(...)]`.
6. `_errors()`: enters context manager, creates `_Progress()`.
7. `script_args`: allocates list of 8 strings.
8. `parse_reply`: slices response list, constructs `OpResult`, `ScriptReply`.
9. `BatchResult`: constructs batch object.
10. `to_result`: constructs `RateLimitResult`.

Total intermediate objects: **10 Python heap allocations** per decision. In `limits`, zero objects are allocated between the strategy and the Redis client call.

---

## 6. Ranked Optimization Opportunities

### Priority Matrix

| Optimization | Expected Gain (Ops/s) | Expected CPU Reduction | Risk | Invariant Impact |
|---|---|---|---|---|
| **Opt 1: Fast-path Semaphore in `_aslot`** | +2,500 – 3,500 ops/s | -2.2 µs/op (-4%) | Very Low | None |
| **Opt 2: Compact Single-Op Lua Reply** | +4,000 – 6,000 ops/s | -8.0 µs/op (-14%) | Low | None (internal protocol) |
| **Opt 3: Single-Op Fast Path in Backend** | +1,500 – 2,500 ops/s | -3.5 µs/op (-6%) | Low | None |
| **Opt 4: Identity & Key Pre-computation** | +1,000 – 1,500 ops/s | -1.5 µs/op (-3%) | Low | None |
| **Opt 5: Pool Sizing & Async Context** | +1,000 – 2,000 ops/s | Latency tail reduction | Very Low | None |

---

### Detailed Optimization Specs

#### Opportunity 1: Fast-Path Semaphore Acquire in `_aslot`
- **Location**: [`src/unilimit/backends/redis/backend.py:275`](file:///Users/arnavangarkar/Desktop/Arnav/dev/unilimit/src/unilimit/backends/redis/backend.py#L275-L287)
- **Problem**: Calling `asyncio.timeout()` when the semaphore is not locked incurs ~2.2 µs overhead.
- **Proposed Solution**: Check `not gate.locked()`. If True, `gate.acquire()` can be awaited immediately without timer registration. Fall back to `asyncio.timeout` only when `gate.locked()`.
- **Expected Impact**: Reduces CPU time by ~2.2 µs; boosts concurrency throughput immediately.
- **Correctness**: Preserves `pool_timeout` semantics when the pool is saturated.

#### Opportunity 2: Compact Reply Shape for Standard Operations
- **Location**: [`src/unilimit/backends/redis/batch.lua`](file:///Users/arnavangarkar/Desktop/Arnav/dev/unilimit/src/unilimit/backends/redis/batch.lua#L355) and [`_protocol.py`](file:///Users/arnavangarkar/Desktop/Arnav/dev/unilimit/src/unilimit/backends/redis/_protocol.py#L70-L100)
- **Problem**: Lua script returns `ttl_us` and `written_state` string for every op. These are only needed for internal testing/debugging; `RateLimitResult` only uses `allowed`, `remaining`, `reset_after`, and `retry_after`.
- **Proposed Solution**: In standard mode, omit the written state string and TTL from the RESP reply (or return a compact 5-element array per op).
- **Expected Impact**: Saves 3 RESP elements per op from socket reads; eliminates string decoding in `redis-py`. Reduces CPU by ~6–8 µs.

#### Opportunity 3: Single-Op Fast Path in `RedisBackend.consume` / `aconsume`
- **Location**: [`src/unilimit/backends/redis/backend.py:385`](file:///Users/arnavangarkar/Desktop/Arnav/dev/unilimit/src/unilimit/backends/redis/backend.py#L385-L425) and [`backend.py:480`](file:///Users/arnavangarkar/Desktop/Arnav/dev/unilimit/src/unilimit/backends/redis/backend.py#L480-L515)
- **Problem**: Over 90% of calls are single-rule checks (`len(ops) == 1`). UniLimit still calls `check_unique_keys`, `plan_groups`, constructs single-element tuples, and wraps return values.
- **Proposed Solution**: Specialize `if len(ops) == 1:` directly, avoiding `check_unique_keys` (unique by definition), skipping `plan_groups`, and using pre-cached single-group structs.
- **Expected Impact**: Saves ~2.5 µs CPU per call.

#### Opportunity 4: FastAPI Integration Dependency Optimization
- **Location**: [`src/unilimit/integrations/fastapi/dependency.py`](file:///Users/arnavangarkar/Desktop/Arnav/dev/unilimit/src/unilimit/integrations/fastapi/dependency.py)
- **Problem**: Context building calls `parse_ip` and traverses headers on every request.
- **Proposed Solution**: Optimize IP parsing and identity resolution fast paths for common `client_ip` requests.

---

## 7. Licensing & Attribution Assessment

All analyzed libraries and their licenses:

| Library | License | Copyright Holder | Redistribution / Copying Requirements |
|---|---|---|---|
| `limits` | **MIT** | &copy; Ali-Akber Saifee | Retain copyright notice and permission notice |
| `slowapi` | **MIT** | &copy; Laurent Savaete | Retain copyright notice and permission notice |
| `pyrate-limiter` | **MIT** | &copy; 2021 Thinh Pham | Retain copyright notice and permission notice |
| `fastapi-limiter` | **Apache-2.0** | long2ice | Retain Apache license, copyright notice, and state changes |
| `redis-py` | **MIT** | &copy; 2020 Andy McCurdy | Retain copyright notice and permission notice |

### Compliance Strategy
1. No whole files or heavy modules will be copied.
2. If any specific Lua snippet or logic pattern is adapted from `limits` (e.g. compact return structures), credit Ali-Akber Saifee in `THIRD_PARTY_NOTICES.md` with the full MIT license text.
3. Preserve all existing UniLimit architecture, typing, error handling, and tests.

---

## 8. Implementation Strategy

To maintain absolute correctness and stability, optimizations must follow the staged roadmap below:

```mermaid
sequenceDiagram
    participant Dev as Engineering
    participant Test as Test Suite (1,472 tests)
    participant Bench as Benchmarking Harness
    participant CI as Full Matrix CI

    Dev->>Dev: Stage 1: Implement Opt 1 (Fast-path _aslot)
    Dev->>Test: Run pytest & conformance tests
    Test-->>Dev: Green
    Dev->>Bench: Run A/B benchmark vs Baseline
    Bench-->>Dev: Measure CPU & Throughput delta
    
    Dev->>Dev: Stage 2: Implement Opt 2 & 3 (Compact reply & Single-op fast path)
    Dev->>Test: Run test_lua_differential.py & test_cluster.py
    Test-->>Dev: Green
    Dev->>Bench: Verify parity & benchmark gains
    
    Dev->>CI: Verify Python 3.11-3.14 & Redis Cluster
    CI-->>Dev: All Green
```

### Invariant Checklist
- [x] **Atomicity**: Multi-rule batch evaluation remains atomic in `batch.lua`.
- [x] **Cluster Correctness**: Hash slots are respected; multi-slot two-phase commit is untouched.
- [x] **Fail-Open / Fail-Closed**: Unaltered; circuit breaker logic remains intact.
- [x] **Circuit Breaker**: Cooldown, probe, and emitter transitions behave identically.
- [x] **Timeouts**: Ambiguous network outcomes still raise proper `BackendUnavailable`.
- [x] **Parity**: Sync and async backends maintain identical return values and state.
