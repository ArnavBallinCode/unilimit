"""Application-wide limiting through UniLimitMiddleware, and its interplay with
RateLimit dependencies."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import BackgroundTasks, Depends, FastAPI, Header
from fastapi.responses import PlainTextResponse, Response, StreamingResponse
from starlette.authentication import AuthCredentials, AuthenticationBackend, SimpleUser
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.requests import HTTPConnection

from unilimit import AccessList, Identity, Limiter, Rule, TrustedProxies
from unilimit.integrations.fastapi import UniLimitMiddleware, setup

from .conftest import LimiterFactory, client, ratelimit_headers

SITE = Rule("site", "3/minute", key="ip")


def app_with(limiter: Limiter, **setup_kwargs: Any) -> tuple[FastAPI, list[str]]:
    app = FastAPI()
    setup(app, limiter, **setup_kwargs)
    executed: list[str] = []

    @app.get("/x")
    async def x() -> dict[str, bool]:
        executed.append("x")
        return {"ok": True}

    @app.get("/direct")
    def direct() -> Response:
        executed.append("direct")
        return PlainTextResponse("direct")

    return app, executed


async def test_global_rules_apply_before_routing(make_limiter: LimiterFactory) -> None:
    app, executed = app_with(make_limiter(global_rules=[SITE]))
    async with client(app) as c:
        responses = [
            await c.get("/x"),
            await c.get("/missing"),  # 404s count too: scanning is limited
            await c.head("/x"),
            await c.get("/x"),
        ]
    assert [r.status_code for r in responses] == [200, 404, 405, 429]
    assert executed == ["x"]  # the rejected request never reached the endpoint
    assert ratelimit_headers(responses[1])["x-ratelimit-remaining"] == "1"
    rejected = responses[3]
    assert rejected.json() == {"detail": "Too Many Requests"}
    assert int(ratelimit_headers(rejected)["retry-after"]) >= 1


async def test_headers_reach_direct_streaming_and_background_responses(
    make_limiter: LimiterFactory,
) -> None:
    app, _ = app_with(make_limiter(global_rules=[Rule("site", "10/minute")]))
    background: list[str] = []

    @app.get("/stream")
    async def stream() -> StreamingResponse:
        async def chunks() -> Any:
            yield b"1"
            yield b"2"

        return StreamingResponse(chunks())

    @app.get("/bg")
    async def bg(tasks: BackgroundTasks) -> dict[str, bool]:
        tasks.add_task(background.append, "ran")
        return {"ok": True}

    async with client(app) as c:
        direct = await c.get("/direct")
        streamed = await c.get("/stream")
        with_bg = await c.get("/bg")
    assert ratelimit_headers(direct)["x-ratelimit-remaining"] == "9"
    assert streamed.content == b"12"
    assert ratelimit_headers(streamed)["x-ratelimit-remaining"] == "8"
    assert ratelimit_headers(with_bg)["x-ratelimit-remaining"] == "7"
    assert background == ["ran"]


async def test_no_rules_no_headers(make_limiter: LimiterFactory) -> None:
    app, _ = app_with(make_limiter())
    async with client(app) as c:
        assert ratelimit_headers(await c.get("/x")) == {}


async def test_exemptions_and_access_lists(make_limiter: LimiterFactory) -> None:
    limiter = make_limiter(
        global_rules=[Rule("site", "1/minute")],
        exempt_paths=["/health"],
        allow=AccessList(networks=["192.0.2.0/24"]),
        deny=AccessList(networks=["198.51.100.0/24"]),
    )
    app, executed = app_with(limiter)

    @app.get("/health")
    async def health() -> dict[str, bool]:
        return {"ok": True}

    async with client(app) as c:
        assert [(await c.get("/health")).status_code for _ in range(3)] == [200] * 3
    async with client(app, peer=("192.0.2.9", 1)) as c:
        assert [(await c.get("/x")).status_code for _ in range(3)] == [200] * 3
    async with client(app, peer=("198.51.100.9", 1)) as c:
        denied = await c.get("/x")
    assert denied.status_code == 403
    assert denied.json() == {"detail": "Forbidden"}
    assert executed == ["x"] * 3


async def test_trusted_proxies_in_middleware(make_limiter: LimiterFactory) -> None:
    limiter = make_limiter(
        global_rules=[Rule("site", "1/minute")],
        trusted_proxies=TrustedProxies(["10.0.0.0/8"]),
    )
    app, _ = app_with(limiter)
    async with client(app, peer=("10.0.0.2", 1)) as c:
        a = await c.get("/x", headers={"x-forwarded-for": "6.6.6.6, 198.51.100.1"})
        b = await c.get("/x", headers={"x-forwarded-for": "6.6.6.6, 198.51.100.2"})
        again = await c.get("/x", headers={"x-forwarded-for": "7.7.7.7, 198.51.100.1"})
    assert [a.status_code, b.status_code, again.status_code] == [200, 200, 429]


class HeaderAuth(AuthenticationBackend):
    async def authenticate(self, conn: HTTPConnection) -> Any:
        user = conn.headers.get("x-user")
        return (AuthCredentials(["authenticated"]), SimpleUser(user)) if user else None


async def test_global_user_rules_with_starlette_authentication(
    make_limiter: LimiterFactory,
) -> None:
    limiter = make_limiter(global_rules=[Rule("per-user", "1/minute", key="user_or_ip")])
    app, _ = app_with(limiter)
    app.add_middleware(AuthenticationMiddleware, backend=HeaderAuth())  # outer: runs first
    async with client(app) as c:
        assert (await c.get("/x", headers={"x-user": "alice"})).status_code == 200
        assert (await c.get("/x", headers={"x-user": "alice"})).status_code == 429
        assert (await c.get("/x", headers={"x-user": "bob"})).status_code == 200
        assert (await c.get("/x")).status_code == 200  # anonymous: falls back to the IP
    assert limiter.peek(limiter.global_rules[0], Identity("user", "alice")).remaining == 0


async def test_custom_user_resolver_for_global_rules(make_limiter: LimiterFactory) -> None:
    limiter = make_limiter(global_rules=[Rule("per-key", "1/minute", key="user")])
    app, _ = app_with(limiter, user=lambda conn: conn.headers.get("x-api-user"))
    async with client(app) as c:
        assert (await c.get("/x", headers={"x-api-user": "svc-1"})).status_code == 200
        assert (await c.get("/x", headers={"x-api-user": "svc-1"})).status_code == 429
        assert (await c.get("/x")).status_code == 403  # no identity: denied by default


# -- middleware and dependencies together ------------------------------------------------


async def test_rule_in_middleware_and_dependency_is_consumed_once(
    make_limiter: LimiterFactory,
) -> None:
    from unilimit.integrations.fastapi import RateLimit

    shared = Rule("shared", "5/minute")
    limiter = make_limiter(global_rules=[shared])
    app, _ = app_with(limiter)

    @app.get("/both", dependencies=[Depends(RateLimit(shared))])
    async def both() -> dict[str, bool]:
        return {"ok": True}

    async with client(app) as c:
        responses = [await c.get("/both") for _ in range(6)]
    assert [r.status_code for r in responses] == [200] * 5 + [429]
    assert [ratelimit_headers(r)["x-ratelimit-remaining"] for r in responses[:5]] == [
        "4",
        "3",
        "2",
        "1",
        "0",
    ]


async def test_same_rule_with_different_identities_counts_each(
    make_limiter: LimiterFactory,
) -> None:
    """A "user_or_ip" global rule keys on the IP in middleware (the user is unknown
    before routing) and on the user in a dependency: two different counters, so both
    are consumed. This is documented, not deduplicated."""
    from unilimit.integrations.fastapi import RateLimit

    rule = Rule("mixed", "5/minute", key="user_or_ip")
    limiter = make_limiter(global_rules=[rule])
    app, _ = app_with(limiter)

    def user(x_user: str | None = Header(None)) -> str | None:
        return x_user

    @app.get("/me", dependencies=[Depends(RateLimit(rule, identity=user))])
    async def me() -> dict[str, bool]:
        return {"ok": True}

    async with client(app) as c:
        await c.get("/me", headers={"x-user": "alice"})
    assert limiter.peek(rule, Identity("ip", "203.0.113.10")).remaining == 4
    assert limiter.peek(rule, Identity("user", "alice")).remaining == 4


async def test_overlapping_policies_precedence(make_limiter: LimiterFactory) -> None:
    """Global (middleware) -> route dependency. A later rejection does not refund the
    global step, and headers describe the most restrictive counter."""
    from unilimit.integrations.fastapi import RateLimit

    site = Rule("site", "10/minute")
    limiter = make_limiter(global_rules=[site])
    app, _ = app_with(limiter)

    @app.get("/strict", dependencies=[Depends(RateLimit("strict", rate="2/minute"))])
    async def strict() -> dict[str, bool]:
        return {"ok": True}

    async with client(app) as c:
        responses = [await c.get("/strict") for _ in range(3)]
        loose = await c.get("/x")
    assert [r.status_code for r in responses] == [200, 200, 429]
    assert ratelimit_headers(responses[0])["x-ratelimit-limit"] == "2"  # strictest binds
    assert ratelimit_headers(responses[2])["x-ratelimit-limit"] == "2"
    assert ratelimit_headers(loose)["x-ratelimit-limit"] == "10"
    # Three /strict requests and one /x passed the global step: 10 - 4 = 6.
    assert limiter.peek(site, Identity("ip", "203.0.113.10")).remaining == 6


async def test_handler_headers_are_not_duplicated(make_limiter: LimiterFactory) -> None:
    from unilimit.integrations.fastapi import RateLimit

    app, _ = app_with(make_limiter(global_rules=[Rule("site", "10/minute")]))

    @app.get("/r", dependencies=[Depends(RateLimit("r", rate="1/minute"))])
    async def r() -> dict[str, bool]:
        return {"ok": True}

    async with client(app) as c:
        await c.get("/r")
        rejected = await c.get("/r")
    assert rejected.status_code == 429
    assert len(rejected.headers.get_list("x-ratelimit-limit")) == 1
    assert rejected.headers["x-ratelimit-limit"] == "1"  # the rejecting rule, not "site"


async def test_non_http_scopes_pass_through() -> None:
    seen: list[str] = []

    async def inner(scope: Any, receive: Any, send: Any) -> None:
        seen.append(scope["type"])

    async def receive() -> Any:
        raise AssertionError("not called")

    async def send(message: Any) -> None:
        raise AssertionError("not called")

    middleware = UniLimitMiddleware(inner, limiter=Limiter.from_url("memory://"))
    await middleware({"type": "websocket", "path": "/ws"}, receive, send)
    assert seen == ["websocket"]


@pytest.mark.parametrize("fail_mode", ["open", "closed"])
async def test_backend_errors_respect_fail_mode_in_middleware(fail_mode: str) -> None:
    from unilimit import BackendUnavailable
    from unilimit.backends.memory import MemoryBackend

    class Down(MemoryBackend):
        async def aconsume(self, ops: Any) -> Any:
            raise BackendUnavailable("down")

    limiter = Limiter(Down(), global_rules=[SITE], fail_mode=fail_mode)  # type: ignore[arg-type]
    app, executed = app_with(limiter)
    async with client(app) as c:
        response = await c.get("/x")
    if fail_mode == "open":
        assert response.status_code == 200
        assert ratelimit_headers(response) == {}  # degraded: no made-up numbers
    else:
        assert response.status_code == 503
        assert response.json() == {"detail": "Service Unavailable"}
        assert executed == []


async def test_starlette_users_without_identity_use_display_name(
    make_limiter: LimiterFactory,
) -> None:
    # SimpleUser does not implement .identity; its display_name is used instead.
    limiter = make_limiter(global_rules=[Rule("per-user", "1/minute", key="user")])
    app, _ = app_with(limiter)
    app.add_middleware(AuthenticationMiddleware, backend=HeaderAuth())
    async with client(app) as c:
        assert (await c.get("/x", headers={"x-user": "carol"})).status_code == 200
    assert limiter.peek(limiter.global_rules[0], Identity("user", "carol")).remaining == 0


async def test_failing_close_on_shutdown_is_logged_not_raised(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    from unilimit.backends.memory import MemoryBackend

    from .conftest import lifespan

    class Broken(MemoryBackend):
        async def aclose(self) -> None:
            raise RuntimeError("close failed")

    app, _ = app_with(Limiter(Broken()))
    with caplog.at_level(logging.ERROR, logger="unilimit"):
        async with lifespan(app):
            pass
    assert "closing the rate limiter on shutdown failed" in caplog.text
