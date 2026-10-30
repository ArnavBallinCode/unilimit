from datetime import timedelta

import pytest

from unilimit import ConfigurationError, Rate
from unilimit.core.rate import parse_duration

S = 1_000_000


@pytest.mark.parametrize(
    ("text", "limit", "window_us"),
    [
        ("100/minute", 100, 60 * S),
        ("100/m", 100, 60 * S),
        ("10/5s", 10, 5 * S),
        ("5 per 30 seconds", 5, 30 * S),
        ("1000/day", 1000, 86_400 * S),
        ("3/2h", 3, 7_200 * S),
        ("1/500ms", 1, 500_000),
        ("  7 / 1 MIN ", 7, 60 * S),
        ("20/second", 20, S),
    ],
)
def test_parse_rate(text: str, limit: int, window_us: int) -> None:
    assert Rate.parse(text) == Rate(limit, window_us)


@pytest.mark.parametrize(
    "text",
    ["", "100", "/minute", "100/", "100/fortnight", "-1/m", "0/m", "1.5/m", "100/0s", "a/b"],
)
def test_parse_rate_rejects_malformed(text: str) -> None:
    with pytest.raises(ConfigurationError):
        Rate.parse(text)


def test_parse_rate_requires_string() -> None:
    with pytest.raises(ConfigurationError):
        Rate.parse(100)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1m", 60 * S),
        ("hour", 3_600 * S),
        (30, 30 * S),
        (0.5, 500_000),
        (timedelta(minutes=2), 120 * S),
        (timedelta(milliseconds=1), 1_000),
    ],
)
def test_parse_duration(value: object, expected: int) -> None:
    assert parse_duration(value) == expected  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "value",
    [0, -1, True, "0s", "999us", float("nan"), float("inf"), timedelta(0), "367d", [1], 0.0001],
)
def test_parse_duration_rejects_invalid(value: object) -> None:
    with pytest.raises(ConfigurationError):
        parse_duration(value)  # type: ignore[arg-type]


def test_rate_validates_limit_and_resolution() -> None:
    with pytest.raises(ConfigurationError):
        Rate(0, S)
    with pytest.raises(ConfigurationError):
        Rate(True, S)
    # More than one event per microsecond is not representable.
    with pytest.raises(ConfigurationError, match="rate too high"):
        Rate(2_000, 1_000)
    assert Rate(1_000, 1_000).limit == 1_000


def test_rate_accessors_and_str() -> None:
    rate = Rate.parse("100/minute")
    assert rate.window == timedelta(minutes=1)
    assert rate.window_seconds == 60.0
    assert str(rate) == "100/1m"
    assert str(Rate(1, 1_500)) == "1/1500us"
    assert str(Rate(1, 2_000)) == "1/2ms"
    assert Rate.of(5, "1h") == Rate(5, 3_600 * S)


# -- compatibility with the `limits` grammar (slowapi, Flask-Limiter) -------------------

LIMITS_STRINGS = [
    "1/second",
    "10/minute",
    "1 per hour",
    "100/day",
    "1/month",
    "2 per year",
    "5 per 2 minutes",
    "10/second;100/day",
    "10/second, 100/day",
    "1/second|5/minute|100/hour",
    "3 per 10 seconds; 1000 per day",
]


@pytest.mark.parametrize("text", LIMITS_STRINGS)
def test_rate_strings_mean_what_they_mean_in_limits(text: str) -> None:
    """Strings copied from slowapi / Flask-Limiter configurations keep their meaning."""
    limits = pytest.importorskip("limits")
    theirs = [(item.amount, item.get_expiry() * S) for item in limits.parse_many(text)]
    ours = [(r.limit, r.window_us) for r in Rate.parse_many(text)]
    assert ours == theirs


def test_month_and_year_follow_limits_not_the_calendar() -> None:
    assert Rate.parse("1/month").window_us == 30 * 86_400 * S
    assert Rate.parse("1/year").window_us == 360 * 86_400 * S


def test_several_rates_need_the_many_apis() -> None:
    with pytest.raises(ConfigurationError, match="several rates"):
        Rate.parse("1/second;2/day")
    with pytest.raises(ConfigurationError):
        Rate.parse_many("")
    assert Rate.parse_many("5/s") == (Rate(5, S),)
