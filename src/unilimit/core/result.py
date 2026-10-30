from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from unilimit.core.rule import Rule


class Reason(StrEnum):
    ALLOWED = "allowed"
    EXEMPT = "exempt"
    RATE_LIMITED = "rate_limited"
    DENIED = "denied"
    MISSING_IDENTITY = "missing_identity"
    BACKEND_UNAVAILABLE = "backend_unavailable"


@dataclass(frozen=True, slots=True)
class RateLimitResult:
    """Outcome of one rule for one identity. Never contains the identity value.

    ``remaining`` describes the state after this request when it was committed, and the
    current state otherwise. ``retry_after`` is ``0.0`` when allowed and ``None`` when
    the cost can never fit. Degraded results come from fail-open/closed handling, not
    from the backend, and their figures carry no information.
    """

    rule: Rule
    allowed: bool
    limit: int
    remaining: int
    reset_after: float
    retry_after: float | None
    enforced: bool = True
    degraded: bool = False
    storage_key: str | None = None
    """Backend key of this counter. It contains only a keyed digest of the identity."""


@dataclass(frozen=True, slots=True)
class Decision:
    """Outcome of evaluating a request against all applicable rules."""

    allowed: bool
    reason: Reason
    results: tuple[RateLimitResult, ...] = ()
    binding: RateLimitResult | None = None
    """The result that determines response headers (see ARCHITECTURE.md §6.1)."""
    degraded: bool = False
    partial_commit: bool = False
    rule: Rule | None = None
    """For ``MISSING_IDENTITY``: the rule whose identity could not be resolved."""

    @property
    def retry_after(self) -> float | None:
        if self.allowed or self.binding is None:
            return None
        return self.binding.retry_after

    @property
    def violations(self) -> tuple[RateLimitResult, ...]:
        """Shadow (``action="log"``) results that would have rejected the request."""
        return tuple(r for r in self.results if not r.enforced and not r.allowed)
