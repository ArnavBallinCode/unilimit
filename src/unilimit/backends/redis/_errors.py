"""Classification of redis-py errors into UniLimit's failure semantics.

Transient availability problems become :class:`BackendUnavailable`, so ``fail_mode``
applies. Everything else, including authentication and permission errors, becomes
:class:`BackendError` and propagates: a misconfigured password must never fail open
silently.
"""

from __future__ import annotations

from redis import exceptions as rex

from unilimit.core.errors import BackendError, BackendUnavailable

_NEVER_TRANSIENT = (
    rex.AuthenticationError,
    rex.AuthorizationError,
    rex.NoPermissionError,
)

_TRANSIENT = (
    rex.ConnectionError,  # includes BusyLoadingError and MaxConnectionsError
    rex.TimeoutError,
    rex.ClusterDownError,  # includes MasterDownError
    rex.TryAgainError,
    rex.ReadOnlyError,  # writes reaching a replica during failover
    rex.OutOfMemoryError,
    rex.SlotNotCoveredError,
    OSError,
)

_TRANSIENT_CODES = ("BUSY", "LOADING", "MASTERDOWN", "CLUSTERDOWN", "OOM", "READONLY", "TRYAGAIN")


def _has_transient_code(message: str) -> bool:
    # Redis 7 returns the code directly ("OOM command not allowed ..."). Redis 6.2 wraps
    # errors raised inside a script: "Error running script (...): ... -OOM command ...".
    return any(
        message.startswith(f"{code} ") or f": -{code} " in message for code in _TRANSIENT_CODES
    )


def translate(exc: BaseException, *, partial_commit: bool) -> BackendError:
    """Map an exception raised by redis-py to a UniLimit backend error."""
    if isinstance(exc, _NEVER_TRANSIENT):
        return BackendError(f"Redis rejected the connection: {exc}")
    if is_transient(exc):
        return BackendUnavailable(f"{type(exc).__name__}: {exc}", partial_commit=partial_commit)
    return BackendError(f"Redis error: {type(exc).__name__}: {exc}")


def is_transient(exc: BaseException) -> bool:
    if isinstance(exc, _NEVER_TRANSIENT):
        return False
    return isinstance(exc, _TRANSIENT) or (
        isinstance(exc, rex.ResponseError) and _has_transient_code(str(exc))
    )


def is_outage(exc: BaseException) -> bool:
    """Whether ``exc`` means Redis itself is unhealthy, which starts a cooldown.

    Exhausting the local connection pool is overload on our side, not an outage.
    """
    return is_transient(exc) and not isinstance(exc, rex.MaxConnectionsError)


HANDLED = (rex.RedisError, rex.RedisClusterException, OSError)
