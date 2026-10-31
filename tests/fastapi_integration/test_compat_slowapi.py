"""The SlowAPI adapter (``unilimit.compat.slowapi``) in real FastAPI and Starlette apps."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from unilimit import ConfigurationError, Identity, Rule
from unilimit.compat.slowapi import (
    Limiter,
    RateLimitExceeded,
    SlowAPIASGIMiddleware,
    SlowAPIMiddleware,
    _rate_limit_exceeded_handler,
    get_remote_address,
)
from unilimit.testing import ManualClock

from .conftest import LimiterFactory, client, ratelimit_headers

IP = Identity("ip", "203.0.113.10")


def memory_limiter(**kwargs: Any) -> Limiter:
    return Limiter(clock=ManualClock(), **kwargs)


def slowapi_app(limiter: Limiter, *, middleware: bool = False) -> FastAPI:
    """The setup SlowAPI documents."""
    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    if middleware:
        app.add_middleware(SlowAPIMiddleware)
    return app


def name_of(fn: Any, rate: str) -> str:
    return f"{fn.__module__}.{fn.__qualname__}:{rate}"


async def statuses(app: Any, path: str, n: int, **kwargs: Any) -> list[int]:
    async with client(app) as c:
        return [(await c.get(path, **kwargs)).status_code for _ in range(n)]


async def test_typical_app_on_every_backend(make_limiter: LimiterFactory) -> None:
    limiter = Limiter(key_func=get_remote_address, limiter=make_limiter())
    app = slowapi_app(limiter)

    @app.get("/home")
    @limiter.limit("2/minute")
    async def home(request: Request) -> dict[str, str]:
        return {"ok": "yes"}

    @app.get("/sync")
    @limiter.limit("1/minute")
    def sync(request: Request) -> dict[str, str]:
        return {"ok": "yes"}

    async with client(app) as c:
        responses = [await c.get("/home") for _ in range(3)]
        assert [r.status_code for r in responses] == [200, 200, 429]
        assert responses[2].json() == {"error": "Rate limit exceeded: 2 per 1 minute"}
        assert ratelimit_headers(responses[2])["retry-after"]
        assert "x-ratelimit-limit" not in ratelimit_headers(responses[0])  # off by default
        assert [(await c.get("/sync")).status_code for _ in range(2)] == [200, 429]


async def test_stacked_and_multi_rate_limits_are_one_atomic_batch(
    make_limiter: LimiterFactory,
) -> None:
    core = make_limiter()
    limiter = Limiter(limiter=core)
    app = slowapi_app(limiter)

    @app.get("/")
    @limiter.limit("2/minute")
    @limiter.limit("5/hour;100/day")
    async def home(request: Request) -> dict[str, str]:
        return {}

    assert await statuses(app, "/", 4) == [200, 200, 429, 429]
    # The rejected requests consumed nothing from the other limits.
    assert core.peek(Rule(name_of(home, "5/1h"), "5/hour"), IP).remaining == 3
    assert core.peek(Rule(name_of(home, "100/1d"), "100/day"), IP).remaining == 98


async def test_key_functions() -> None:
    limiter = memory_limiter()
    app = slowapi_app(limiter)

    @app.get("/by-header")
    @limiter.limit("1/minute", key_func=lambda r: r.headers.get("x-user", ""))
    async def by_header(request: Request) -> dict[str, str]:
        return {}

    @app.get("/constant")
    @limiter.limit("1/minute", key_func=lambda: "everyone")
    async def constant(request: Request) -> dict[str, str]:
        return {}

    async with client(app) as c:
        a = [(await c.get("/by-header", headers={"x-user": "a"})).status_code for _ in range(2)]
        b = (await c.get("/by-header", headers={"x-user": "b"})).status_code
        missing = await c.get("/by-header")  # an empty key is never a free pass
        assert (a, b, missing.status_code) == ([200, 429], 200, 403)
        assert missing.json() == {"error": "Forbidden"}
        assert (await c.get("/constant")).status_code == 200
    async with client(app, peer=("198.51.100.7", 5000)) as other:
        assert (await other.get("/constant")).status_code == 429


async def test_default_and_application_limits_need_the_middleware() -> None:
    limiter = memory_limiter(default_limits=["2/minute"], application_limits=["7/minute"])
    app = slowapi_app(limiter, middleware=True)

    @app.get("/a")
    async def a() -> dict[str, str]:
        return {}

    @app.get("/b")
    def b() -> dict[str, str]:
        return {}

    @app.get("/decorated")
    @limiter.limit("10/minute")
    async def decorated(request: Request) -> dict[str, str]:
        return {}

    @app.get("/exempt")
    @limiter.exempt
    async def exempt() -> dict[str, str]:
        return {}

    async with client(app) as c:
        a_codes = [(await c.get("/a")).status_code for _ in range(3)]
        rejected = await c.get("/a")
        b_codes = [(await c.get("/b")).status_code for _ in range(2)]  # own default counter
        # Decorated endpoints skip the defaults; the application limit is shared.
        decorated_codes = [(await c.get("/decorated")).status_code for _ in range(4)]
        exempt_codes = [(await c.get("/exempt")).status_code for _ in range(3)]
    assert a_codes == [200, 200, 429]
    assert rejected.json() == {"error": "Rate limit exceeded: 2 per 1 minute"}
    assert b_codes == [200, 200]
    assert decorated_codes == [200, 200, 200, 429]  # 7 admitted in total: 2 + 2 + 3
    assert exempt_codes == [200, 200, 200]


async def test_override_defaults_false_adds_the_defaults() -> None:
    limiter = memory_limiter(default_limits=["1/minute"])
    app = slowapi_app(limiter, middleware=True)

    @app.get("/")
    @limiter.limit("10/minute", override_defaults=False)
    async def home(request: Request) -> dict[str, str]:
        return {}

    assert await statuses(app, "/", 2) == [200, 429]


async def test_middleware_without_limiter_state_fails_loudly() -> None:
    app = FastAPI()
    app.add_middleware(SlowAPIASGIMiddleware)

    @app.get("/")
    async def home() -> dict[str, str]:
        return {}

    with pytest.raises(ConfigurationError, match=r"app\.state\.limiter"):
        await statuses(app, "/", 1)


async def test_shared_limit() -> None:
    limiter = memory_limiter()
    app = slowapi_app(limiter)

    @app.get("/one")
    @limiter.shared_limit("2/minute", scope="reports")
    async def one(request: Request) -> dict[str, str]:
        return {}

    @app.get("/two")
    @limiter.shared_limit("2/minute", scope="reports")
    async def two(request: Request) -> dict[str, str]:
        return {}

    async with client(app) as c:
        codes = [(await c.get(path)).status_code for path in ("/one", "/two", "/one")]
    assert codes == [200, 200, 429]


async def test_limit_options() -> None:
    limiter = memory_limiter()
    app = slowapi_app(limiter)

    @app.api_route("/methods", methods=["GET", "POST"])
    @limiter.limit("1/minute", methods=["post"])
    async def methods(request: Request) -> dict[str, str]:
        return {}

    @app.api_route("/per-method", methods=["GET", "POST"])
    @limiter.limit("1/minute", per_method=True)
    async def per_method(request: Request) -> dict[str, str]:
        return {}

    @app.get("/exempt-when")
    @limiter.limit("1/minute", exempt_when=lambda r: r.headers.get("x-admin") == "1")
    async def exempt_when(request: Request) -> dict[str, str]:
        return {}

    @app.get("/cost")
    @limiter.limit("5/minute", cost=lambda r: int(r.query_params["n"]))
    async def cost(request: Request) -> dict[str, str]:
        return {}

    @app.get("/message")
    @limiter.limit("1/minute", error_message="slow down")
    async def message(request: Request) -> dict[str, str]:
        return {}

    async with client(app) as c:
        assert [(await c.get("/methods")).status_code for _ in range(3)] == [200] * 3
        assert [(await c.post("/methods")).status_code for _ in range(2)] == [200, 429]
        assert (await c.get("/per-method")).status_code == 200
        assert (await c.post("/per-method")).status_code == 200  # its own counter
        assert (await c.get("/per-method")).status_code == 429
        admin = {"x-admin": "1"}
        assert [(await c.get("/exempt-when", headers=admin)).status_code for _ in range(3)] == [
            200
        ] * 3
        assert [(await c.get("/exempt-when")).status_code for _ in range(2)] == [200, 429]
        assert [(await c.get(f"/cost?n={n}")).status_code for n in (3, 3, 2)] == [200, 429, 200]
        assert [(await c.get("/message")).status_code for _ in range(2)] == [200, 429]
        assert (await c.get("/message")).json() == {"error": "Rate limit exceeded: slow down"}


async def test_dynamic_limit_values() -> None:
    limiter = memory_limiter(key_func=lambda r: r.headers["x-plan"])
    app = slowapi_app(limiter)

    @app.get("/")
    @limiter.limit(lambda key: "3/minute" if key == "gold" else "1/minute")
    async def home(request: Request) -> dict[str, str]:
        return {}

    async with client(app) as c:
        gold = [(await c.get("/", headers={"x-plan": "gold"})).status_code for _ in range(4)]
        free = [(await c.get("/", headers={"x-plan": "free"})).status_code for _ in range(2)]
    assert (gold, free) == ([200, 200, 200, 429], [200, 429])


async def test_headers_enabled() -> None:
    limiter = memory_limiter(headers_enabled=True)
    app = slowapi_app(limiter)

    @app.get("/response")
    @limiter.limit("5/minute")
    async def returns_response(request: Request) -> Response:
        return JSONResponse({})

    @app.get("/param")
    @limiter.limit("5/minute")
    def takes_response(request: Request, response: Response) -> dict[str, str]:
        return {}

    @app.get("/neither")
    @limiter.limit("5/minute")
    async def neither(request: Request) -> dict[str, str]:
        return {}

    async with client(app) as c:
        for path in ("/response", "/param"):
            headers = ratelimit_headers(await c.get(path))
            assert (headers["x-ratelimit-limit"], headers["x-ratelimit-remaining"]) == ("5", "4")
        with pytest.raises(TypeError, match="response"):
            await c.get("/neither")


async def test_headers_from_the_middleware() -> None:
    limiter = memory_limiter(default_limits=["3/minute"], headers_enabled=True)
    app = slowapi_app(limiter, middleware=True)

    @app.get("/")
    async def home() -> dict[str, str]:
        return {}

    async with client(app) as c:
        headers = ratelimit_headers(await c.get("/"))
    assert headers["x-ratelimit-remaining"] == "2"


async def test_enabled_false_disables_limits() -> None:
    limiter = memory_limiter(default_limits=["1/minute"])
    limiter.enabled = False
    app = slowapi_app(limiter, middleware=True)

    @app.get("/")
    @limiter.limit("1/minute")
    async def home(request: Request) -> dict[str, str]:
        return {}

    assert await statuses(app, "/", 3) == [200, 200, 200]


async def test_strategy(make_limiter: LimiterFactory) -> None:
    core = make_limiter()
    limiter = Limiter(strategy="fixed-window", limiter=core)
    app = slowapi_app(limiter)

    @app.get("/")
    @limiter.limit("3/minute")
    async def home(request: Request) -> dict[str, str]:
        return {}

    assert await statuses(app, "/", 1) == [200]
    rule = Rule(name_of(home, "3/1m"), "3/minute", algorithm="fixed_window")
    assert core.peek(rule, IP).remaining == 2


async def test_starlette_app_with_positional_request() -> None:
    limiter = memory_limiter()

    @limiter.limit("1/minute")
    async def home(request: Request) -> PlainTextResponse:
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route("/", home)])
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    assert await statuses(app, "/", 2) == [200, 429]


def test_endpoint_without_request_parameter_is_rejected_at_import() -> None:
    limiter = memory_limiter()
    with pytest.raises(ConfigurationError, match="request"):

        @limiter.limit("1/minute")
        async def home() -> None: ...


@pytest.mark.parametrize(
    "kwargs",
    [
        {"strategy": "moving-window"},
        {"key_style": "url"},
        {"swallow_errors": True},
        {"key_prefix": "app"},
        {"in_memory_fallback": ["1/second"]},
        {"auto_check": False},
        {"storage_uri": "memory://", "limiter": object()},
    ],
)
def test_unsupported_options_fail_loudly(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ConfigurationError):
        Limiter(**kwargs)


def test_invalid_rates_fail_at_import() -> None:
    limiter = memory_limiter()
    with pytest.raises(ConfigurationError):

        @limiter.limit("lots")
        async def home(request: Request) -> None: ...

    with pytest.raises(ConfigurationError):
        memory_limiter(default_limits=["lots"])
