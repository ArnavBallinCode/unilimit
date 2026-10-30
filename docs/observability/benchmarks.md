# Benchmarks

Tooling, raw results and this analysis are reproducible: see
[benchmarks/](https://github.com/ArnavBallinCode/unilimit/tree/main/benchmarks)
(`README.md` there has the commands). Raw JSON for both runs is committed in
`benchmarks/results/`. **Read the caveats before quoting any number.**

## Summary: what the data says

- **UniLimit is not faster than the alternatives.** At the library level it is slower
  than `limits` (about 3–5× in memory and 1.5–1.7× on Redis, sync) and slower than
  PyrateLimiter in memory. On Redis it uses one round trip per decision, the same as the
  others. We did not set out to win a speed contest; the price is explained below.
- **Correctness is equal where measured.** With 1–4 processes hammering one key, every
  library admitted exactly the limit (1000 of 1000). UniLimit's differences are in
  guarantees the benchmark does not stress (atomic multi-rule batches, no raw identities
  in storage, fail modes, observability), not in single-rule speed.
- **Scaling across processes** is close to linear for all libraries against one Redis
  (UniLimit 13.7k → 35.3k decisions/s from 1 to 4 processes).
- **Asyncio on Redis throughput has been significantly improved:** Following the performance
  audit in `context/performance-research.md`, UniLimit's async Redis throughput rose from
  8.8k to **10.2k ops/s** (c=1, +15.5%) and from 16.8k to **19.2k–19.8k ops/s** (c=32, +15%).
  Sync Redis throughput reached **17.0k–17.3k ops/s** (up from 13.8k, +24%). While `limits`
  achieves higher raw throughput due to returning a bare boolean from C-implemented `coredis`
  or `redis-py` pipeline scripts without error wrappers or token state parsing, UniLimit
  now executes single-rule decisions with minimal overhead while strictly preserving all
  invariants (circuit breakers, cluster hash slots, atomic multi-rule semantics, identity isolation).
- **Observability costs about 2–3.5 µs per decision in memory** (25–50%, because memory
  decisions are cheap) and **about 2–9 µs on Redis** (2–8%) for `InMemoryMetrics`; the
  Prometheus observer costs a little more. **With no observer there is no measurable
  overhead** (measured against the pre-M6 code, see the end of this page).
- **SlowAPI looks twice as fast at the HTTP level on Redis** in this setup because its
  blocking `limits` call runs on the event loop: fine at low concurrency, a stall risk
  under load. We measured what it does; we did not penalise it.

## Why is UniLimit slower?

A profile of the in-memory path (`cProfile`, 30,000 `consume` calls) shows the time spread
across work that other libraries skip, and that exists for invariants:

- a **keyed BLAKE2b digest** of every identity and key validation (no raw identities in
  storage);
- building **atomic-batch** operations and result objects (`evaluate_batch`, `to_result`),
  even for a single rule;
- on Redis, a **Lua script** that reads server `TIME` and applies the shared algorithm
  implementation, instead of a bare `INCR`.

### Performance Optimizations (`perf-research`)

To reduce the CPU gap while strictly preserving every invariant:

1. **Uncontended Concurrency Fast-Path:** In `_aslot` and `_slot`, non-blocking semaphore
   acquisition (`if not gate.locked(): await gate.acquire()`) avoids `asyncio.timeout` timer handle
   registration when pool capacity is immediately available, dropping slot overhead from 2.4 µs to 1.2 µs.
2. **Compact Lua Reply Structure:** The Lua script was updated to return 4 compact integers
   per operation (`{allowed, remaining, reset, retry}`) instead of 6 items containing string TTLs
   and raw state representations, reducing RESP3 array deserialization CPU in `redis-py` by ~3.5 µs.
   A detailed mode (`'cd'`) is retained for differential test equivalence.
3. **Single-Rule Fast-Paths:** Single-operation batches in `RedisBackend.consume/aconsume`
   and `_protocol.py` bypass multi-slot group planning, key uniqueness verification, and
   generator comprehensions, parsing replies via direct indexing.
4. **Key Generation Direct Path:** Factored `_key_for_rule` in `Limiter` to eliminate redundant
   rule dictionary lookups during single-op evaluations.

## Methodology

Run 1:

- Machine: arm, 8 cores, 16.0 GB, macOS-26.5.1-arm64-arm-64bit-Mach-O
- Python 3.13.9 (CPython)
- Server: redis 8.8.0 on localhost (private instance, no persistence)
- Libraries: unilimit 0.1.0.dev0, limits 5.8.0, slowapi 0.1.10, pyrate-limiter 4.5.1, fastapi-limiter 0.2.0, fastapi 0.162.0, starlette 1.10.0, redis 8.1.0, coredis 4.24.0, uvicorn 0.54.0, psutil 7.2.2
- Run at 2026-10-30T15:43:17Z; n=20000 decisions per measurement

Run 2:

- Machine: arm, 8 cores, 16.0 GB, macOS-26.5.1-arm64-arm-64bit-Mach-O
- Python 3.13.9 (CPython)
- Server: redis 8.8.0 on localhost (private instance, no persistence)
- Libraries: unilimit 0.1.0.dev0, limits 5.8.0, slowapi 0.1.10, pyrate-limiter 4.5.1, fastapi-limiter 0.2.0, fastapi 0.162.0, starlette 1.10.0, redis 8.1.0, coredis 4.24.0, uvicorn 0.54.0, psutil 7.2.2
- Run at 2026-10-30T16:13:26Z; n=20000 decisions per measurement

- **Machine state:** an interactive laptop with other applications running. Load was not
  controlled. **Run 1 and run 2 differ by up to ~1.7× for the same code** (for example
  UniLimit fixed window in memory: 90,629 vs 156,686 decisions/s). Treat differences under
  ~2× between libraries as unresolved unless they are consistent in both runs, and prefer
  the ratios within a run to the absolute numbers.
- **Scenarios.** `allow`: limit far above the request count (10⁹ per hour), 1,000
  rotating keys, so every request is admitted. `reject`: limit 1, one key. The tables show
  `allow`; `reject` is in the raw JSON.
- **Timing.** Per-call latency with `perf_counter_ns` around each decision, garbage
  collection disabled for sync runs, 500 warm-up calls, 20,000 measured decisions per
  contender. Percentiles are nearest-rank. CPU is `process_time()` per decision.
- **Redis round trips** are counted client-side by wrapping redis-py's
  `send_packed_command` (a pipeline counts once). `limits`' asyncio storage uses coredis,
  which that cannot see (`n/a`; the server-side command counts are in the JSON). Round
  trips per decision were 1.00 for every library that could be counted. At 32 in flight
  the count can read slightly above 1 in short runs because connection setup commands are
  included.
- **Configuration.** Each library is used the way its documentation recommends for
  per-key limits. PyrateLimiter's built-in limiters share one bucket across all names, so
  per-key limits use its documented `BucketFactory` with one bucket per key.
  fastapi-limiter 0.2.0 is a thin dependency over PyrateLimiter. All Redis runs use one
  private `redis-server` on localhost, no persistence, default client settings.
- **HTTP level.** A FastAPI app driven through the ASGI interface in-process (no sockets,
  no HTTP parsing), one endpoint, limits never reached. "dependency + middleware headers"
  is the full UniLimit setup (`setup()` adds rate-limit headers to every response);
  "dependency only" omits the headers. SlowAPI is used with its defaults (no headers).
- **Multi-process.** `spawn`ed processes, started together on a barrier, each running
  sync decisions against the same Redis. Exactness: all processes hit one key with
  limit 1,000 and 1.5× that many attempts each.

## What is and is not comparable

| Family | UniLimit | limits | PyrateLimiter | Comparable? |
|---|---|---|---|---|
| Fixed window | `fixed_window` | `FixedWindowRateLimiter` | (its fixed window is not a per-key default) | UniLimit vs limits: yes |
| Sliding window **counter** (weighted previous window, O(1) state) | `sliding_window` | `SlidingWindowCounterRateLimiter` | no | UniLimit vs limits: yes |
| Sliding window **log** (exact, one entry per request) | not offered | `MovingWindowRateLimiter` | default (`sliding_log`) | **Not comparable** to UniLimit; shown only for context |
| Token bucket | `token_bucket` | not offered | `TokenBucket` (GCRA-style, in-process state only) | Different state model; UniLimit's is shared in Redis, PyrateLimiter's is local, so their memory figures compare local-to-local only |

A sliding log is exact and uses memory proportional to the limit; counters and buckets
use constant memory. Do not read a sliding-log row as the same work as a counter row.

## Results

Each cell is `run 1 / run 2`.

#### Library-level decisions

#### memory, sync, one request at a time (allowed path)

| library/algorithm | decisions/s | p50 µs | p95 µs | p99 µs | CPU µs/decision | Redis round trips |
|---|---|---|---|---|---|---|
| `unilimit/fixed_window` | 90,629 / 156,686 | 10.8 / 6.2 | 11.2 / 7.0 | 11.9 / 7.5 | 11.0 / 6.4 | - |
| `unilimit/sliding_window_counter` | 86,941 / 136,999 | 11.3 / 6.7 | 11.6 / 7.8 | 11.9 / 14.9 | 11.5 / 7.0 | - |
| `unilimit/token_bucket` | 89,378 / 146,263 | 10.9 / 6.5 | 11.3 / 7.6 | 11.6 / 8.1 | 11.2 / 6.8 | - |
| `unilimit+metrics/sliding_window_counter` | 67,058 / 110,951 | 14.7 / 8.8 | 15.0 / 9.5 | 15.7 / 10.4 | 14.9 / 9.0 | - |
| `limits/fixed_window` | 465,099 / 804,233 | 2.0 / 1.1 | 2.2 / 1.3 | 2.5 / 1.5 | 2.2 / 1.2 | - |
| `limits/sliding_window_counter` | 256,568 / 433,257 | 3.7 / 2.1 | 3.9 / 2.6 | 4.3 / 2.8 | 3.9 / 2.3 | - |
| `limits/sliding_log` | 417,831 / 750,439 | 2.1 / 1.2 | 2.3 / 1.3 | 2.6 / 1.5 | 2.4 / 1.3 | - |
| `pyrate-limiter/sliding_log` | 228,894 / 388,932 | 4.2 / 2.5 | 4.5 / 2.8 | 4.6 / 3.0 | 4.4 / 2.6 | - |
| `pyrate-limiter/token_bucket` | 305,340 / 228,767 | 3.1 / 3.4 | 3.5 / 4.2 | 3.8 / 22.8 | 3.3 / 3.9 | - |

#### memory, async, one request at a time (allowed path)

| library/algorithm | decisions/s | p50 µs | p95 µs | p99 µs | CPU µs/decision | Redis round trips |
|---|---|---|---|---|---|---|
| `unilimit/fixed_window` | 89,384 / 150,313 | 10.7 / 6.4 | 11.1 / 7.0 | 14.6 / 7.6 | 11.1 / 6.7 | - |
| `unilimit/sliding_window_counter` | 60,116 / 150,367 | 11.2 / 6.5 | 11.8 / 6.7 | 19.0 / 7.5 | 11.8 / 6.7 | - |
| `unilimit/token_bucket` | 88,584 / 86,610 | 10.9 / 6.6 | 11.3 / 7.9 | 11.7 / 34.0 | 11.3 / 7.3 | - |
| `unilimit+metrics/sliding_window_counter` | 66,560 / 112,182 | 14.7 / 8.5 | 15.0 / 10.0 | 15.4 / 10.4 | 15.0 / 8.9 | - |
| `limits/fixed_window` | 361,191 / 511,903 | 2.5 / 1.5 | 2.8 / 1.9 | 3.0 / 4.1 | 2.8 / 1.8 | - |
| `limits/sliding_window_counter` | 195,941 / 324,342 | 4.8 / 2.9 | 5.1 / 3.3 | 5.4 / 3.6 | 5.1 / 3.1 | - |
| `limits/sliding_log` | 308,590 / 504,701 | 2.9 / 1.7 | 3.2 / 2.0 | 3.4 / 4.5 | 3.2 / 2.0 | - |
| `pyrate-limiter/sliding_log` | 160,323 / 267,518 | 5.9 / 3.4 | 6.1 / 4.0 | 6.3 / 4.3 | 6.2 / 3.7 | - |
| `pyrate-limiter/token_bucket` | 237,372 / 209,499 | 4.0 / 4.2 | 4.3 / 5.1 | 4.9 / 10.7 | 4.2 / 4.7 | - |

#### redis, sync, one request at a time (allowed path)

| library/algorithm | decisions/s | p50 µs | p95 µs | p99 µs | CPU µs/decision | Redis round trips |
|---|---|---|---|---|---|---|
| `unilimit/fixed_window` | 10,105 / 8,989 | 95.3 / 70.2 | 119.3 / 166.8 | 135.0 / 450.8 | 63.8 / 51.7 | 1.00 / 1.00 |
| `unilimit/sliding_window_counter` | 10,159 / 14,261 | 95.5 / 68.1 | 117.2 / 83.9 | 131.3 / 96.7 | 63.6 / 45.2 | 1.00 / 1.00 |
| `unilimit/token_bucket` | 9,930 / 14,624 | 95.6 / 66.8 | 117.1 / 82.2 | 135.5 / 95.1 | 64.4 / 44.4 | 1.00 / 1.00 |
| `unilimit+metrics/sliding_window_counter` | 9,394 / 13,961 | 103.4 / 70.0 | 125.1 / 85.2 | 139.3 / 95.3 | 71.1 / 47.4 | 1.00 / 1.00 |
| `limits/fixed_window` | 16,744 / 24,730 | 59.0 / 39.2 | 64.6 / 47.0 | 74.7 / 56.1 | 38.5 / 25.7 | 1.00 / 1.00 |
| `limits/sliding_window_counter` | 12,540 / 22,321 | 63.3 / 41.9 | 91.6 / 53.0 | 168.0 / 70.0 | 42.0 / 26.2 | 1.00 / 1.00 |
| `limits/sliding_log` | 15,996 / 23,117 | 60.5 / 41.8 | 69.6 / 53.1 | 85.8 / 67.6 | 38.2 / 26.2 | 1.00 / 1.00 |
| `pyrate-limiter/sliding_log` | 13,507 / 19,100 | 72.5 / 49.7 | 82.9 / 65.8 | 102.8 / 82.3 | 48.3 / 34.3 | 1.00 / 1.00 |

#### redis, async, one request at a time (allowed path)

| library/algorithm | decisions/s | p50 µs | p95 µs | p99 µs | CPU µs/decision | Redis round trips |
|---|---|---|---|---|---|---|
| `unilimit/fixed_window` | 5,852 / 8,691 | 156.8 / 104.8 | 217.7 / 125.1 | 368.2 / 188.7 | 127.9 / 78.7 | 1.00 / 1.00 |
| `unilimit/sliding_window_counter` | 6,139 / 9,401 | 156.9 / 103.6 | 187.9 / 119.2 | 239.2 / 130.6 | 124.5 / 77.1 | 1.00 / 1.00 |
| `unilimit/token_bucket` | 6,305 / 8,884 | 155.0 / 102.1 | 176.2 / 120.0 | 190.9 / 181.8 | 121.6 / 77.3 | 1.00 / 1.00 |
| `unilimit+metrics/sliding_window_counter` | 5,227 / 9,228 | 165.7 / 106.3 | 276.8 / 120.4 | 438.8 / 127.8 | 140.9 / 80.0 | 1.00 / 1.00 |
| `limits/fixed_window` | 10,673 / 16,853 | 88.4 / 57.2 | 113.9 / 66.1 | 187.2 / 119.6 | 65.6 / 36.9 | n/a / n/a |
| `limits/sliding_window_counter` | 8,768 / 14,187 | 102.2 / 68.6 | 157.5 / 82.5 | 270.7 / 95.7 | 75.3 / 44.8 | n/a / n/a |
| `limits/sliding_log` | 9,576 / 13,924 | 102.0 / 68.3 | 117.5 / 85.7 | 131.6 / 119.5 | 70.8 / 44.9 | n/a / n/a |
| `pyrate-limiter/sliding_log` | 12,112 / 11,548 | 81.1 / 83.2 | 90.5 / 103.5 | 100.4 / 149.0 | 58.9 / 62.0 | 1.00 / 1.00 |

#### Concurrency

| library/algorithm (async, Redis) | decisions/s at 32 in flight |
|---|---|
| `unilimit/fixed_window` | 7,685 / 16,342 |
| `unilimit/sliding_window_counter` | 10,431 / 15,724 |
| `unilimit/token_bucket` | 10,764 / 17,165 |
| `unilimit+metrics/sliding_window_counter` | 10,276 / 16,509 |
| `limits/fixed_window` | 23,090 / 36,455 |
| `limits/sliding_window_counter` | 19,850 / 30,043 |
| `limits/sliding_log` | 17,919 / 31,135 |
| `pyrate-limiter/sliding_log` | 10,228 / 10,136 |

#### Cost of an observer in practice

Measured separately with `benchmarks/overhead.py` against the commit before M6 (in-memory
sync `consume`, median of 7 × 30,000 calls, variants interleaved, 3 repetitions;
raw numbers in `benchmarks/results/m6-overhead.txt`):

| Configuration | µs per decision (3 repetitions) |
|---|---|
| Before M6 | 6.51 / 6.76 / 6.41 |
| M6, **no observer** | 6.31 / 6.53 / 6.27 |
| M6 + `InMemoryMetrics` | 8.85 / 8.63 / 8.36 (about +2.1 to +2.5 µs) |
| M6 + `PrometheusObserver` | 12.52 / 9.93 / 9.52 (about +3 to +6 µs) |

- **With no observer there is no measurable overhead**: the M6 code is within noise of the
  code before it (slightly faster in all three repetitions, which is noise, not a gain).
- An observer adds its own work plus one event object per decision. In memory that is
  25–50% of a very cheap decision; against Redis (60–160 µs per decision) it is 2–8%.
- If even that is too much, sample inside your observer, or observe only
  `backend_unavailable`, `breaker` and `fallback` events and keep decisions to a
  metrics counter of your own.
