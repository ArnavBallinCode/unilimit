"""Worker entry points for the multi-process tests (imported by spawned processes)."""

from __future__ import annotations

import asyncio
import functools
import traceback
from collections.abc import Callable
from typing import Any

from unilimit import Limiter, RequestContext, Rule
from unilimit.testing import ManualClock

FROZEN_NOW = 1_800_000_000_000_000


def build(url: str, cluster: bool, frozen: bool) -> Limiter:
    """Generous timeouts and no cooldown: these tests measure atomicity under contention.

    On a busy CI machine, 8 processes plus Redis can starve each other of CPU for longer
    than production-style timeouts. That would fail open by design, which is covered by
    the reliability tests, and would hide what is being measured here.
    """
    clock = ManualClock(FROZEN_NOW) if frozen else None
    if url.startswith("memory://"):
        return Limiter.from_url(url, clock=clock)
    return Limiter.from_url(
        url,
        cluster=cluster,
        clock=clock,
        socket_timeout=10,
        socket_connect_timeout=10,
        pool_timeout=30,
        cooldown=0,
    )


def reports_errors(fn: Callable[..., None]) -> Callable[..., None]:
    """Send any exception to the parent instead of dying silently before the barrier."""

    @functools.wraps(fn)
    def wrapper(*args: Any) -> None:
        out = args[-1]
        try:
            fn(*args)
        except BaseException:
            out.put({"error": traceback.format_exc()})
            raise

    return wrapper


@reports_errors
def single_identity(
    url: str,
    cluster: bool,
    frozen: bool,
    rule: Rule,
    attempts: int,
    use_async: bool,
    barrier: Any,
    out: Any,
) -> None:
    limiter = build(url, cluster, frozen)
    barrier.wait(60)
    if use_async:

        async def run() -> list[Any]:
            results = await asyncio.gather(
                *(limiter.aconsume(rule, "shared") for _ in range(attempts))
            )
            await limiter.aclose()
            return list(results)

        results = asyncio.run(run())
    else:
        results = [limiter.consume(rule, "shared") for _ in range(attempts)]
        limiter.close()
    out.put(
        {
            "admitted": sum(r.allowed and not r.degraded for r in results),
            "degraded": sum(r.degraded for r in results),
        }
    )


SHARED = Rule("shared", "300/day", key="global", algorithm="fixed_window")
PER_USER = Rule("per-user", "1000/day", key="user", algorithm="fixed_window")


@reports_errors
def multi_rule(
    url: str,
    cluster: bool,
    frozen: bool,
    users: int,
    attempts: int,
    use_async: bool,
    worker: int,
    barrier: Any,
    out: Any,
) -> None:
    limiter = build(url, cluster, frozen)
    rules = [PER_USER, SHARED]
    contexts = [RequestContext(user_id=f"u{(worker + i) % users}") for i in range(attempts)]
    barrier.wait(60)
    if use_async:

        async def run() -> list[Any]:
            decisions = await asyncio.gather(*(limiter.aevaluate(c, rules) for c in contexts))
            await limiter.aclose()
            return list(decisions)

        decisions = asyncio.run(run())
    else:
        decisions = [limiter.evaluate(c, rules) for c in contexts]
        limiter.close()
    out.put(
        {
            "admitted": sum(d.allowed and not d.degraded for d in decisions),
            "degraded": sum(d.degraded for d in decisions),
            "partial": sum(d.partial_commit for d in decisions),
        }
    )
