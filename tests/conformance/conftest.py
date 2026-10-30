"""Backend contract suite (ARCHITECTURE.md §6.2, §9.1).

Every test runs against each backend through both its sync and async methods. The
Redis backends join the parametrisation in Milestone 3.
"""

import pytest

from unilimit.testing import ManualClock

from .drivers import BACKENDS, Driver


@pytest.fixture(params=[(b, m) for b in BACKENDS for m in ("sync", "async")], ids=str)
def driver(request: pytest.FixtureRequest) -> Driver:
    name, mode = request.param
    clock = ManualClock()
    return Driver(BACKENDS[name](clock), clock, mode)
