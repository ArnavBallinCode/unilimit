"""Turn raw result files into the markdown tables of docs/observability/benchmarks.md.

    python -m benchmarks.report benchmarks/results/m6-run1.json benchmarks/results/m6-run2.json

With several files, each figure is shown for every run ("run1 / run2"), so run-to-run
variability is visible instead of hidden behind one number.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def fmt(values: list[float | None], digits: int = 0) -> str:
    return " / ".join("n/a" if v is None else f"{v:,.{digits}f}" for v in values)


def table(headers: list[str], rows: list[list[str]]) -> str:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    out += ["| " + " | ".join(r) + " |" for r in rows]
    return "\n".join(out)


def collect(
    runs: list[dict[str, Any]], suite: str, key: tuple[str, ...]
) -> dict[tuple[Any, ...], list[dict[str, Any]]]:
    grouped: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for run in runs:
        for rec in run["suites"].get(suite, []):
            grouped.setdefault(tuple(rec[k] for k in key), []).append(rec)
    return grouped


def decision_tables(runs: list[dict[str, Any]]) -> str:
    grouped = collect(runs, "decision", ("backend", "mode", "scenario", "concurrency", "contender"))
    out = []
    for backend in ("memory", "redis"):
        for mode in ("sync", "async"):
            rows = []
            for (b, m, scenario, conc, name), recs in grouped.items():
                if (b, m) != (backend, mode) or scenario != "allow" or conc != 1:
                    continue
                rows.append([
                    f"`{name.rsplit('/', 2)[0]}`",
                    fmt([r["ops_per_second"] for r in recs]),
                    fmt([r["p50_us"] for r in recs], 1),
                    fmt([r["p95_us"] for r in recs], 1),
                    fmt([r["p99_us"] for r in recs], 1),
                    fmt([r["cpu_us_per_op"] for r in recs], 1),
                    fmt([r["round_trips_per_decision"] for r in recs], 2) if backend == "redis" else "-",
                ])  # fmt: skip
            out.append(f"#### {backend}, {mode}, one request at a time (allowed path)\n")
            out.append(
                table(
                    [
                        "library/algorithm",
                        "decisions/s",
                        "p50 µs",
                        "p95 µs",
                        "p99 µs",
                        "CPU µs/decision",
                        "Redis round trips",
                    ],
                    rows,
                )
            )
            out.append("")
    return "\n".join(out)


def concurrent_table(runs: list[dict[str, Any]]) -> str:
    grouped = collect(runs, "decision", ("backend", "mode", "scenario", "concurrency", "contender"))
    rows = []
    for (b, m, scenario, conc, name), recs in grouped.items():
        if m != "async" or scenario != "allow" or conc != 32 or b != "redis":
            continue
        rows.append([f"`{name.rsplit('/', 2)[0]}`", fmt([r["ops_per_second"] for r in recs])])
    return table(["library/algorithm (async, Redis)", "decisions/s at 32 in flight"], rows)


def overhead_table(runs: list[dict[str, Any]]) -> str:
    grouped = collect(runs, "decision", ("backend", "mode", "scenario", "concurrency", "contender"))
    rows = []
    for backend in ("memory", "redis"):
        for mode in ("sync", "async"):
            base = grouped.get(
                (backend, mode, "allow", 1, f"unilimit/sliding_window_counter/{backend}/{mode}")
            )
            obs = grouped.get(
                (
                    backend,
                    mode,
                    "allow",
                    1,
                    f"unilimit+metrics/sliding_window_counter/{backend}/{mode}",
                )
            )
            if not base or not obs:
                continue
            b = [r["p50_us"] for r in base]
            o = [r["p50_us"] for r in obs]
            added = [x - y for x, y in zip(o, b, strict=False)]
            rows.append([backend, mode, fmt(b, 1), fmt(o, 1), fmt(added, 1)])
    return table(
        ["backend", "mode", "p50 µs, no observer", "p50 µs, InMemoryMetrics", "added µs"], rows
    )


def http_table(runs: list[dict[str, Any]]) -> str:
    grouped = collect(runs, "http", ("backend", "contender", "concurrency"))
    rows = []
    for (backend, name, conc), recs in grouped.items():
        rows.append([
            backend, name, str(conc), fmt([r["ops_per_second"] for r in recs]),
            fmt([r["p50_us"] for r in recs], 1), fmt([r["p99_us"] for r in recs], 1),
        ])  # fmt: skip
    return table(["backend", "setup", "in flight", "requests/s", "p50 µs", "p99 µs"], rows)


def workers_table(runs: list[dict[str, Any]]) -> str:
    grouped = collect(runs[-1:], "workers", ("contender", "workers"))  # latest run only
    rows = []
    for (name, workers), recs in grouped.items():
        r0 = recs[-1]
        rows.append([
            f"`{name.rsplit('/', 2)[0]}`", str(workers),
            fmt([r["aggregate_ops_per_second"] for r in recs]),
            f"{r0['exactness_admitted']} of {r0['exactness_limit']} ({r0['exactness_attempts']:,} attempts)",
        ])  # fmt: skip
    return table(
        ["library/algorithm", "processes", "aggregate decisions/s", "admitted on one shared key"],
        rows,
    )


def environment(run: dict[str, Any]) -> str:
    env = run["environment"]
    pk = env["packages"]
    lines = [
        f"- Machine: {env['processor'] or env['machine']}, {env['cpu_count']} cores, {env.get('memory_total_gb', '?')} GB, {env['platform']}",
        f"- Python {env['python']} ({env['implementation']})",
        f"- Server: {env['redis_version']} on localhost (private instance, no persistence)",
        "- Libraries: " + ", ".join(f"{k} {v}" for k, v in pk.items()),
        f"- Run at {env['timestamp_utc']}; n={run['parameters']['n']} decisions per measurement",
    ]
    return "\n".join(lines)


def main(paths: list[str]) -> None:
    runs = [json.loads(Path(p).read_text()) for p in paths]
    print("<!-- generated by benchmarks/report.py from:", ", ".join(paths), "-->\n")
    for i, run in enumerate(runs, 1):
        print(f"Run {i}:\n\n{environment(run)}\n")
    print("### Library-level decisions\n\n" + decision_tables(runs))
    print("### Concurrency\n\n" + concurrent_table(runs) + "\n")
    print("### Observability overhead\n\n" + overhead_table(runs) + "\n")
    print("### HTTP (FastAPI, ASGI in-process)\n\n" + http_table(runs) + "\n")
    print("### Multiple processes, one Redis\n\n" + workers_table(runs) + "\n")


if __name__ == "__main__":
    main(sys.argv[1:])
