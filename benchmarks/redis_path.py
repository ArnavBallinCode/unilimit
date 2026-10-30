"""A/B harness for the Redis hot path: UniLimit against an in-process raw-EVALSHA floor.

    python -m benchmarks.redis_path --src src --label baseline --out /tmp/a.json

Why a floor: a laptop's absolute numbers drift by tens of percent between runs, but a
ratio taken in the same process, interleaved round by round, drifts far less. The floor
is `limits`' fixed-window script (`INCRBY` + conditional `EXPIRE`, an integer reply)
called through redis-py: the cheapest thing a client can do per decision.

Each configuration runs ROUNDS interleaved rounds of OPS decisions; the figure is the
median of the per-round means. `--src` puts another checkout's `src/` first on the path
so the same harness can measure the code before and after a change.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

ROUNDS = 9
OPS = 2000
ALGORITHMS = ("fixed_window", "sliding_window", "token_bucket")
FLOOR_LUA = """
local current = redis.call("incrby", KEYS[1], tonumber(ARGV[2]))
if tonumber(current) == tonumber(ARGV[2]) then redis.call("expire", KEYS[1], ARGV[1]) end
return current
"""  # limits 5.8.0: resources/redis/lua_scripts/incr_expire.lua (MIT), reproduced for the floor


def median_round_us(rounds: list[tuple[float, float]], ops: int) -> tuple[float, float]:
    wall = statistics.median(w for w, _ in rounds) / ops * 1e6
    cpu = statistics.median(c for _, c in rounds) / ops * 1e6
    return wall, cpu


async def arun(fn: Any, conc: int, ops: int) -> tuple[float, float]:
    t0, c0 = time.perf_counter(), time.process_time()
    if conc == 1:
        for i in range(ops):
            await fn(i)
    else:
        for start in range(0, ops, conc):
            await asyncio.gather(*(fn(i) for i in range(start, min(ops, start + conc))))
    return time.perf_counter() - t0, time.process_time() - c0


def srun(fn: Any, ops: int) -> tuple[float, float]:
    t0, c0 = time.perf_counter(), time.process_time()
    for i in range(ops):
        fn(i)
    return time.perf_counter() - t0, time.process_time() - c0


async def main_async(url: str) -> dict[str, Any]:
    import redis
    import redis.asyncio as ra

    from unilimit import Limiter, Rule

    out: dict[str, Any] = {}
    ar = ra.Redis.from_url(url)
    sr = redis.Redis.from_url(url)
    afloor, sfloor = ar.register_script(FLOOR_LUA), sr.register_script(FLOOR_LUA)
    limiters = {a: Limiter.from_url(url) for a in ALGORITHMS}
    rules = {
        a: Rule("b", "1000000000/3600s", algorithm=a, burst=10**9 if a == "token_bucket" else None)
        for a in ALGORITHMS
    }

    async def floor_a(i: int) -> Any:
        return await afloor(keys=[f"f{i % 1000}"], args=[3600, 1], client=ar)

    configs: list[tuple[str, int, Any]] = [("floor", 1, floor_a), ("floor", 32, floor_a)]
    for a in ALGORITHMS:
        lim, rule = limiters[a], rules[a]

        async def call(i: int, lim: Any = lim, rule: Any = rule) -> Any:
            return await lim.aconsume(rule, f"k{i % 1000}")

        configs += [(f"async/{a}", 1, call), (f"async/{a}", 32, call)]
    for _, _, fn in configs:  # warm up every path
        await arun(fn, 1, 300)
    samples: dict[tuple[str, int], list[tuple[float, float]]] = {}
    for _ in range(ROUNDS):  # interleaved: drift hits every config alike
        for name, conc, fn in configs:
            samples.setdefault((name, conc), []).append(await arun(fn, conc, OPS))
        for name, fn in (("sync/floor", lambda i: sfloor(keys=[f"f{i % 1000}"], args=[3600, 1])),):
            samples.setdefault((name, 1), []).append(srun(fn, OPS))
        for a in ALGORITHMS:
            lim, rule = limiters[a], rules[a]
            samples.setdefault((f"sync/{a}", 1), []).append(
                srun(lambda i, lim=lim, rule=rule: lim.consume(rule, f"k{i % 1000}"), OPS)
            )
    for (name, conc), rounds in samples.items():
        wall, cpu = median_round_us(rounds, OPS)
        out[f"{name}@{conc}"] = {"us_per_op": wall, "cpu_us_per_op": cpu}
    for lim in limiters.values():
        await lim.aclose()
        lim.close()
    await ar.aclose()
    sr.close()
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--src", default="src")
    parser.add_argument("--label", default="run")
    parser.add_argument("--out", default=None)
    args = parser.parse_args()
    sys.path.insert(0, str(Path(args.src).resolve()))
    from benchmarks.common import RedisServer

    server = RedisServer().start()
    try:
        data = asyncio.run(main_async(server.url))
    finally:
        server.stop()
    import redis

    hiredis = "hiredis" in sys.modules or _has("hiredis")
    result = {"label": args.label, "hiredis": hiredis, "redis_py": redis.__version__, "ops": data}
    text = json.dumps(result, indent=2)
    if args.out:
        Path(args.out).write_text(text + "\n")
    floor1 = data["floor@1"]["us_per_op"]
    print(f"[{args.label}] hiredis={hiredis}  (ratios are vs the raw-EVALSHA floor, same process)")
    for key, val in data.items():
        base = data["floor@32"] if key.endswith("@32") else data["floor@1"]
        print(
            f"  {key:26s} {val['us_per_op']:8.1f} us/op  cpu {val['cpu_us_per_op']:7.1f}  "
            f"x{val['us_per_op'] / base['us_per_op']:.2f} of floor"
        )
    del floor1


def _has(module: str) -> bool:
    import importlib.util

    return importlib.util.find_spec(module) is not None


if __name__ == "__main__":
    main()
