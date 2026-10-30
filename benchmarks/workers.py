"""Multi-process benchmark against one shared Redis: throughput scaling and exactness.

Exactness: every process hammers the same key; a library with an exact, atomic limit
admits exactly ``limit`` requests in total, however many processes there are.
"""

from __future__ import annotations

import multiprocessing as mp
import time
from typing import Any

from benchmarks.common import RedisServer

LIMIT = 1000
ATTEMPTS_PER_WORKER = 1500
THROUGHPUT_OPS = 5000
NAMES = (
    "unilimit/fixed_window/redis/sync",
    "unilimit/sliding_window_counter/redis/sync",
    "limits/fixed_window/redis/sync",
    "limits/sliding_window_counter/redis/sync",
    "limits/sliding_log/redis/sync",
    "pyrate-limiter/sliding_log/redis/sync",
)


def _child(name: str, url: str, mode: str, ops: int, start_at: float, out: Any) -> None:
    from benchmarks.contenders import contenders

    contender = next(c for c in contenders() if c.name == name)
    if mode == "exact":
        inst = contender.build(url, LIMIT, 3600)
        keys = ["shared"]
    else:
        inst = contender.build(url, 10**9, 3600)
        keys = [f"w{i}" for i in range(500)]
    warm = [f"warm{i}" for i in range(50)]  # never the measured key: it would eat quota
    inst.prepare(keys + warm)
    for key in warm:
        inst.hit(key)
    while time.time() < start_at:  # all workers start together
        time.sleep(0.001)
    t0 = time.perf_counter()
    admitted = sum(1 for i in range(ops) if inst.hit(keys[i % len(keys)]))
    elapsed = time.perf_counter() - t0
    inst.close()
    out.put((admitted, elapsed))


def _run(name: str, url: str, mode: str, workers: int, ops: int) -> tuple[int, float]:
    ctx = mp.get_context("spawn")
    out: Any = ctx.Queue()
    start_at = time.time() + 3 + workers * 0.5  # spawn + import time
    procs = [
        ctx.Process(target=_child, args=(name, url, mode, ops, start_at, out))
        for _ in range(workers)
    ]
    for p in procs:
        p.start()
    results = [out.get(timeout=300) for _ in procs]
    for p in procs:
        p.join(30)
    return sum(r[0] for r in results), max(r[1] for r in results)


def run_workers(server: RedisServer, quick: bool = False) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    counts = (1, 2) if quick else (1, 2, 4)
    for name in NAMES:
        for workers in counts:
            ops = 200 if quick else THROUGHPUT_OPS
            server.flush()
            admitted, elapsed = _run(name, server.url, "throughput", workers, ops)
            rate = workers * ops / elapsed
            attempts = 300 if quick else ATTEMPTS_PER_WORKER
            server.flush()
            exact_admitted, _ = _run(name, server.url, "exact", workers, attempts)
            records.append(
                {
                    "contender": name,
                    "workers": workers,
                    "aggregate_ops_per_second": rate,
                    "exactness_limit": LIMIT,
                    "exactness_attempts": workers * attempts,
                    "exactness_admitted": exact_admitted,
                    "throughput_admitted_of": [admitted, workers * ops],
                }
            )
            print(
                f"workers {name:45s} w={workers} {rate:9.0f} ops/s  "
                f"exactness admitted {exact_admitted} of limit {LIMIT} "
                f"({workers * attempts} attempts)",
                flush=True,
            )
    return records
