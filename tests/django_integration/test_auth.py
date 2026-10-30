"""Django authentication: sessions, ``request.user`` and ``request.auser()``."""

from __future__ import annotations

from typing import Any

import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth.models import User
from django.test import Client

from unilimit import Identity, Rule

from .conftest import LimiterFactory, asgi, wsgi


def _session_cookie(user: User) -> str:
    client = Client()
    client.force_login(user)
    return client.cookies["sessionid"].value


@pytest.mark.django_db
def test_user_key_with_sessions_under_wsgi(make_limiter: LimiterFactory) -> None:
    limiter = make_limiter()
    alice = User.objects.create_user("alice")
    bob = User.objects.create_user("bob")
    a, b = wsgi(), wsgi()
    a.force_login(alice)
    b.force_login(bob)
    assert [a.get("/user").status_code for _ in range(3)] == [200, 200, 429]
    assert [b.get("/user").status_code for _ in range(2)] == [200, 200]  # same IP, own quota
    anonymous = wsgi().get("/user")
    assert anonymous.status_code == 403  # on_missing="deny": no user, no access
    rule = Rule("dj-user", "2/minute", key="user")
    assert limiter.peek(rule, Identity("user", str(alice.pk))).remaining == 0


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("path", ["/user", "/user-async"])
async def test_user_key_with_sessions_under_asgi(make_limiter: LimiterFactory, path: str) -> None:
    make_limiter()
    alice = await sync_to_async(User.objects.create_user)("alice")
    cookie = await sync_to_async(_session_cookie)(alice)
    async with asgi(cookies={"sessionid": cookie}) as logged_in, asgi() as anonymous:
        statuses = [(await logged_in.get(path)).status_code for _ in range(3)]
        assert (await anonymous.get(path)).status_code == 403
    assert statuses == [200, 200, 429]


@pytest.mark.django_db(transaction=True)
async def test_user_or_ip_falls_back_to_ip(memory_limiter: LimiterFactory) -> None:
    memory_limiter()
    alice = await sync_to_async(User.objects.create_user)("alice")
    cookie = await sync_to_async(_session_cookie)(alice)
    async with asgi(cookies={"sessionid": cookie}) as logged_in, asgi() as anonymous:
        user = [(await logged_in.get("/user-or-ip")).status_code for _ in range(3)]
        anon = [(await anonymous.get("/user-or-ip")).status_code for _ in range(3)]
    assert user == anon == [200, 200, 429]  # separate counters: user 1 vs the IP


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("handler", ["wsgi", "asgi"])
async def test_global_user_rules_after_authentication_middleware(
    memory_limiter: LimiterFactory, handler: str
) -> None:
    memory_limiter(global_rules=[Rule("per-user", "2/minute", key="user", on_missing="skip")])
    alice = await sync_to_async(User.objects.create_user)("alice")
    cookie = await sync_to_async(_session_cookie)(alice)
    if handler == "asgi":
        async with asgi(cookies={"sessionid": cookie}) as c, asgi() as anon:
            statuses = [(await c.get("/ok")).status_code for _ in range(3)]
            anonymous = [(await anon.get("/ok")).status_code for _ in range(3)]
    else:

        def run() -> tuple[list[int], list[int]]:
            c = wsgi()
            c.cookies["sessionid"] = cookie
            return [c.get("/ok").status_code for _ in range(3)], [
                wsgi().get("/ok").status_code for _ in range(3)
            ]

        statuses, anonymous = await sync_to_async(run)()
    assert statuses == [200, 200, 429]
    assert anonymous == [200] * 3  # skipped: no user


@pytest.mark.django_db(transaction=True)
async def test_middleware_before_authentication_sees_no_user(
    memory_limiter: LimiterFactory, settings: Any
) -> None:
    """The documented pitfall (and system check W001): order matters."""
    settings.MIDDLEWARE = [
        "unilimit.integrations.django.UniLimitMiddleware",
        "django.contrib.sessions.middleware.SessionMiddleware",
        "django.contrib.auth.middleware.AuthenticationMiddleware",
    ]
    memory_limiter(global_rules=[Rule("per-user", "2/minute", key="user")])
    alice = await sync_to_async(User.objects.create_user)("alice")
    cookie = await sync_to_async(_session_cookie)(alice)
    async with asgi(cookies={"sessionid": cookie}) as c:
        assert (await c.get("/ok")).status_code == 403  # looks anonymous: denied


def test_no_session_lookup_when_no_rule_needs_the_user(memory_limiter: LimiterFactory) -> None:
    """IP rules never touch the session or the database (database access is blocked in
    this test, so any lookup would fail it)."""
    memory_limiter(global_rules=[Rule("site", "5/minute")])
    c = wsgi()
    c.cookies["sessionid"] = "does-not-exist"
    assert c.get("/sync").status_code == 200
