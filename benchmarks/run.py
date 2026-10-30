"""Run the benchmark suites and write raw results.

uv run --group bench python -m benchmarks.run --suites decision --n 20000
uv run --group bench python -m benchmarks.run --quick            # smoke run
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import time
from pathlib import Path
from typing import Any

from benchmarks.common import RedisServer, environment
from benchmarks.contenders import contenders


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suites", default="decision,http,workers")
    parser.add_argument("--n", type=int, default=20_000, help="decisions per measurement")
    parser.add_argument("--quick", action="store_true", help="tiny run to check the tooling")
    parser.add_argument("--only", default="", help="substring filter on contender names")
    parser.add_argument(
        "--concurrency",
        default="1,32",
        help="in-flight operations for async runs, e.g. 1,8,32,64,128",
    )
    parser.add_argument("--scenarios", default="allow,reject")
    parser.add_argument(
        "--profile-calls",
        action="store_true",
        help="add Python calls and retained bytes per decision",
    )
    parser.add_argument("--redis-binary", default=None)
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)
    n = 500 if args.quick else args.n
    suites = set(args.suites.split(","))

    server = RedisServer(args.redis_binary).start()
    results: dict[str, Any] = {"suites": {}}
    try:
        results["environment"] = environment(server.binary, server.version())
        results["parameters"] = {"n": n, "quick": args.quick, "redis_url": "private server"}
        if "decision" in suites:
            from benchmarks.decision import measure, profile_calls

            levels = tuple(int(x) for x in args.concurrency.split(","))
            scenarios = tuple(args.scenarios.split(","))
            records = []
            for c in contenders():
                if args.only and args.only not in c.name:
                    continue
                for scenario in scenarios:
                    concurrencies = levels if c.mode == "async" else (1,)
                    for concurrency in concurrencies:
                        if scenario == "reject" and concurrency != 1:
                            continue
                        rec = measure(
                            c, server if c.backend == "redis" else None, n, scenario, concurrency
                        )
                        if args.profile_calls and scenario == "allow" and concurrency == 1:
                            rec.update(profile_calls(c, server if c.backend == "redis" else None))
                        records.append(rec)
                        print(
                            f"{rec['contender']:55s} {scenario:6s} c={rec['concurrency']:<3d} "
                            f"{rec['ops_per_second']:10.0f} ops/s  p50 {rec['p50_us']:7.1f}us "
                            f"p99 {rec['p99_us']:8.1f}us  rt/dec {rec['round_trips_per_decision']}",
                            flush=True,
                        )
            results["suites"]["decision"] = records
        if "http" in suites:
            from benchmarks.http import run_http

            results["suites"]["http"] = run_http(server, 200 if args.quick else max(2000, n // 4))
        if "workers" in suites:
            from benchmarks.workers import run_workers

            results["suites"]["workers"] = run_workers(server, quick=args.quick)
    finally:
        server.stop()

    out = (
        Path(args.out)
        if args.out
        else Path("benchmarks/results")
        / (time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + f"-{socket.gethostname()}.json")
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=2) + "\n")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
