"""Backend contract suite (ARCHITECTURE.md §6.2, §9.1).

Every test runs against every backend (memory, standalone Redis, Redis Cluster) through
both its sync and async methods.
"""

from collections.abc import AsyncIterator

import pytest

from unilimit.testing import ManualClock

from .drivers import BACKEND_KINDS, Driver, make_backend


@pytest.fixture(params=[(b, m) for b in BACKEND_KINDS for m in ("sync", "async")], ids=str)
async def driver(request: pytest.FixtureRequest) -> AsyncIterator[Driver]:
    kind, mode = request.param
    clock = ManualClock()
    d = Driver(make_backend(kind, clock, request), clock, mode)
    yield d
    await d.close()
