"""Settings, system checks, rejection responses and decorator misuse."""

from __future__ import annotations

import json
from typing import Any

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.http import HttpRequest, HttpResponse, JsonResponse

from unilimit import ConfigurationError, Decision, Limiter, Rule
from unilimit.http import ResponseConfig
from unilimit.integrations.django import conf, ratelimit
from unilimit.integrations.django.checks import check_settings
from unilimit.integrations.drf.checks import check_drf_settings

from .conftest import LimiterFactory, ratelimit_headers, wsgi

SECURITY = "django.contrib.security.middleware.SecurityMiddleware"
SESSION = "django.contrib.sessions.middleware.SessionMiddleware"
AUTH = "django.contrib.auth.middleware.AuthenticationMiddleware"
OURS = "unilimit.integrations.django.UniLimitMiddleware"

LIMITER = Limiter.from_url("memory://", global_rules=[Rule("u", "5/minute", key="user")])


def rejected(request: HttpRequest, decision: Decision) -> HttpResponse:
    return JsonResponse({"error": decision.reason.value}, status=429)


# -- settings ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "message"),
    [
        (None, "not configured"),
        ("redis://x", "must be a dict"),
        ({"URL": "memory://", "TYPO": 1}, "unknown UNILIMIT keys"),
        ({}, "exactly one"),
        ({"URL": "memory://", "LIMITER": LIMITER}, "exactly one"),
        ({"LIMITER": LIMITER, "OPTIONS": {}}, "applies to 'URL' only"),
        ({"URL": "memory://", "OPTIONS": []}, "must be a dict"),
        ({"URL": "ftp://x"}, "unsupported backend"),
        ({"URL": "memory://", "OPTIONS": {"bogus": 1}}, "OPTIONS"),
        ({"LIMITER": "no.such.module"}, "cannot import"),
        ({"LIMITER": "json.dumps"}, "must be a Limiter"),
        ({"LIMITER": LIMITER, "RESPONSE": "json.dumps"}, "ResponseConfig"),
        ({"LIMITER": LIMITER, "REJECTION_VIEW": 3}, "callable"),
    ],
)
def test_invalid_settings(settings: Any, value: Any, message: str) -> None:
    if value is None:
        del settings.UNILIMIT
    else:
        settings.UNILIMIT = value
    with pytest.raises(ImproperlyConfigured, match=message):
        conf.load_config()
    errors = check_settings()
    assert [e.id for e in errors] == ["unilimit.E001"]


def test_limiter_by_dotted_path(settings: Any) -> None:
    settings.UNILIMIT = {"LIMITER": "tests.django_integration.test_config.LIMITER"}
    assert conf.get_limiter() is LIMITER


# -- system checks -----------------------------------------------------------------------


def test_check_middleware_before_authentication(settings: Any) -> None:
    settings.UNILIMIT = {"LIMITER": LIMITER}
    settings.MIDDLEWARE = [SECURITY, OURS, SESSION, AUTH]
    assert [m.id for m in check_settings()] == ["unilimit.W001"]
    settings.MIDDLEWARE = [SESSION, AUTH, OURS]
    assert check_settings() == []


def test_check_missing_middleware_with_global_rules(settings: Any) -> None:
    settings.UNILIMIT = {"LIMITER": LIMITER}
    settings.MIDDLEWARE = [SESSION, AUTH]
    assert [m.id for m in check_settings()] == ["unilimit.W002"]


def test_check_ip_rules_need_no_ordering(settings: Any) -> None:
    settings.UNILIMIT = {"URL": "memory://"}
    settings.MIDDLEWARE = [OURS, SESSION, AUTH]
    assert check_settings() == []


def test_drf_checks(settings: Any) -> None:
    settings.REST_FRAMEWORK = {
        "DEFAULT_THROTTLE_CLASSES": [
            "rest_framework.throttling.AnonRateThrottle",
            "unilimit.integrations.drf.UniLimitAnonRateThrottle",
            "unilimit.integrations.drf.UniLimitUserRateThrottle",
        ],
        "DEFAULT_THROTTLE_RATES": {"anon": "1/month", "user": "10/min", "x": "5/30s"},
    }
    assert sorted(str(m.id) for m in check_drf_settings()) == ["unilimit.E002", "unilimit.W003"]
    settings.REST_FRAMEWORK = {
        "DEFAULT_THROTTLE_CLASSES": [
            "rest_framework.throttling.ScopedRateThrottle",
            "unilimit.integrations.drf.UniLimitScopedRateThrottle",
        ],
    }
    assert [m.id for m in check_drf_settings()] == ["unilimit.E002"]
    settings.REST_FRAMEWORK = {
        "DEFAULT_THROTTLE_CLASSES": [
            "rest_framework.throttling.UserRateThrottle",
            "unilimit.integrations.drf.UniLimitAnonRateThrottle",
        ],
        "DEFAULT_THROTTLE_RATES": {"anon": "1/day", "user": "10/hour"},
    }
    assert check_drf_settings() == []


def test_checks_run_through_django(settings: Any) -> None:
    from django.core.checks import run_checks

    settings.MIDDLEWARE = [OURS, SESSION, AUTH]
    settings.UNILIMIT = {"LIMITER": LIMITER}
    assert "unilimit.W001" in {m.id for m in run_checks(tags=["unilimit"])}


# -- responses ---------------------------------------------------------------------------


def test_custom_response_config(memory_limiter: LimiterFactory, settings: Any) -> None:
    limiter = memory_limiter()
    settings.UNILIMIT = {
        "LIMITER": limiter,
        "RESPONSE": ResponseConfig(
            rate_limited_status=503, body="slow down", ietf_headers=True, legacy_headers=False
        ),
    }
    c = wsgi()
    responses = [c.get("/sync") for _ in range(3)]
    assert responses[2].status_code == 503
    assert responses[2].content == b"slow down"
    assert responses[2]["Content-Type"] == "text/plain; charset=utf-8"
    assert "ratelimit-policy" in ratelimit_headers(responses[0])
    assert "x-ratelimit-limit" not in ratelimit_headers(responses[0])


def test_rejection_view(memory_limiter: LimiterFactory, settings: Any) -> None:
    limiter = memory_limiter()
    settings.UNILIMIT = {
        "LIMITER": limiter,
        "REJECTION_VIEW": "tests.django_integration.test_config.rejected",
    }
    c = wsgi()
    responses = [c.get("/sync") for _ in range(3)]
    assert json.loads(responses[2].content) == {"error": "rate_limited"}
    assert "retry-after" in ratelimit_headers(responses[2])  # added if the view did not


def test_denied_and_missing_identity_statuses(memory_limiter: LimiterFactory) -> None:
    from unilimit import AccessList

    memory_limiter(deny=AccessList(networks=["203.0.113.0/24"]))
    assert wsgi().get("/ok").status_code == 403
    assert wsgi(peer="198.51.100.1").get("/ok").status_code == 200


# -- decorator misuse ----------------------------------------------------------------------


def test_decorator_misuse() -> None:
    with pytest.raises(ConfigurationError):
        ratelimit()
    with pytest.raises(TypeError, match="method_decorator"):
        ratelimit("x", rate="1/s")(type("V", (), {}))
    with pytest.raises(ConfigurationError, match="inline"):
        ratelimit("a", "b", rate="1/s")
    with pytest.raises(ConfigurationError, match="no rule named 'nope'"):

        @ratelimit("nope", limiter=Limiter.from_url("memory://"))
        def view(request: HttpRequest) -> HttpResponse:
            return HttpResponse()


def test_decorated_view_needs_a_request(memory_limiter: LimiterFactory) -> None:
    memory_limiter()

    @ratelimit("x", rate="1/s")
    def view(value: int) -> int:
        return value

    with pytest.raises(TypeError, match="request"):
        view(1)


def test_global_rules_without_middleware_warn_once(
    memory_limiter: LimiterFactory, settings: Any, caplog: pytest.LogCaptureFixture
) -> None:
    settings.MIDDLEWARE = []
    memory_limiter(global_rules=[Rule("site", "100/minute")])
    from unilimit.integrations.django import _request

    _request._warned.clear()
    c = wsgi()
    for _ in range(2):
        c.get("/sync")
    warnings = [r for r in caplog.records if "not in MIDDLEWARE" in r.getMessage()]
    assert len(warnings) == 1
