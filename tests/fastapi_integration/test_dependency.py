"""Endpoint-level limiting through the RateLimit dependency, over real ASGI requests."""

from __future__ import annotations

import logging
from typing import Any

import pytest
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse

from unilimit import (
    ConfigurationError,
    Decision,
    Identity,
    IdentityError,
    Limiter,
    Rule,
    TrustedProxies,
    UnknownRuleError,
)
from unilimit.http import ResponseConfig
from unilimit.integrations.fastapi import RateLimit, setup

from .conftest import LimiterFactory, client, ratelimit_headers

# -- basics ------------------------------------------------------------------------------


@pytest.mark.parametrize("endpoint_kind", ["async", "sync"])
async def test_limits_and_headers(make_limiter: LimiterFactory, endpoint_kind: str) -> None:
    app = FastAPI()
    setup(app, make_limiter())
    limit = RateLimit("items", rate="2/minute")
    if endpoint_kind == "async":

        @app.get("/items", dependencies=[Depends(limit)])
        async def items_async() -> dict[str, bool]:
            return {"ok": True}
    else:

        @app.get("/items", dependencies=[Depends(limit)])
        def items_sync() -> dict[str, bool]:
            return {"ok": True}

    async with client(app) as c:
        first, second, third = [await c.get("/items") for _ in range(3)]
    assert [first.status_code, second.status_code, third.status_code] == [200, 200, 429]
    assert ratelimit_headers(first)["x-ratelimit-limit"] == "2"
    assert ratelimit_headers(first)["x-ratelimit-remaining"] == "1"
    assert ratelimit_headers(second)["x-ratelimit-remaining"] == "0"
    assert "retry-after" not in ratelimit_headers(second)
    rejected = ratelimit_headers(third)
    assert rejected["x-ratelimit-remaining"] == "0"
    assert 1 <= int(rejected["retry-after"]) <= 120
    assert third.json() == {"detail": "Too Many Requests"}


RETURN_KINDS = ["dict", "model", "json", "plain", "response", "streaming", "status_204"]


def _endpoint(kind: str) -> Any:
    async def endpoint() -> Any:
        if kind == "dict":
            return {"ok": True}
        if kind == "model":
            return {"ok": True, "extra": "dropped"}
        if kind == "json":
            return JSONResponse({"ok": True}, headers={"X-Own": "1"})
        if kind == "plain":
            return PlainTextResponse("ok")
        if kind == "response":
            return Response(content=b"raw", media_type="application/octet-stream")
        if kind == "status_204":
            return Response(status_code=204)

        async def chunks() -> Any:
            yield b"a"
            yield b"b"

        return StreamingResponse(chunks())

    return endpoint


@pytest.mark.parametrize("kind", RETURN_KINDS)
async def test_headers_reach_every_response_type(make_limiter: LimiterFactory, kind: str) -> None:
    app = FastAPI()
    setup(app, make_limiter())
    app.get("/r", dependencies=[Depends(RateLimit("r", rate="5/minute"))])(_endpoint(kind))
    async with client(app) as c:
        response = await c.get("/r")
    assert response.status_code in (200, 204)
    assert ratelimit_headers(response) == {
        "x-ratelimit-limit": "5",
        "x-ratelimit-remaining": "4",
        "x-ratelimit-reset": ratelimit_headers(response)["x-ratelimit-reset"],
    }
    if kind == "json":
        assert response.headers["x-own"] == "1"  # endpoint headers are kept
    if kind == "streaming":
        assert response.content == b"ab"


async def test_without_middleware_direct_responses_lose_headers() -> None:
    """Documented limitation: without setup(), headers are added through FastAPI's
    injected Response, which FastAPI ignores when an endpoint returns its own."""
    limiter = Limiter.from_url("memory://")
    app = FastAPI()
    limit = RateLimit("r", rate="5/minute", limiter=limiter)

    @app.get("/dict", dependencies=[Depends(limit)])
    async def as_dict() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/direct", dependencies=[Depends(limit)])
    async def direct() -> Response:
        return PlainTextResponse("raw")

    async with client(app) as c:
        assert ratelimit_headers(await c.get("/dict"))["x-ratelimit-remaining"] == "4"
        assert ratelimit_headers(await c.get("/direct")) == {}


async def test_without_setup_rejections_still_work_with_fastapi_defaults(
    caplog: pytest.LogCaptureFixture,
) -> None:
    limiter = Limiter.from_url("memory://", global_rules=[Rule("g", "100/minute")])
    app = FastAPI()

    @app.get("/x", dependencies=[Depends(RateLimit("x", rate="1/minute", limiter=limiter))])
    async def x() -> dict[str, bool]:
        return {"ok": True}

    with caplog.at_level(logging.WARNING, logger="unilimit"):
        async with client(app) as c:
            assert (await c.get("/x")).status_code == 200
            rejected = await c.get("/x")
    assert rejected.status_code == 429
    assert rejected.json() == {"detail": "Too Many Requests"}
    assert "retry-after" in ratelimit_headers(rejected)
    assert "UniLimitMiddleware is not installed" in caplog.text


async def test_missing_limiter_is_a_configuration_error() -> None:
    app = FastAPI()

    @app.get("/x", dependencies=[Depends(RateLimit("x", rate="1/minute"))])
    async def x() -> None: ...

    async with client(app) as c:
        with pytest.raises(ConfigurationError, match="setup"):
            await c.get("/x")


# -- identity ----------------------------------------------------------------------------


@pytest.mark.parametrize("dependency_kind", ["async", "sync"])
async def test_identity_dependency_resolves_first_and_runs_once(
    make_limiter: LimiterFactory, dependency_kind: str
) -> None:
    calls: list[str | None] = []

    async def current_user_async(x_user: str | None = Header(None)) -> str | None:
        calls.append(x_user)
        return x_user

    def current_user_sync(x_user: str | None = Header(None)) -> str | None:
        calls.append(x_user)
        return x_user

    current_user: Any = current_user_async if dependency_kind == "async" else current_user_sync

    limiter = make_limiter(on_missing="deny")
    app = FastAPI()
    setup(app, limiter)

    @app.get("/me")
    async def me(
        decision: Decision = Depends(RateLimit("me", rate="2/minute", identity=current_user)),
        user: str | None = Depends(current_user),
    ) -> dict[str, Any]:
        return {"user": user, "remaining": decision.results[0].remaining}

    async with client(app) as c:
        alice = [await c.get("/me", headers={"x-user": "alice"}) for _ in range(3)]
        bob = await c.get("/me", headers={"x-user": "bob"})
        anonymous = await c.get("/me")
    assert [r.status_code for r in alice] == [200, 200, 429]
    assert alice[0].json() == {"user": "alice", "remaining": 1}
    assert bob.status_code == 200  # separate counter per user
    assert anonymous.status_code == 403  # on_missing="deny": never silently unlimited
    assert len(calls) == 5  # once per request, shared with the endpoint
    assert limiter.peek(limiter_rule(limiter, "me"), Identity("user", "alice")).remaining == 0


def limiter_rule(limiter: Limiter, name: str) -> Rule:
    return Rule(name, "2/minute", key="user")


async def test_identity_dependency_failure_blocks_before_limiting(
    make_limiter: LimiterFactory,
) -> None:
    def require_user(authorization: str | None = Header(None)) -> int:
        if authorization != "Bearer good":
            raise HTTPException(status_code=401, detail="unauthenticated")
        return 42

    limiter = make_limiter()
    app = FastAPI()
    setup(app, limiter)
    rule_dep = RateLimit("api", rate="1/minute", identity=require_user)

    @app.get("/api", dependencies=[Depends(rule_dep)])
    async def api() -> dict[str, bool]:
        return {"ok": True}

    async with client(app) as c:
        assert (await c.get("/api")).status_code == 401
        assert (await c.get("/api", headers={"authorization": "Bearer good"})).status_code == 200
        assert (await c.get("/api", headers={"authorization": "Bearer good"})).status_code == 429
    remaining = limiter.peek(Rule("api", "1/minute", key="user"), Identity("user", "42"))
    assert remaining.remaining == 0  # integer user ids are supported


@pytest.mark.parametrize(
    ("on_missing", "expected"), [("deny", 403), ("skip", 200), ("raise", "error")]
)
async def test_missing_identity_policies(
    make_limiter: LimiterFactory, on_missing: str, expected: object
) -> None:
    def nobody() -> None:
        return None

    app = FastAPI()
    setup(app, make_limiter())
    dep = RateLimit("p", rate="1/minute", identity=nobody, on_missing=on_missing)  # type: ignore[arg-type]

    @app.get("/p", dependencies=[Depends(dep)])
    async def p() -> dict[str, bool]:
        return {"ok": True}

    async with client(app) as c:
        if expected == "error":
            with pytest.raises(IdentityError):
                await c.get("/p")
        else:
            response = await c.get("/p")
            assert response.status_code == expected
            if expected == 200:
                assert ratelimit_headers(response) == {}


async def test_fallback_chain_user_then_trusted_ip(make_limiter: LimiterFactory) -> None:
    def maybe_user(x_user: str | None = Header(None)) -> str | None:
        return x_user

    limiter = make_limiter(trusted_proxies=TrustedProxies(["10.0.0.0/8"]))
    app = FastAPI()
    setup(app, limiter)
    dep = RateLimit("api", rate="1/minute", identity=maybe_user, key="user_or_ip")

    @app.get("/api", dependencies=[Depends(dep)])
    async def api() -> dict[str, bool]:
        return {"ok": True}

    via_lb = {"x-forwarded-for": "198.51.100.7"}
    async with client(app, peer=("10.0.0.2", 1234)) as c:
        assert (await c.get("/api", headers=via_lb)).status_code == 200  # anonymous -> IP
        assert (await c.get("/api", headers=via_lb)).status_code == 429
        other_ip = {"x-forwarded-for": "198.51.100.8"}
        assert (await c.get("/api", headers=other_ip)).status_code == 200
        user = {**via_lb, "x-user": "alice"}
        assert (await c.get("/api", headers=user)).status_code == 200  # user bucket
        assert (await c.get("/api", headers=user)).status_code == 429


async def test_custom_identity_function_sees_the_request(make_limiter: LimiterFactory) -> None:
    app = FastAPI()
    setup(app, make_limiter())
    per_org = RateLimit("org", rate="1/minute", key=lambda ctx: ctx.raw.path_params["org"])

    @app.get("/orgs/{org}/reports", dependencies=[Depends(per_org)])
    async def reports(org: str) -> dict[str, str]:
        return {"org": org}

    async with client(app) as c:
        assert (await c.get("/orgs/acme/reports")).status_code == 200
        assert (await c.get("/orgs/acme/reports")).status_code == 429
        assert (await c.get("/orgs/globex/reports")).status_code == 200


async def test_untrusted_forwarded_headers_are_ignored(make_limiter: LimiterFactory) -> None:
    app = FastAPI()
    setup(app, make_limiter(trusted_proxies=TrustedProxies(["10.0.0.0/8"])))

    @app.get("/x", dependencies=[Depends(RateLimit("x", rate="1/minute"))])
    async def x() -> dict[str, bool]:
        return {"ok": True}

    async with client(app, peer=("203.0.113.50", 1)) as c:
        assert (await c.get("/x", headers={"x-forwarded-for": "1.1.1.1"})).status_code == 200
        # Rotating a spoofed header does not grant a fresh bucket.
        assert (await c.get("/x", headers={"x-forwarded-for": "2.2.2.2"})).status_code == 429


# -- named, shared and per-route rules ---------------------------------------------------


async def test_named_rules_and_shared_counters(make_limiter: LimiterFactory) -> None:
    search = Rule("search", "2/minute")
    limiter = make_limiter(rules=[search])
    app = FastAPI()
    setup(app, limiter)

    @app.get("/a", dependencies=[Depends(RateLimit("search"))])
    async def a() -> dict[str, str]:
        return {"r": "a"}

    @app.get("/b", dependencies=[Depends(RateLimit(search))])
    async def b() -> dict[str, str]:
        return {"r": "b"}

    async with client(app) as c:
        assert (await c.get("/a")).status_code == 200
        assert (await c.get("/b")).status_code == 200
        assert (await c.get("/a")).status_code == 429  # one counter for both routes


async def test_per_route_counters(make_limiter: LimiterFactory) -> None:
    app = FastAPI()
    setup(app, make_limiter())
    router = APIRouter(dependencies=[Depends(RateLimit("api", rate="1/minute", per_route=True))])

    @router.get("/a/{item}")
    async def a(item: int) -> dict[str, int]:
        return {"item": item}

    @router.get("/b")
    async def b() -> dict[str, str]:
        return {"r": "b"}

    app.include_router(router)
    async with client(app) as c:
        assert (await c.get("/a/1")).status_code == 200
        assert (await c.get("/a/2")).status_code == 429  # same route template
        assert (await c.get("/b")).status_code == 200  # a different route


async def test_unknown_rule_names(make_limiter: LimiterFactory) -> None:
    limiter = make_limiter()
    with pytest.raises(UnknownRuleError):
        RateLimit("nope", limiter=limiter)
    app = FastAPI()
    setup(app, limiter)

    @app.get("/x", dependencies=[Depends(RateLimit("nope"))])
    async def x() -> None: ...

    async with client(app) as c:
        with pytest.raises(UnknownRuleError):
            await c.get("/x")


# -- multiple rules and precedence -------------------------------------------------------


async def test_rules_in_one_dependency_are_atomic(make_limiter: LimiterFactory) -> None:
    burst = Rule("burst", "2/minute", key="ip", algorithm="fixed_window")
    daily = Rule("daily", "10/day", key="ip", algorithm="fixed_window")
    limiter = make_limiter()
    app = FastAPI()
    setup(app, limiter)

    @app.get("/x", dependencies=[Depends(RateLimit(burst, daily))])
    async def x() -> dict[str, bool]:
        return {"ok": True}

    async with client(app) as c:
        statuses = [(await c.get("/x")).status_code for _ in range(8)]
    assert statuses == [200, 200] + [429] * 6
    assert limiter.peek(daily, Identity("ip", "203.0.113.10")).remaining == 8


async def test_router_app_and_endpoint_dependencies(make_limiter: LimiterFactory) -> None:
    order: list[str] = []

    def tracking(tag: str, rate: str) -> Any:
        dep = RateLimit(tag, rate=rate)

        async def wrapper(request: Request) -> Decision:
            order.append(tag)
            return await dep(request=request, response=Response())

        return wrapper

    app = FastAPI(dependencies=[Depends(tracking("app", "100/minute"))])
    setup(app, make_limiter())
    router = APIRouter(dependencies=[Depends(tracking("router", "50/minute"))])

    @router.get("/x", dependencies=[Depends(tracking("route", "3/minute"))])
    async def x(d: Decision = Depends(tracking("param", "20/minute"))) -> dict[str, bool]:
        return {"ok": True}

    app.include_router(router)
    async with client(app) as c:
        response = await c.get("/x")
    assert order == ["app", "router", "route", "param"]
    # Headers describe the most restrictive counter across all dependencies.
    assert ratelimit_headers(response)["x-ratelimit-limit"] == "3"
    assert ratelimit_headers(response)["x-ratelimit-remaining"] == "2"


async def test_same_rule_in_several_dependencies_is_consumed_once(
    make_limiter: LimiterFactory,
) -> None:
    shared = Rule("shared", "10/minute")
    limiter = make_limiter()
    app = FastAPI()
    setup(app, limiter)
    router = APIRouter(dependencies=[Depends(RateLimit(shared))])

    @router.get("/x", dependencies=[Depends(RateLimit(shared))])
    async def x(_: Decision = Depends(RateLimit(shared))) -> dict[str, bool]:
        return {"ok": True}

    app.include_router(router)
    async with client(app) as c:
        await c.get("/x")
        response = await c.get("/x")
    assert ratelimit_headers(response)["x-ratelimit-remaining"] == "8"  # 1 per request
    assert limiter.peek(shared, Identity("ip", "203.0.113.10")).remaining == 8


async def test_shadow_rules_never_block(make_limiter: LimiterFactory) -> None:
    app = FastAPI()
    setup(app, make_limiter())

    @app.get("/x", dependencies=[Depends(RateLimit("trial", rate="1/minute", action="log"))])
    async def x() -> dict[str, bool]:
        return {"ok": True}

    async with client(app) as c:
        statuses = [(await c.get("/x")).status_code for _ in range(3)]
    assert statuses == [200, 200, 200]


async def test_weighted_cost_from_the_request(make_limiter: LimiterFactory) -> None:
    app = FastAPI()
    setup(app, make_limiter())
    batch = RateLimit(
        "batch",
        rate="10/minute",
        algorithm="fixed_window",
        cost=lambda ctx: int(ctx.headers.get("x-batch", "1")),
    )

    @app.post("/batch", dependencies=[Depends(batch)])
    async def post_batch() -> dict[str, bool]:
        return {"ok": True}

    async with client(app) as c:
        first = await c.post("/batch", headers={"x-batch": "7"})
        second = await c.post("/batch", headers={"x-batch": "4"})
        third = await c.post("/batch", headers={"x-batch": "3"})
    assert ratelimit_headers(first)["x-ratelimit-remaining"] == "3"
    assert second.status_code == 429  # 7 + 4 > 10, and nothing was consumed
    assert third.status_code == 200


async def test_custom_response_config(make_limiter: LimiterFactory) -> None:
    app = FastAPI()
    setup(
        app,
        make_limiter(),
        response=ResponseConfig(
            body=lambda d: {"error": "slow_down", "retry_after": d.retry_after},
            ietf_headers=True,
        ),
    )

    @app.get("/x", dependencies=[Depends(RateLimit("x", rate="1/minute"))])
    async def x() -> dict[str, bool]:
        return {"ok": True}

    async with client(app) as c:
        ok = await c.get("/x")
        rejected = await c.get("/x")
    assert ratelimit_headers(ok)["ratelimit-policy"] == '"x";q=1;w=60'
    assert rejected.status_code == 429
    assert rejected.json()["error"] == "slow_down"
    assert rejected.json()["retry_after"] > 0


def test_constructor_validation() -> None:
    with pytest.raises(ConfigurationError, match="at least one rule"):
        RateLimit()
    with pytest.raises(ConfigurationError, match="exactly one name"):
        RateLimit("a", "b", rate="1/s")
    with pytest.raises(ConfigurationError, match="inline rule"):
        RateLimit("a", key="user")
    with pytest.raises(ConfigurationError, match="Rule objects or rule names"):
        RateLimit(42)  # type: ignore[arg-type]
    with pytest.raises(ConfigurationError, match="callable"):
        RateLimit("a", rate="1/s", identity="user")  # type: ignore[arg-type]
    assert repr(RateLimit("a", rate="1/s")) == "RateLimit(a)"


async def test_per_route_names_stay_bounded_for_long_paths(make_limiter: LimiterFactory) -> None:
    app = FastAPI()
    setup(app, make_limiter())
    path = "/" + "/".join(f"segment{i}" for i in range(40))

    @app.get(path, dependencies=[Depends(RateLimit("long", rate="1/minute", per_route=True))])
    async def long_route() -> dict[str, bool]:
        return {"ok": True}

    async with client(app) as c:
        assert (await c.get(path)).status_code == 200
        assert (await c.get(path)).status_code == 429
