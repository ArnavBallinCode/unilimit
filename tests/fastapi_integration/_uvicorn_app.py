"""Application served by real uvicorn workers in the end-to-end tests."""

from __future__ import annotations

import os

from fastapi import Depends, FastAPI

from unilimit import Limiter, Rule
from unilimit.integrations.fastapi import RateLimit, setup

limiter = Limiter.from_url(
    os.environ["UNILIMIT_TEST_REDIS_URL"],
    cluster=os.environ.get("UNILIMIT_TEST_CLUSTER") == "1",
    global_rules=[Rule("site", "400/day", algorithm="fixed_window")],
    # "/" is exempt: local tooling (port scanners) may probe it and would spend the budget.
    exempt_paths=["/health", "/"],
)
app = FastAPI()
setup(app, limiter)


@app.get("/health")
async def health() -> dict[str, int]:
    return {"pid": os.getpid()}


@app.get("/limited", dependencies=[Depends(RateLimit("e2e", rate="100/day"))])
async def limited() -> dict[str, int]:
    return {"pid": os.getpid()}


@app.get("/sync-limited", dependencies=[Depends(RateLimit("e2e-sync", rate="100/day"))])
def sync_limited() -> dict[str, int]:
    return {"pid": os.getpid()}
