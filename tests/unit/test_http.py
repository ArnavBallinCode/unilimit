import json

import pytest

from unilimit import Decision, Reason, Rule
from unilimit.core.result import RateLimitResult
from unilimit.http import (
    ResponseConfig,
    binding_result,
    rate_limit_headers,
    rejection_response,
)

RULE = Rule("api", "10/minute")
OTHER = Rule("burst", "2/second")


def result(rule: Rule = RULE, **kw: object) -> RateLimitResult:
    base: dict[str, object] = {
        "rule": rule,
        "allowed": True,
        "limit": rule.capacity,
        "remaining": 5,
        "reset_after": 12.3,
        "retry_after": 0.0,
    }
    base.update(kw)
    return RateLimitResult(**base)  # type: ignore[arg-type]


def test_success_headers_use_the_most_restrictive_result() -> None:
    headers = dict(
        rate_limit_headers([result(remaining=5), result(OTHER, remaining=1, reset_after=0.4)])
    )
    assert headers == {
        "X-RateLimit-Limit": "2",
        "X-RateLimit-Remaining": "1",
        "X-RateLimit-Reset": "1",  # rounded up, never 0 while capacity is missing
    }
    assert "Retry-After" not in headers


def test_rejection_headers_include_retry_after_of_the_longest_wait() -> None:
    results = [
        result(allowed=False, remaining=0, retry_after=3.2, reset_after=6.0),
        result(OTHER, allowed=False, remaining=0, retry_after=0.1, reset_after=0.5),
    ]
    headers = dict(rate_limit_headers(results, rejected=True))
    assert headers["Retry-After"] == "4"
    assert headers["X-RateLimit-Limit"] == "10"
    tiny = dict(rate_limit_headers([result(allowed=False, retry_after=0.01)], rejected=True))
    assert tiny["Retry-After"] == "1"  # at least one second
    never = dict(rate_limit_headers([result(allowed=False, retry_after=None)], rejected=True))
    assert "Retry-After" not in never  # the cost can never fit


def test_shadow_and_degraded_results_never_drive_headers() -> None:
    assert rate_limit_headers([result(Rule("s", "1/s", action="log"), enforced=False)]) == []
    assert rate_limit_headers([result(degraded=True)]) == []
    assert binding_result([]) is None


def test_epoch_reset_and_disabled_legacy_headers() -> None:
    epoch = dict(rate_limit_headers([result()], ResponseConfig(reset_format="epoch"), now=1000.0))
    assert epoch["X-RateLimit-Reset"] == "1013"
    assert rate_limit_headers([result()], ResponseConfig(legacy_headers=False)) == []


def test_ietf_draft_headers_are_opt_in_and_never_include_partition_keys() -> None:
    assert "RateLimit" not in dict(rate_limit_headers([result()]))
    weird = Rule('a "quoted" \\ ünï', "3/2s")
    headers = dict(
        rate_limit_headers(
            [result(), result(weird, remaining=1, reset_after=1.5)],
            ResponseConfig(ietf_headers=True),
        )
    )
    assert headers["RateLimit-Policy"] == '"api";q=10;w=60, "a \\"quoted\\" \\\\ _n_";q=3;w=2'
    assert headers["RateLimit"] == '"api";r=5;t=13, "a \\"quoted\\" \\\\ _n_";r=1;t=2'
    assert "pk=" not in headers["RateLimit"]


@pytest.mark.parametrize(
    ("reason", "status", "detail"),
    [
        (Reason.RATE_LIMITED, 429, "Too Many Requests"),
        (Reason.DENIED, 403, "Forbidden"),
        (Reason.MISSING_IDENTITY, 403, "Forbidden"),
        (Reason.BACKEND_UNAVAILABLE, 503, "Service Unavailable"),
    ],
)
def test_default_rejections(reason: Reason, status: int, detail: str) -> None:
    spec = rejection_response(Decision(allowed=False, reason=reason))
    assert spec.status_code == status
    assert json.loads(spec.body) == {"detail": detail}
    assert spec.media_type == "application/json"
    assert spec.detail == detail


def test_custom_bodies_and_statuses() -> None:
    decision = Decision(
        allowed=False,
        reason=Reason.RATE_LIMITED,
        results=(result(allowed=False, retry_after=2.0),),
        binding=result(allowed=False, retry_after=2.0),
    )
    text = rejection_response(decision, ResponseConfig(body="slow down", rate_limited_status=503))
    assert (text.status_code, text.body, text.media_type) == (
        503,
        b"slow down",
        "text/plain; charset=utf-8",
    )
    assert text.detail == "slow down"
    custom = rejection_response(
        decision,
        ResponseConfig(body=lambda d: {"error": d.reason.value, "retry": d.retry_after}),
    )
    assert json.loads(custom.body) == {"error": "rate_limited", "retry": 2.0}
    assert ("Retry-After", "2") in custom.headers


def test_invalid_configuration() -> None:
    with pytest.raises(ValueError, match="reset_format"):
        ResponseConfig(reset_format="iso")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="HTTP error status"):
        ResponseConfig(rate_limited_status=200)
    with pytest.raises(ValueError, match="nothing to reject"):
        rejection_response(Decision(allowed=True, reason=Reason.ALLOWED))
