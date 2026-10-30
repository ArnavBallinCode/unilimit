import pytest
from redis import exceptions as rex

from unilimit import BackendError, BackendUnavailable
from unilimit.backends.redis._errors import is_outage, translate

REDIS62_SCRIPT_OOM = (
    "Error running script (call to f_f0f4aa47880da4677a5bbb9343b06f60209659ba): "
    "@user_script:326: @user_script: 326: -OOM command not allowed when used memory > "
    "'maxmemory'."
)


@pytest.mark.parametrize(
    "exc",
    [
        rex.ConnectionError("refused"),
        rex.TimeoutError("timed out"),
        rex.BusyLoadingError("loading"),
        rex.ClusterDownError("CLUSTERDOWN The cluster is down"),
        rex.TryAgainError("TRYAGAIN"),
        rex.ReadOnlyError("READONLY You can't write against a read only replica."),
        rex.OutOfMemoryError("OOM command not allowed"),
        rex.ResponseError("BUSY Redis is busy running a script"),
        rex.ResponseError(REDIS62_SCRIPT_OOM),
        rex.SlotNotCoveredError("slot not covered"),
        ConnectionRefusedError(61, "refused"),
    ],
)
def test_transient_errors_become_backend_unavailable(exc: Exception) -> None:
    error = translate(exc, partial_commit=True)
    assert isinstance(error, BackendUnavailable)
    assert error.partial_commit
    assert is_outage(exc)


@pytest.mark.parametrize(
    "exc",
    [
        rex.AuthenticationError("invalid password"),
        rex.AuthorizationError("NOAUTH"),
        rex.NoPermissionError("NOPERM"),
        rex.ResponseError("WRONGTYPE Operation against a key holding the wrong kind of value"),
        rex.ResponseError("ERR Error running script: attempt to compare nil with number"),
        rex.ResponseError("ERR unknown command"),
        rex.DataError("bad argument"),
    ],
)
def test_other_errors_are_never_subject_to_fail_open(exc: Exception) -> None:
    error = translate(exc, partial_commit=False)
    assert isinstance(error, BackendError)
    assert not isinstance(error, BackendUnavailable)
    assert not is_outage(exc)


def test_pool_exhaustion_is_unavailable_but_not_an_outage() -> None:
    exc = rex.MaxConnectionsError("Too many connections")
    assert isinstance(translate(exc, partial_commit=False), BackendUnavailable)
    assert not is_outage(exc)  # Redis is healthy: no cooldown
