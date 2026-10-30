"""Framework-independent HTTP representation of rate-limit decisions.

Integrations turn a :class:`~unilimit.Decision` into a :class:`ResponseSpec` (status,
headers, body) and copy :func:`rate_limit_headers` onto successful responses. Nothing
here depends on a web framework, and the core limiter does not depend on this module.

Headers (``legacy_headers``, on by default):

- ``X-RateLimit-Limit``: capacity of the binding rule.
- ``X-RateLimit-Remaining``: quota left under the binding rule.
- ``X-RateLimit-Reset``: seconds until full capacity is restored (rounded up), or a Unix
  timestamp with ``reset_format="epoch"``.
- ``Retry-After``: on rejection, whole seconds until the request could succeed (at least
  1). Omitted when it can never succeed (cost above capacity).

The draft IETF fields ``RateLimit-Policy`` / ``RateLimit``
(draft-ietf-httpapi-ratelimit-headers-11) are opt-in via ``ietf_headers=True``. They
list every enforced rule. Partition keys (``pk``) are never sent, so identities are not
disclosed.
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from unilimit.core.result import Decision, RateLimitResult, Reason

Body = Mapping[str, Any] | str
BodyFactory = Callable[[Decision], Body]

_DEFAULT_BODIES: dict[Reason, Body] = {
    Reason.RATE_LIMITED: {"detail": "Too Many Requests"},
    Reason.DENIED: {"detail": "Forbidden"},
    Reason.MISSING_IDENTITY: {"detail": "Forbidden"},
    Reason.BACKEND_UNAVAILABLE: {"detail": "Service Unavailable"},
}


@dataclass(frozen=True, slots=True)
class ResponseSpec:
    """A framework-neutral HTTP response."""

    status_code: int
    headers: tuple[tuple[str, str], ...]
    body: bytes
    media_type: str

    @property
    def detail(self) -> str:
        """A short text for frameworks whose error type carries a single message."""
        if self.media_type == "application/json":
            parsed = json.loads(self.body)
            detail = parsed.get("detail") if isinstance(parsed, dict) else None
            if isinstance(detail, str):
                return detail
        return self.body.decode("utf-8", "replace")


@dataclass(frozen=True, slots=True)
class ResponseConfig:
    """How rejections and rate-limit headers are rendered.

    ``body`` is a JSON-serialisable mapping, a plain-text string, or a callable that
    receives the :class:`Decision` and returns either. ``None`` uses a generic body per
    reason. Bodies never contain internal error details.
    """

    rate_limited_status: int = 429
    denied_status: int = 403
    missing_identity_status: int = 403
    unavailable_status: int = 503
    body: Body | BodyFactory | None = None
    legacy_headers: bool = True
    ietf_headers: bool = False
    reset_format: Literal["delta", "epoch"] = "delta"

    def __post_init__(self) -> None:
        if self.reset_format not in ("delta", "epoch"):
            raise ValueError("reset_format must be 'delta' or 'epoch'")
        for name in (
            "rate_limited_status",
            "denied_status",
            "missing_identity_status",
            "unavailable_status",
        ):
            status = getattr(self, name)
            if not isinstance(status, int) or not 400 <= status <= 599:
                raise ValueError(f"{name} must be an HTTP error status (4xx or 5xx)")

    def status_for(self, reason: Reason) -> int:
        return {
            Reason.RATE_LIMITED: self.rate_limited_status,
            Reason.DENIED: self.denied_status,
            Reason.MISSING_IDENTITY: self.missing_identity_status,
            Reason.BACKEND_UNAVAILABLE: self.unavailable_status,
        }[reason]


def binding_result(results: Iterable[RateLimitResult]) -> RateLimitResult | None:
    """The result that determines headers among enforced, non-degraded results.

    A rejecting result with the longest wait wins (``None`` = never). Otherwise the
    result with the least remaining quota, then the soonest reset. Ties keep order.
    """
    candidates = [r for r in results if r.enforced and not r.degraded]
    rejecting = [r for r in candidates if not r.allowed]
    if rejecting:
        return max(rejecting, key=lambda r: math.inf if r.retry_after is None else r.retry_after)
    if candidates:
        return min(candidates, key=lambda r: (r.remaining, r.reset_after))
    return None


def _seconds(value: float) -> int:
    return max(0, math.ceil(value - 1e-9))


def _sf_string(value: str) -> str:
    """An RFC 8941 sf-string: printable ASCII only, with quotes and backslashes escaped."""
    safe = "".join(c if 0x20 <= ord(c) <= 0x7E else "_" for c in value)
    return '"' + safe.replace("\\", "\\\\").replace('"', '\\"') + '"'


def rate_limit_headers(
    results: Iterable[RateLimitResult],
    config: ResponseConfig | None = None,
    *,
    rejected: bool = False,
    now: float | None = None,
) -> list[tuple[str, str]]:
    """Headers describing ``results`` (from one decision or several merged ones)."""
    config = config or ResponseConfig()
    results = [r for r in results if r.enforced and not r.degraded]
    binding = binding_result(results)
    headers: list[tuple[str, str]] = []
    if binding is None:
        return headers
    if config.legacy_headers:
        reset = _seconds(binding.reset_after)
        if config.reset_format == "epoch":
            reset = math.ceil((time.time() if now is None else now) + binding.reset_after)
        headers += [
            ("X-RateLimit-Limit", str(binding.limit)),
            ("X-RateLimit-Remaining", str(max(0, binding.remaining))),
            ("X-RateLimit-Reset", str(reset)),
        ]
    if config.ietf_headers:
        seen: dict[str, RateLimitResult] = {}
        for r in results:
            seen.setdefault(r.rule.name, r)
        headers.append(
            (
                "RateLimit-Policy",
                ", ".join(
                    f"{_sf_string(n)};q={r.limit};w={_seconds(r.rule.rate.window_seconds)}"
                    for n, r in seen.items()
                ),
            )
        )
        headers.append(
            (
                "RateLimit",
                ", ".join(
                    f"{_sf_string(n)};r={max(0, r.remaining)};t={_seconds(r.reset_after)}"
                    for n, r in seen.items()
                ),
            )
        )
    if rejected and binding.retry_after is not None:
        headers.append(("Retry-After", str(max(1, math.ceil(binding.retry_after - 1e-9)))))
    return headers


def rejection_response(decision: Decision, config: ResponseConfig | None = None) -> ResponseSpec:
    """The HTTP response for a decision that did not admit the request."""
    if decision.allowed:
        raise ValueError("decision admitted the request; there is nothing to reject")
    config = config or ResponseConfig()
    body: Body | BodyFactory | None = config.body
    if callable(body):
        body = body(decision)
    if body is None:
        body = _DEFAULT_BODIES[decision.reason]
    if isinstance(body, str):
        payload, media_type = body.encode("utf-8"), "text/plain; charset=utf-8"
    else:
        payload, media_type = json.dumps(dict(body)).encode("utf-8"), "application/json"
    headers = rate_limit_headers(decision.results, config, rejected=True)
    return ResponseSpec(config.status_for(decision.reason), tuple(headers), payload, media_type)


__all__ = [
    "ResponseConfig",
    "ResponseSpec",
    "binding_result",
    "rate_limit_headers",
    "rejection_response",
]
