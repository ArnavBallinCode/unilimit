"""Shared benchmark helpers: environment capture, statistics, a throwaway Redis server
and a client-side round-trip counter."""

from __future__ import annotations

import contextlib
import os
import platform
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Any

PACKAGES = (
    "unilimit", "limits", "slowapi", "pyrate-limiter", "fastapi-limiter", "fastapi",
    "starlette", "redis", "coredis", "uvicorn", "psutil",
)  # fmt: skip


def environment(
    redis_binary: str | None = None, redis_version: str | None = None
) -> dict[str, Any]:
    versions = {}
    for name in PACKAGES:
        with contextlib.suppress(metadata.PackageNotFoundError):
            versions[name] = metadata.version(name)
    info: dict[str, Any] = {
        "python": sys.version.split()[0],
        "implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "cpu_count": os.cpu_count(),
        "packages": versions,
        "redis_binary": redis_binary,
        "redis_version": redis_version,
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    with contextlib.suppress(Exception):
        import psutil

        info["memory_total_gb"] = round(psutil.virtual_memory().total / 2**30, 1)
    return info


def percentile(sorted_ns: list[int], q: float) -> float:
    """``q`` in [0, 100]; nearest-rank on a sorted list; returns microseconds."""
    if not sorted_ns:
        return float("nan")
    index = min(len(sorted_ns) - 1, max(0, round(q / 100 * len(sorted_ns) + 0.5) - 1))
    return sorted_ns[index] / 1000


@dataclass
class Timing:
    ops: int
    seconds: float
    latencies_ns: list[int]
    cpu_seconds: float

    def summary(self) -> dict[str, float]:
        ordered = sorted(self.latencies_ns)
        return {
            "ops": self.ops,
            "ops_per_second": self.ops / self.seconds if self.seconds else float("nan"),
            "p50_us": percentile(ordered, 50),
            "p95_us": percentile(ordered, 95),
            "p99_us": percentile(ordered, 99),
            "mean_us": statistics.fmean(ordered) / 1000 if ordered else float("nan"),
            "cpu_us_per_op": self.cpu_seconds / self.ops * 1e6 if self.ops else float("nan"),
        }


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class RedisServer:
    """A private ``redis-server`` (or ``valkey-server``) on a free port; never touches an
    existing Redis."""

    def __init__(self, binary: str | None = None) -> None:
        found = binary or os.environ.get("UNILIMIT_REDIS_SERVER")
        found = found or shutil.which("redis-server") or shutil.which("valkey-server")
        if not found:
            raise SystemExit("redis-server not found: install it or set UNILIMIT_REDIS_SERVER")
        self.binary = found
        self.port = free_port()
        self.dir = Path(tempfile.mkdtemp(prefix="unilimit-bench-"))
        self.proc: subprocess.Popen[bytes] | None = None

    @property
    def url(self) -> str:
        return f"redis://127.0.0.1:{self.port}/0"

    def start(self) -> RedisServer:
        self.proc = subprocess.Popen(
            [self.binary, "--port", str(self.port), "--bind", "127.0.0.1", "--save", "",
             "--appendonly", "no", "--dir", str(self.dir), "--protected-mode", "no"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )  # fmt: skip
        import redis

        deadline = time.monotonic() + 10
        client = redis.Redis(port=self.port)
        while True:
            try:
                client.ping()
                break
            except redis.ConnectionError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.05)
        client.close()
        return self

    def client(self) -> Any:
        import redis

        return redis.Redis(port=self.port)

    def version(self) -> str:
        c = self.client()
        info = c.info("server")
        c.close()
        name = "valkey" if "valkey_version" in info else "redis"
        return f"{name} {info.get('valkey_version') or info['redis_version']}"

    def flush(self) -> None:
        c = self.client()
        c.flushall()
        c.close()

    def commandstats(self) -> dict[str, int]:
        c = self.client()
        stats = c.info("commandstats")
        c.close()
        return {k.removeprefix("cmdstat_"): int(v["calls"]) for k, v in stats.items()}

    def stop(self) -> None:
        if self.proc is not None:
            self.proc.terminate()
            with contextlib.suppress(Exception):
                self.proc.wait(10)
            self.proc = None
        shutil.rmtree(self.dir, ignore_errors=True)


_IGNORED_COMMANDS = {"info", "config", "client", "hello", "ping", "flushall", "flushdb", "script"}


def server_commands_delta(before: dict[str, int], after: dict[str, int]) -> dict[str, int]:
    """Calls per command between two snapshots, excluding housekeeping. Commands that a
    Lua script runs internally are included (they appear in the server's statistics)."""
    return {
        cmd: after.get(cmd, 0) - before.get(cmd, 0)
        for cmd in after
        if cmd.split("|")[0] not in _IGNORED_COMMANDS and after[cmd] != before.get(cmd, 0)
    }


class RoundTrips:
    """Counts client-side Redis round trips by wrapping redis-py's ``send_packed_command``
    (a pipeline or MULTI block is one round trip). Works for sync and asyncio clients."""

    def __init__(self) -> None:
        self.count = 0
        self._restore: list[tuple[Any, Any]] = []

    def __enter__(self) -> RoundTrips:
        import redis.asyncio.connection as aconn
        import redis.connection as conn

        counter = self
        original_sync = conn.AbstractConnection.send_packed_command
        original_async = aconn.AbstractConnection.send_packed_command

        def sync_wrapper(self_: Any, command: Any, check_health: bool = True) -> Any:
            counter.count += 1
            return original_sync(self_, command, check_health=check_health)

        async def async_wrapper(self_: Any, command: Any, check_health: bool = True) -> Any:
            counter.count += 1
            return await original_async(self_, command, check_health=check_health)

        self._restore = [
            (conn.AbstractConnection, original_sync),
            (aconn.AbstractConnection, original_async),
        ]
        conn.AbstractConnection.send_packed_command = sync_wrapper  # type: ignore[method-assign]
        aconn.AbstractConnection.send_packed_command = async_wrapper  # type: ignore[method-assign]
        return self

    def __exit__(self, *exc: object) -> None:
        for cls, original in self._restore:
            cls.send_packed_command = original  # type: ignore[method-assign]


@contextlib.contextmanager
def cpu_timer() -> Iterator[list[float]]:
    out = [0.0]
    start = time.process_time()
    try:
        yield out
    finally:
        out[0] = time.process_time() - start
