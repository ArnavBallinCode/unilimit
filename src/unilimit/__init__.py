"""UniLimit: one rate-limiting core for Django, FastAPI and plain Python."""

from unilimit._version import __version__
from unilimit.algorithms import Algorithm
from unilimit.backends.memory import MemoryBackend
from unilimit.core.context import RequestContext
from unilimit.core.errors import (
    BackendError,
    BackendUnavailable,
    ConfigurationError,
    IdentityError,
    RateLimitExceeded,
    UniLimitError,
    UnknownRuleError,
)
from unilimit.core.identity import Identity, TrustedProxies
from unilimit.core.limiter import Limiter
from unilimit.core.policy import AccessList
from unilimit.core.rate import Rate
from unilimit.core.result import Decision, RateLimitResult, Reason
from unilimit.core.rule import Rule

__all__ = [
    "AccessList",
    "Algorithm",
    "BackendError",
    "BackendUnavailable",
    "ConfigurationError",
    "Decision",
    "Identity",
    "IdentityError",
    "Limiter",
    "MemoryBackend",
    "Rate",
    "RateLimitExceeded",
    "RateLimitResult",
    "Reason",
    "RequestContext",
    "Rule",
    "TrustedProxies",
    "UniLimitError",
    "UnknownRuleError",
    "__version__",
]
