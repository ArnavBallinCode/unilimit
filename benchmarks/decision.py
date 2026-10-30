"""Library-level benchmark: one rate-limit decision per call, no web framework."""

from __future__ import annotations

import asyncio
import gc
import time
from typing import Any

from benchmarks.common import (
    RedisServer,
    RoundTrips,
    Timing,
    server_commands_delta,
)
from benchmarks.contenders import Contender, Instance

KEYS = 1000
WARMUP = 500


def _keys() -> list[str]:
    return [f"k{i}" for i in range(KEYS)]


def run_sync(inst: Instance, n: int, keys: list[str]) -> tuple[Timing, int, int]:
    hit = inst.hit
    for i in range(WARMUP):
        hit(keys[i % len(keys)])
    latencies: list[int] = []
    admitted = 0
    gc.collect()
    gc.disable()
    cpu0, t0 = time.process_time(), time.perf_counter()
    try:
        with RoundTrips() as trips:
            for i in range(n):
                s = time.perf_counter_ns()
                ok = hit(keys[i % len(keys)])
                latencies.append(time.perf_counter_ns() - s)
                admitted += ok
    finally:
        gc.enable()
    seconds = time.perf_counter() - t0
    return Timing(n, seconds, latencies, time.process_time() - cpu0), admitted, trips.count


async def run_async(
    inst: Instance, n: int, keys: list[str], concurrency: int
) -> tuple[Timing, int, int]:
    hit = inst.ahit
    for i in range(WARMUP):
        await hit(keys[i % len(keys)])
    latencies: list[int] = []
    admitted = 0
    gc.collect()
    cpu0, t0 = time.process_time(), time.perf_counter()

    async def one(i: int) -> bool:
        s = time.perf_counter_ns()
        ok = await hit(keys[i % len(keys)])
        latencies.append(time.perf_counter_ns() - s)
        return ok

    with RoundTrips() as trips:
        if concurrency == 1:
            for i in range(n):
                admitted += await one(i)
        else:
            for start in range(0, n, concurrency):
                results = await asyncio.gather(
                    *(one(i) for i in range(start, min(n, start + concurrency)))
                )
                admitted += sum(results)
    seconds = time.perf_counter() - t0
    return Timing(n, seconds, latencies, time.process_time() - cpu0), admitted, trips.count


async def _measure(
    c: Contender, server: RedisServer | None, n: int, scenario: str, concurrency: int
) -> dict[str, Any]:
    if server is not None:
        server.flush()
    limit, window = (10**9, 3600) if scenario == "allow" else (1, 3600)
    inst = c.build(server.url if server else None, limit, window)
    keys = _keys() if scenario == "allow" else ["only"]
    try:
        await inst.aprepare(keys)
        if server is not None:
            before = server.commandstats()
        if c.mode == "sync":
            timing, admitted, trips = run_sync(inst, n, keys)
        else:
            timing, admitted, trips = await run_async(inst, n, keys, concurrency)
        record: dict[str, Any] = {
            "contender": c.name,
            "family": c.family,
            "algorithm": c.algorithm,
            "backend": c.backend,
            "mode": c.mode,
            "scenario": scenario,
            "concurrency": concurrency if c.mode == "async" else 1,
            **timing.summary(),
            "admitted": admitted,
            # None: no redis-py traffic seen (limits' asyncio storage uses coredis)
            "round_trips_per_decision": (trips / n if trips else None) if server else 0.0,
        }
        if server is not None:
            delta = server_commands_delta(before, server.commandstats())
            record["server_commands_per_decision"] = {  # the delta includes the warm-up calls
                cmd: round(count / (n + WARMUP), 3) for cmd, count in sorted(delta.items())
            }
        return record
    finally:
        await inst.aclose()


def measure(
    c: Contender, server: RedisServer | None, n: int, scenario: str, concurrency: int = 1
) -> dict[str, Any]:
    return asyncio.run(_measure(c, server, n, scenario, concurrency))
