"""The FastAPI documentation is executable: every ```python block of each page runs, in
order, against a real Redis, and the behaviour the page describes is checked over HTTP."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from unilimit import Limiter

from .conftest import client, lifespan, ratelimit_headers

DOCS = Path(__file__).resolve().parents[2] / "docs" / "fastapi"
PAGES = sorted(p.name for p in DOCS.glob("*.md"))


def python_blocks(page: str) -> str:
    return "\n\n".join(re.findall(r"```python\n(.*?)```", (DOCS / page).read_text(), re.S))


@pytest.fixture
async def run_page(redis_url: str) -> AsyncIterator[Any]:
    namespaces: list[dict[str, Any]] = []

    def run(page: str) -> dict[str, Any]:
        code = python_blocks(page).replace("redis://localhost:6379/0", redis_url)
        namespace: dict[str, Any] = {"__name__": f"docs_{page.removesuffix('.md')}"}
        exec(compile(code, str(DOCS / page), "exec"), namespace)
        namespaces.append(namespace)
        return namespace

    yield run
    for namespace in namespaces:  # close everything the page created
        for value in list(namespace.values()):
            if isinstance(value, Limiter):
                await value.aclose()
                value.close()
        for name in ("async_client", "sync_client"):
            if name in namespace:
                closing = namespace[name].aclose() if name == "async_client" else None
                if closing is not None:
                    await closing
                else:
                    namespace[name].close()


def test_every_page_has_examples() -> None:
    assert set(PAGES) == {
        "authentication.md",
        "dependencies.md",
        "errors-and-headers.md",
        "identity.md",
        "middleware.md",
        "quickstart.md",
        "redis.md",
    }
    for page in PAGES:
        assert python_blocks(page), page


async def test_quickstart(run_page: Any) -> None:
    app = run_page("quickstart.md")["app"]
    async with lifespan(app), client(app) as c:
        searches = [await c.get("/search") for _ in range(11)]
        status = await c.get("/status")
    assert [r.status_code for r in searches] == [200] * 10 + [429]
    assert ratelimit_headers(searches[0])["x-ratelimit-limit"] == "10"
    assert "retry-after" in ratelimit_headers(searches[-1])
    assert searches[-1].json() == {"detail": "Too Many Requests"}
    assert ratelimit_headers(status)["x-ratelimit-limit"] == "100"  # the global "site" rule


async def test_middleware_page(run_page: Any) -> None:
    app = run_page("middleware.md")["app"]
    async with lifespan(app), client(app) as c:
        health = [await c.get("/health") for _ in range(30)]
        feed = await c.get("/feed")
    assert all(r.status_code == 200 and not ratelimit_headers(r) for r in health)
    assert ratelimit_headers(feed)["x-ratelimit-limit"] == "40"  # burst=40 binds first
    async with client(app, peer=("198.51.100.4", 1)) as c:
        assert (await c.get("/feed")).status_code == 403  # deny list
    async with client(app, peer=("10.1.2.3", 1)) as c:
        first = await c.get("/feed", headers={"x-forwarded-for": "192.0.2.1"})
        second = await c.get("/feed", headers={"x-forwarded-for": "192.0.2.2"})
    assert (
        ratelimit_headers(first)["x-ratelimit-remaining"]
        == ratelimit_headers(second)["x-ratelimit-remaining"]
    )  # different clients behind the trusted proxy: separate counters


async def test_dependencies_page(run_page: Any) -> None:
    app = run_page("dependencies.md")["app"]
    async with lifespan(app), client(app) as c:
        assert ratelimit_headers(await c.get("/search"))["x-ratelimit-limit"] == "10"
        assert ratelimit_headers(await c.get("/catalog"))["x-ratelimit-limit"] == "100"
        daily = await c.get("/reports/daily")
        weekly = await c.get("/reports/weekly")
        assert ratelimit_headers(daily)["x-ratelimit-remaining"] == "4"
        assert ratelimit_headers(weekly)["x-ratelimit-remaining"] == "3"  # shared counter
        exports = [(await c.post("/export")).status_code for _ in range(3)]
        assert exports == [200, 200, 429]
        assert (await c.get("/quota")).json() == {"remaining": 29}
        users = await c.get("/admin/users")
        audit = await c.get("/admin/audit")
        assert ratelimit_headers(users)["x-ratelimit-remaining"] == "59"
        assert ratelimit_headers(audit)["x-ratelimit-remaining"] == "59"  # per route
        logins = [(await c.post("/login")).status_code for _ in range(6)]
        assert logins == [200] * 5 + [429]


async def test_authentication_page(run_page: Any) -> None:
    app = run_page("authentication.md")["app"]
    alice = {"authorization": "Bearer token-alice"}
    bob = {"authorization": "Bearer token-bob"}
    async with lifespan(app), client(app) as c:
        assert (
            await c.get("/profile", headers={"authorization": "Bearer nope"})
        ).status_code == 401
        ok = await c.get("/profile", headers=alice)
        assert ok.json() == {"user_id": 1}
        assert ratelimit_headers(ok)["x-ratelimit-limit"] == "30"
        anonymous = [await c.get("/articles") for _ in range(2)]
        signed_in = await c.get("/articles", headers=alice)
        assert ratelimit_headers(anonymous[1])["x-ratelimit-remaining"] == "58"
        assert ratelimit_headers(signed_in)["x-ratelimit-remaining"] == "59"  # own counter
        pro = await c.get("/api/data", headers=alice)
        free = await c.get("/api/data", headers=bob)
        assert ratelimit_headers(pro)["x-ratelimit-limit"] == "1000"
        assert ratelimit_headers(free)["x-ratelimit-limit"] == "10"


async def test_identity_page(run_page: Any) -> None:
    app = run_page("identity.md")["app"]
    async with lifespan(app), client(app) as c:
        keyed = await c.get("/partner/orders", headers={"x-api-key": "k1"})
        other_key = await c.get("/partner/orders", headers={"x-api-key": "k2"})
        assert ratelimit_headers(keyed)["x-ratelimit-remaining"] == "599"
        assert ratelimit_headers(other_key)["x-ratelimit-remaining"] == "599"
        no_tenant = await c.get("/tenant/usage")
        assert no_tenant.status_code == 403  # no identity -> denied, never unlimited
        tenant = await c.get("/tenant/usage", headers={"x-tenant": "acme"})
        assert ratelimit_headers(tenant)["x-ratelimit-limit"] == "1000"
        acme = [await c.get("/orgs/acme/reports") for _ in range(2)]
        globex = await c.get("/orgs/globex/reports")
        assert ratelimit_headers(acme[1])["x-ratelimit-remaining"] == "18"
        assert ratelimit_headers(globex)["x-ratelimit-remaining"] == "19"
        exports = [(await c.get("/orgs/acme/exports")).status_code for _ in range(6)]
        assert exports == [200] * 5 + [429]


async def test_redis_page(run_page: Any) -> None:
    namespace = run_page("redis.md")
    app = namespace["app"]
    async with lifespan(app), client(app) as c:
        items = await c.get("/items")
    assert ratelimit_headers(items)["x-ratelimit-limit"] == "60"
    for shared in (namespace["public_api"], namespace["admin_api"]):
        async with lifespan(shared):
            pass
    own = namespace["own_clients"]
    from unilimit import Rule

    assert (await own.aconsume(Rule("doc", "1/minute"), "x")).allowed


async def test_errors_page(run_page: Any) -> None:
    app = run_page("errors-and-headers.md")["app"]
    async with lifespan(app), client(app) as c:
        assert (await c.get("/x")).status_code == 200
        rejected = await c.get("/x")
    assert rejected.status_code == 429
    assert rejected.json() == {"error": "rate_limited"}  # the page's own handler
    assert "retry-after" in ratelimit_headers(rejected)
