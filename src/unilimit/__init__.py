"""UniLimit: one rate-limiting core for Django, FastAPI and plain Python."""

from unilimit._logging import configure_logging
from unilimit._version import __version__
from unilimit.algorithms import Algorithm
from unilimit.backends.memory import MemoryBackend
from unilimit.core.concurrency import ConcurrencyLimit, LeaseDecision
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
from unilimit.metrics import InMemoryMetrics
from unilimit.observability import (
    BackendUnavailableEvent,
    BreakerEvent,
    DecisionEvent,
    Event,
    FallbackEvent,
    Observer,
    RuleOutcome,
)

__all__ = [
    "AccessList",
    "Algorithm",
    "BackendError",
    "BackendUnavailable",
    "BackendUnavailableEvent",
    "BreakerEvent",
    "ConcurrencyLimit",
    "ConfigurationError",
    "Decision",
    "DecisionEvent",
    "Event",
    "FallbackEvent",
    "Identity",
    "IdentityError",
    "InMemoryMetrics",
    "LeaseDecision",
    "Limiter",
    "MemoryBackend",
    "Observer",
    "Rate",
    "RateLimitExceeded",
    "RateLimitResult",
    "Reason",
    "RequestContext",
    "Rule",
    "RuleOutcome",
    "TrustedProxies",
    "UniLimitError",
    "UnknownRuleError",
    "__version__",
    "configure_logging",
]
