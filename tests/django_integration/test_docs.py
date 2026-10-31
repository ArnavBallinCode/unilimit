"""The Django and DRF documentation is executable: every ```python block of each page
runs, in order, against a real Redis, and the behaviour the page describes is checked
through Django's real request handlers."""

from __future__ import annotations

import re
import sys
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.test import Client

from unilimit import Limiter
from unilimit.integrations.django import conf

from .conftest import ratelimit_headers, wsgi

DOCS = Path(__file__).resolve().parents[2] / "docs"
PAGES = sorted(
    f"{d.name}/{p.name}" for d in (DOCS / "django", DOCS / "drf") for p in d.glob("*.md")
)
TEXT_ONLY = {"django/versions.md"}


def python_blocks(page: str) -> str:
    return "\n\n".join(re.findall(r"```python\n(.*?)```", (DOCS / page).read_text(), re.S))


@pytest.fixture
def serve(redis_url: str, settings: Any) -> Iterator[Any]:
    """Execute a page and install what it defines (``urlpatterns``, ``UNILIMIT``,
    ``MIDDLEWARE``, ``REST_FRAMEWORK``) as the project's settings."""
    modules: list[str] = []

    def run(page: str) -> dict[str, Any]:
        code = python_blocks(page).replace("redis://localhost:6379/0", redis_url)
        namespace: dict[str, Any] = {"__name__": "docs_" + re.sub(r"\W", "_", page)}
        exec(compile(code, str(DOCS / page), "exec"), namespace)
        urls = types.ModuleType(f"docs_urls_{len(modules)}")
        urls.urlpatterns = namespace.get("urlpatterns", [])  # type: ignore[attr-defined]
        sys.modules[urls.__name__] = urls
        modules.append(urls.__name__)
        settings.ROOT_URLCONF = urls.__name__
        for name in ("UNILIMIT", "MIDDLEWARE", "REST_FRAMEWORK"):
            if name in namespace:
                setattr(settings, name, namespace[name])
        conf.reset()
        return namespace

    yield run
    config = conf._config  # close what the page created, then forget it
    if config is not None:
        config.limiter.close()
    conf.reset()
    for name in modules:
        del sys.modules[name]


def test_every_page_has_examples() -> None:
    assert set(PAGES) == {
        "django/authentication.md",
        "django/decorators.md",
        "django/errors-and-headers.md",
        "django/middleware.md",
        "django/migrating-from-django-ratelimit.md",
        "django/quickstart.md",
        "django/redis.md",
        "django/versions.md",
        "drf/migration.md",
        "drf/quickstart.md",
    }
    for page in PAGES:
        assert page in TEXT_ONLY or python_blocks(page), page


def test_quickstart(serve: Any) -> None:
    serve("django/quickstart.md")
    client = wsgi()
    searches = [client.get("/search") for _ in range(11)]
    status = client.get("/status")
    assert [r.status_code for r in searches] == [200] * 10 + [429]
    assert ratelimit_headers(searches[0])["x-ratelimit-limit"] == "10"
    assert "retry-after" in ratelimit_headers(searches[-1])
    assert ratelimit_headers(status)["x-ratelimit-limit"] == "100"  # the global "site" rule
    assert [client.get("/report").status_code for _ in range(6)] == [200] * 5 + [429]


def test_middleware_page(serve: Any) -> None:
    serve("django/middleware.md")
    client = wsgi()
    health = [client.get("/health") for _ in range(30)]
    feed = client.get("/feed")
    assert all(r.status_code == 200 and not ratelimit_headers(r) for r in health)
    assert ratelimit_headers(feed)["x-ratelimit-limit"] == "40"  # burst=40 binds first
    assert wsgi("198.51.100.4").get("/feed").status_code == 403  # deny list
    behind = wsgi("10.1.2.3")
    first = behind.get("/feed", headers={"X-Forwarded-For": "192.0.2.1"})
    second = behind.get("/feed", headers={"X-Forwarded-For": "192.0.2.2"})
    assert (
        ratelimit_headers(first)["x-ratelimit-remaining"]
        == ratelimit_headers(second)["x-ratelimit-remaining"]
    )  # different clients behind the trusted proxy: separate counters
    assert wsgi().get("/missing").status_code == 404  # global rules count 404s too


def test_decorators_page(serve: Any) -> None:
    serve("django/decorators.md")
    client = wsgi()
    assert ratelimit_headers(client.get("/search"))["x-ratelimit-limit"] == "10"
    assert [client.post("/login").status_code for _ in range(6)] == [200] * 5 + [429]
    assert client.get("/login").status_code == 200  # methods=["POST"]
    daily, weekly = client.get("/daily"), client.get("/weekly")
    assert ratelimit_headers(daily)["x-ratelimit-remaining"] == "1"
    assert ratelimit_headers(weekly)["x-ratelimit-remaining"] == "0"  # shared counter
    assert client.get("/weekly").status_code == 429
    assert ratelimit_headers(client.get("/api"))["x-ratelimit-limit"] == "10"
    assert client.get("/tiered").status_code == 200
    assert [client.get("/export").status_code for _ in range(3)] == [200, 200, 429]
    assert [client.get("/heavy").status_code for _ in range(5)] == [200] * 4 + [429]  # cost=5
    assert [client.get("/articles").status_code for _ in range(4)] == [200] * 3 + [429]


@pytest.mark.django_db
def test_authentication_page(serve: Any) -> None:
    serve("django/authentication.md")
    alice, bob = User.objects.create_user("alice"), User.objects.create_user("bob")
    a, b = wsgi(), wsgi()
    a.force_login(alice)
    b.force_login(bob)
    assert [a.get("/profile").status_code for _ in range(2)] == [200, 200]
    assert wsgi().get("/profile").status_code == 403  # on_missing="deny" before the login check
    anonymous = [wsgi().get("/articles") for _ in range(2)]
    signed_in = a.get("/articles")
    assert ratelimit_headers(anonymous[1])["x-ratelimit-remaining"] == "58"
    assert ratelimit_headers(signed_in)["x-ratelimit-remaining"] == "59"  # own counter
    keyed = Client(REMOTE_ADDR="203.0.113.50", HTTP_X_API_KEY="k1")
    other = Client(REMOTE_ADDR="203.0.113.50", HTTP_X_API_KEY="k2")
    assert ratelimit_headers(keyed.get("/partner"))["x-ratelimit-remaining"] == "599"
    assert ratelimit_headers(keyed.get("/partner"))["x-ratelimit-remaining"] == "598"
    assert ratelimit_headers(other.get("/partner"))["x-ratelimit-remaining"] == "599"
    tenant = Client(HTTP_X_TENANT="acme")
    assert ratelimit_headers(tenant.get("/tenant"))["x-ratelimit-limit"] == "1000"
    reports = [wsgi().get("/org-reports?org=acme") for _ in range(2)]
    assert ratelimit_headers(reports[1])["x-ratelimit-remaining"] == "18"
    assert wsgi().get("/lenient").status_code == 200  # anonymous: rule skipped


def test_redis_page(serve: Any) -> None:
    namespace = serve("django/redis.md")
    assert isinstance(namespace["limiter"], Limiter)
    assert conf.get_limiter() is namespace["limiter"]
    assert "with_lifespan(get_asgi_application())" in (DOCS / "django" / "redis.md").read_text()
    namespace["limiter"].close()


def test_errors_page(serve: Any) -> None:
    serve("django/errors-and-headers.md")
    client = wsgi()
    assert client.get("/x").status_code == 200
    limited = client.get("/x")
    assert limited.status_code == 429
    assert limited.json()["title"] == "Too many requests"
    assert limited.json()["retry_after"] > 0
    assert "retry-after" in ratelimit_headers(limited)


def test_drf_quickstart(serve: Any) -> None:
    serve("drf/quickstart.md")
    client = wsgi()
    assert [client.get("/profile").status_code for _ in range(2)] == [200, 200]
    for _ in range(98):
        client.get("/profile")
    throttled = client.get("/profile")
    assert throttled.status_code == 429
    assert "Retry-After" in throttled.headers
    assert throttled.json()["detail"].startswith("Request was throttled")
    assert [client.post("/upload").status_code for _ in range(11)] == [200] * 10 + [429]
    assert [client.get("/search").status_code for _ in range(6)] == [200] * 5 + [429]


def test_drf_migration(serve: Any) -> None:
    serve("drf/migration.md")
    client = wsgi()
    assert [client.get("/contacts").status_code for _ in range(21)] == [200] * 20 + [429]


@pytest.mark.django_db
def test_migrating_from_django_ratelimit_page(serve: Any) -> None:
    serve("django/migrating-from-django-ratelimit.md")
    client = wsgi()
    searches = [client.get("/search") for _ in range(6)]
    assert [r.status_code for r in searches] == [200] * 5 + [429]
    assert "retry-after" in ratelimit_headers(searches[-1])
    staff = wsgi()
    staff.force_login(User.objects.create_user("staff", is_staff=True))
    assert [staff.get("/search").status_code for _ in range(7)] == [200] * 7  # rate None
    assert [client.get("/submit").status_code for _ in range(3)] == [200] * 3  # GET: safe
    assert client.post("/submit").status_code == 200
    comments = [client.get("/comment") for _ in range(4)]
    assert [r.status_code for r in comments] == [200, 200, 200, 400]  # block=False
    assert comments[-1].json() == {"error": "slow down"}
