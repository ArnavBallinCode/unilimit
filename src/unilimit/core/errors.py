"""Exception hierarchy. Every exception raised deliberately by UniLimit derives from
:class:`UniLimitError`."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from unilimit.core.result import Decision


class UniLimitError(Exception):
    """Base class for all UniLimit errors."""


class ConfigurationError(UniLimitError, ValueError):
    """Invalid rule, limiter or backend configuration."""


class UnknownRuleError(ConfigurationError, LookupError):
    """A rule was referenced by a name that is not registered on the limiter."""


class IdentityError(UniLimitError):
    """No identity could be resolved for a rule whose ``on_missing`` is ``"raise"``."""


class RateLimitExceeded(UniLimitError):  # noqa: N818 - established public name
    """A request was not admitted. Raised by framework integrations.

    ``decision`` explains why (rate limited, denied, missing identity or backend
    unavailable with fail-closed).
    """

    def __init__(self, decision: Decision, message: str = "rate limit exceeded") -> None:
        super().__init__(message)
        self.decision = decision


class BackendError(UniLimitError):
    """Unexpected backend failure. Never subject to fail-open/fail-closed handling."""


class BackendUnavailable(BackendError):  # noqa: N818 - established public name
    """The backend is temporarily unreachable (connection, timeout, cooldown).

    This is the only error to which a rule's ``fail_mode`` applies. ``partial_commit``
    is True when part of a multi-slot Redis Cluster batch was already committed before
    the failure.
    """

    def __init__(self, message: str = "", *, partial_commit: bool = False) -> None:
        super().__init__(message)
        self.partial_commit = partial_commit
