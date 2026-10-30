from __future__ import annotations

import os

import pytest
from hypothesis import HealthCheck, settings

from unilimit import MemoryBackend
from unilimit.testing import ManualClock

# Deterministic by default (fixed seed); `HYPOTHESIS_PROFILE=explore` for deep fuzzing.
settings.register_profile(
    "default",
    derandomize=True,
    max_examples=300,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.register_profile("explore", max_examples=5_000, deadline=None)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "default"))


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock()


@pytest.fixture
def backend(clock: ManualClock) -> MemoryBackend:
    return MemoryBackend(clock=clock)
