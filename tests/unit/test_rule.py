from datetime import timedelta

import pytest

from unilimit import Algorithm, ConfigurationError, Rate, Rule
from unilimit.algorithms import LimitSpec


def test_rate_string_and_limit_window_are_equivalent() -> None:
    a = Rule("r", "100/minute")
    b = Rule("r", limit=100, window="1m")
    c = Rule("r", limit=100, window=timedelta(minutes=1))
    assert a == b == c
    assert hash(a) == hash(b)
    assert a.limit == 100
    assert a.window_us == 60_000_000


def test_defaults() -> None:
    rule = Rule("r", "1/s")
    assert rule.algorithm is Algorithm.SLIDING_WINDOW
    assert rule.key == ("ip",)
    assert rule.action == "reject"
    assert rule.enforced
    assert rule.on_missing is None
    assert rule.fail_mode is None
    assert rule.capacity == 1
    assert rule.spec == LimitSpec(Algorithm.SLIDING_WINDOW, 1, 1_000_000, 1)


def test_token_bucket_burst_is_capacity() -> None:
    rule = Rule("r", "10/s", algorithm="token_bucket", burst=25)
    assert rule.capacity == 25
    assert rule.spec.capacity == 25
    assert rule.spec.limit == 10


def test_normalisation() -> None:
    rule = Rule("r", Rate(5, 1_000_000), methods=["get", "Post"], paths=["/a*"], key="user_or_ip")
    assert rule.methods == frozenset({"GET", "POST"})
    assert rule.paths == ("/a*",)
    assert rule.key == ("user", "ip")
    assert not Rule("r", "1/s", action="log").enforced


def test_rules_are_immutable() -> None:
    rule = Rule("r", "1/s")
    with pytest.raises(AttributeError):
        rule.name = "x"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("args", "kwargs"),
    [
        (("",), {"rate": "1/s"}),
        (("   ",), {"rate": "1/s"}),
        (("x" * 201,), {"rate": "1/s"}),
        (("bad\nname",), {"rate": "1/s"}),
        ((123,), {"rate": "1/s"}),
        (("r",), {}),
        (("r",), {"limit": 5}),
        (("r", "1/s"), {"limit": 5}),
        (("r",), {"rate": 5}),
        (("r", "1/s"), {"algorithm": "leaky"}),
        (("r", "1/s"), {"burst": 5}),
        (("r", "1/s"), {"algorithm": "token_bucket", "burst": 0}),
        (("r", "1/s"), {"algorithm": "token_bucket", "burst": True}),
        (("r", "5/s"), {"cost": 6}),
        (("r", "5/s"), {"cost": 0}),
        (("r", "5/s"), {"cost": True}),
        (("r", "5/s"), {"cost": "1"}),
        (("r", "1/s"), {"on_missing": "ignore"}),
        (("r", "1/s"), {"fail_mode": "maybe"}),
        (("r", "1/s"), {"action": "block"}),
        (("r", "1/s"), {"when": True}),
        (("r", "1/s"), {"methods": "GET"}),
        (("r", "1/s"), {"paths": "/x"}),
        (("r", "1/s"), {"key": "email"}),
        (("r", "1/s"), {"key": []}),
        (("r", "1/s"), {"key": 5}),
        (("r", "1/s"), {"key": ["ip", 5]}),
    ],
)
def test_validation(args: tuple[object, ...], kwargs: dict[str, object]) -> None:
    with pytest.raises(ConfigurationError):
        Rule(*args, **kwargs)  # type: ignore[arg-type]


def test_static_cost_may_use_token_bucket_burst() -> None:
    assert Rule("r", "5/s", algorithm="token_bucket", burst=10, cost=10).cost == 10


def test_any_printable_name_is_allowed() -> None:
    assert Rule("GET /items/{id}", "1/s").name == "GET /items/{id}"
