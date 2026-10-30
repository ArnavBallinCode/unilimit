"""Cost of observability on the in-memory hot path, against a checkout from before M6.

    git worktree add /tmp/premain <commit-before-M6>
    for mode in none metrics prom; do
        python benchmarks/overhead.py /tmp/premain/src none     # baseline: old code
        python benchmarks/overhead.py src $mode                 # current code
    done

Prints the median of 7 x 30,000 `consume` calls in microseconds. Run the variants
interleaved, several times: single numbers on a laptop vary by tens of percent.
"""

from __future__ import annotations

import gc
import statistics
import sys
import time

sys.path.insert(0, sys.argv[1])
from unilimit import Limiter, Rule

mode = sys.argv[2]
kwargs = {}
if mode == "metrics":
    from unilimit import InMemoryMetrics

    kwargs["observer"] = InMemoryMetrics()
elif mode == "prom":
    from prometheus_client import CollectorRegistry

    from unilimit.integrations.prometheus import PrometheusObserver

    kwargs["observer"] = PrometheusObserver(CollectorRegistry())

limiter = Limiter.from_url("memory://", **kwargs)
rule = Rule("b", "1000000000/3600s", algorithm="sliding_window")
keys = [f"k{i}" for i in range(1000)]
for i in range(3000):
    limiter.consume(rule, keys[i % 1000])
samples = []
gc.disable()
for _ in range(7):
    start = time.perf_counter()
    for i in range(30000):
        limiter.consume(rule, keys[i % 1000])
    samples.append((time.perf_counter() - start) / 30000 * 1e6)
print(f"{statistics.median(samples):.2f}")
