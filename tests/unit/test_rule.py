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


def test_full_refill_time_is_bounded() -> None:
    # 10 years exactly is allowed; anything longer would overflow Redis TTLs and leave
    # the exact-integer range of IEEE doubles.
    assert Rule("ok", "1/day", algorithm="token_bucket", burst=3660).capacity == 3660
    with pytest.raises(ConfigurationError, match="longer than 10 years"):
        Rule("huge", "1/day", algorithm="token_bucket", burst=3661)
    with pytest.raises(ConfigurationError, match="longer than 10 years"):
        Rule("absurd", limit=1, window="366d", algorithm="token_bucket", burst=291_673)


def test_limit_spec_validates_invariants() -> None:
    for bad in (
        (Algorithm.FIXED_WINDOW, 0, 1_000, 0),
        (Algorithm.FIXED_WINDOW, 2_000, 1_000, 2_000),
        (Algorithm.SLIDING_WINDOW, 5, 1_000_000, 6),
        (Algorithm.TOKEN_BUCKET, 1, 86_400_000_000, 10**6),
    ):
        with pytest.raises(ValueError, match=r"positive integer|rate too high|capacity|10 years"):
            LimitSpec(*bad)


def test_rule_many_from_a_limits_string() -> None:
    single = Rule.many("api", "10/second", key="user")
    assert [r.name for r in single] == ["api"]
    rules = Rule.many("api", "10/second;1000/day", key="user", algorithm="fixed_window")
    assert [r.name for r in rules] == ["api:10/1s", "api:1000/1d"]  # one counter each
    assert all(r.key == ("user",) and r.algorithm == "fixed_window" for r in rules)
    with pytest.raises(ConfigurationError, match="several rates"):
        Rule("api", "10/second;1000/day")
