"""End to end: real uvicorn (ASGI) and gunicorn (WSGI) worker processes, real TCP,
shared Redis."""

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


def _command(server: str, port: int) -> list[str]:
    if server == "uvicorn":
        return [
            sys.executable, "-m", "uvicorn",
            "tests.django_integration._e2e_app:asgi_application",
            "--port", str(port), "--workers", str(WORKERS), "--log-level", "warning",
        ]  # fmt: skip
    return [
        sys.executable, "-m", "gunicorn",
        "tests.django_integration._e2e_app:wsgi_application",
        "--bind", f"127.0.0.1:{port}", "--workers", str(WORKERS), "--threads", "4",
        "--log-level", "warning",
    ]  # fmt: skip


@pytest.fixture(params=["uvicorn-redis", "uvicorn-cluster", "gunicorn-redis", "gunicorn-cluster"])
def served(request: pytest.FixtureRequest) -> Iterator[str]:
    server, kind = request.param.split("-")
    if kind == "redis":
        url, cluster = request.getfixturevalue("redis_url"), "0"
    else:
        url, cluster = request.getfixturevalue("cluster_url"), "1"
    port = free_port()
    env = {
        **os.environ,
        "DJANGO_SETTINGS_MODULE": "tests.django_integration._e2e_settings",
        "UNILIMIT_TEST_REDIS_URL": url,
        "UNILIMIT_TEST_CLUSTER": cluster,
        "PYTHONWARNINGS": "error::ResourceWarning",
    }
    proc = subprocess.Popen(
        _command(server, port), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 30
    pids: set[int] = set()
    while len(pids) < WORKERS:  # wait until every worker answers
        with contextlib.suppress(httpx.HTTPError):
            pids.add(httpx.get(f"{base}/health", timeout=1).json()["pid"])
        if time.monotonic() > deadline:
            proc.kill()
            output = proc.stderr.read().decode() if proc.stderr else ""
            raise TimeoutError(f"{server} did not start: {output}")
        time.sleep(0.05)
    yield base
    proc.send_signal(signal.SIGTERM)
    _, stderr = proc.communicate(timeout=30)
    assert proc.returncode == 0, stderr.decode()
    output = stderr.decode()
    assert "Traceback" not in output, output
    # No leaked Redis connections. (A uvicorn worker may report the listening socket it
    # inherited from the supervisor; that one is not ours.)
    leaks = [
        line
        for line in output.splitlines()
        if "ResourceWarning" in line and f"'127.0.0.1', {port})" not in line
    ]
    assert not leaks, output


async def test_workers_share_exact_limits(served: str) -> None:
    limits = httpx.Limits(max_connections=60, max_keepalive_connections=0)  # spread workers
    async with httpx.AsyncClient(base_url=served, limits=limits, timeout=30) as c:
        responses = await asyncio.gather(
            *(c.get(("/sync", "/async", "/api")[i % 3]) for i in range(450))
        )
        statuses = Counter(r.status_code for r in responses)
        # 100/day for each of the three views (decorators and a DRF throttle).
        assert statuses == {200: 300, 429: 150}
        pids = {r.json()["pid"] for r in responses if r.status_code == 200}
        assert len(pids) >= 2  # admissions really came from several worker processes

        # The global rule (500/day) has absorbed 450 requests; /health is exempt.
        assert all(r.status_code == 200 for r in [await c.get("/health") for _ in range(3)])
        rest = await asyncio.gather(*(c.get("/missing") for _ in range(80)))
        assert Counter(r.status_code for r in rest) == {404: 50, 429: 30}
