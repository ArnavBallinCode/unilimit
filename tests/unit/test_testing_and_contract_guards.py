import pytest

from unilimit.algorithms import Algorithm, LimitSpec
from unilimit.backends.base import BatchResult, ConsumeOp, evaluate_batch, peek_batch
from unilimit.core.policy import Plan, decide
from unilimit.testing import ManualClock

OP = ConsumeOp("k", LimitSpec(Algorithm.FIXED_WINDOW, 1, 1_000_000, 1))


def test_manual_clock() -> None:
    clock = ManualClock(start_us=10)
    clock.advance(1.5)
    clock.advance(us=5)
    assert clock.now_us() == 1_500_015
    with pytest.raises(ValueError, match="set"):
        clock.advance(-1)
    clock.set(3)
    assert clock.now_us() == 3


def test_reference_batch_functions_validate_lengths() -> None:
    with pytest.raises(ValueError, match="same length"):
        evaluate_batch([OP], [], 0)
    with pytest.raises(ValueError, match="same length"):
        peek_batch([OP], [None, None], 0)


def test_decide_rejects_mismatched_results() -> None:
    with pytest.raises(ValueError, match="does not match"):
        decide(Plan(()), BatchResult(True, evaluate_batch([OP], [None], 0)[0].results))
