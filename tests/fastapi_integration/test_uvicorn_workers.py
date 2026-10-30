"""End to end: real uvicorn worker processes, real TCP, shared Redis."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Iterator

import httpx
import pytest

from tests._servers import free_port

WORKERS = 3


@pytest.fixture(params=["redis", "cluster"])
def served(request: pytest.FixtureRequest) -> Iterator[str]:
    if request.param == "redis":
        url, cluster = request.getfixturevalue("redis_url"), "0"
    else:
        url, cluster = request.getfixturevalue("cluster_url"), "1"
    port = free_port()
    env = {
        **os.environ,
        "UNILIMIT_TEST_REDIS_URL": url,
        "UNILIMIT_TEST_CLUSTER": cluster,
        "PYTHONWARNINGS": "error::ResourceWarning",
    }
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "tests.fastapi_integration._uvicorn_app:app",
            "--port",
            str(port),
            "--workers",
            str(WORKERS),
            "--log-level",
            "warning",
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 30
    pids: set[int] = set()
    while len(pids) < WORKERS:  # wait until every worker answers
        with contextlib.suppress(httpx.HTTPError):
            pids.add(httpx.get(f"{base}/health", timeout=1).json()["pid"])
        if time.monotonic() > deadline:
            proc.kill()
            startup_output = proc.stderr.read().decode() if proc.stderr else ""
            raise TimeoutError(f"uvicorn did not start: {startup_output}")
        time.sleep(0.05)
    yield base
    proc.send_signal(signal.SIGTERM)
    _, stderr = proc.communicate(timeout=30)
    assert proc.returncode == 0, stderr.decode()
    assert b"Traceback" not in stderr, stderr.decode()  # clean lifespan shutdown everywhere


async def test_multiple_workers_share_exact_limits(served: str) -> None:
    limits = httpx.Limits(max_connections=60, max_keepalive_connections=0)  # spread workers
    async with httpx.AsyncClient(base_url=served, limits=limits, timeout=30) as c:
        endpoint = await asyncio.gather(
            *(c.get("/limited" if i % 2 else "/sync-limited") for i in range(300))
        )
        statuses = Counter(r.status_code for r in endpoint)
        # 100/day per rule: "/limited" and "/sync-limited" use different rule names.
        assert statuses[200] == 200
        assert statuses[429] == 100
        pids = {r.json()["pid"] for r in endpoint if r.status_code == 200}
        assert len(pids) >= 2  # admissions really came from several worker processes

        # The global rule (400/day) has absorbed 300 requests so far.
        more = await asyncio.gather(*(c.get("/health") for _ in range(5)))  # exempt
        assert all(r.status_code == 200 for r in more)
        rest = await asyncio.gather(*(c.get("/missing") for _ in range(150)))
        assert Counter(r.status_code for r in rest) == {404: 100, 429: 50}
