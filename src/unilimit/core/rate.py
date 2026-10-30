"""Rate and duration parsing.

Grammar::

    rate     := INT ("/" | "per") [INT] UNIT      e.g. "100/minute", "10/5s", "5 per 30 seconds"
    duration := INT UNIT | UNIT                   e.g. "1m", "30s", "500ms", "hour"

Durations are stored as integer microseconds.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta

from unilimit.core.errors import ConfigurationError

US_PER_SECOND = 1_000_000

_UNITS_US: dict[str, int] = {}
for _names, _us in (
    (("ms", "msec", "millisecond", "milliseconds"), 1_000),
    (("s", "sec", "secs", "second", "seconds"), US_PER_SECOND),
    (("m", "min", "mins", "minute", "minutes"), 60 * US_PER_SECOND),
    (("h", "hr", "hrs", "hour", "hours"), 3_600 * US_PER_SECOND),
    (("d", "day", "days"), 86_400 * US_PER_SECOND),
    # As in the `limits` library (and so in slowapi / Flask-Limiter): fixed lengths, not
    # calendar months or years. Rate strings migrated from those libraries keep their
    # meaning.
    (("month", "months"), 30 * 86_400 * US_PER_SECOND),
    (("y", "year", "years"), 360 * 86_400 * US_PER_SECOND),
):
    for _name in _names:
        _UNITS_US[_name] = _us

_DURATION_RE = re.compile(r"^\s*(\d+)?\s*([a-zA-Z]+)\s*$")
_RATE_RE = re.compile(r"^\s*(\d+)\s*(?:/|\s+per\s+)\s*(.+?)\s*$", re.IGNORECASE)
_RATE_SEPARATORS = re.compile(r"[,;|]")

MIN_WINDOW_US = 1_000
"""Smallest accepted window: 1 ms."""

MAX_WINDOW_US = 366 * 86_400 * US_PER_SECOND
"""Largest accepted window: 366 days."""

Duration = str | int | float | timedelta
"""Accepted duration inputs: ``"1m"``, seconds as ``int``/``float``, or ``timedelta``."""


def parse_duration(value: Duration) -> int:
    """Convert a duration to integer microseconds, validating its range."""
    if isinstance(value, bool):
        raise ConfigurationError(f"invalid duration: {value!r}")
    if isinstance(value, timedelta):
        us = (value.days * 86_400 + value.seconds) * US_PER_SECOND + value.microseconds
    elif isinstance(value, int):
        us = value * US_PER_SECOND
    elif isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ConfigurationError(f"invalid duration: {value!r}")
        us = round(value * US_PER_SECOND)
    elif isinstance(value, str):
        match = _DURATION_RE.match(value)
        unit_us = _UNITS_US.get(match.group(2).lower()) if match else None
        if match is None or unit_us is None:
            raise ConfigurationError(
                f"invalid duration {value!r}; expected e.g. '30s', '1m', '1h', '1d' or '500ms'"
            )
        us = int(match.group(1) or 1) * unit_us
    else:
        raise ConfigurationError(f"invalid duration type: {type(value).__name__}")
    if not MIN_WINDOW_US <= us <= MAX_WINDOW_US:
        raise ConfigurationError(f"duration {value!r} must be between 1ms and 366 days")
    return us


@dataclass(frozen=True, slots=True)
class Rate:
    """``limit`` events per ``window_us`` microseconds."""

    limit: int
    window_us: int

    def __post_init__(self) -> None:
        if isinstance(self.limit, bool) or not isinstance(self.limit, int) or self.limit < 1:
            raise ConfigurationError(f"limit must be a positive integer, got {self.limit!r}")
        if not isinstance(self.window_us, int) or not (
            MIN_WINDOW_US <= self.window_us <= MAX_WINDOW_US
        ):
            raise ConfigurationError("window must be between 1ms and 366 days")
        if self.window_us < self.limit:
            # Emission interval below 1µs cannot be represented by any backend clock.
            raise ConfigurationError(
                f"rate too high: {self.limit} per {self.window_us}µs exceeds 1 event per µs"
            )

    @classmethod
    def parse(cls, text: str) -> Rate:
        """Parse ``"100/minute"``, ``"10/5s"`` or ``"5 per 30 seconds"``."""
        if not isinstance(text, str):
            raise ConfigurationError(f"rate must be a string, got {type(text).__name__}")
        if _RATE_SEPARATORS.search(text):
            raise ConfigurationError(
                f"{text!r} contains several rates; use Rule.many() or Rate.parse_many()"
            )
        match = _RATE_RE.match(text)
        if match is None:
            raise ConfigurationError(
                f"invalid rate {text!r}; expected e.g. '100/minute', '10/5s', '1000/day'"
            )
        return cls(int(match.group(1)), parse_duration(match.group(2)))

    @classmethod
    def parse_many(cls, text: str) -> tuple[Rate, ...]:
        """Parse one or more rates separated by ``;``, ``,`` or ``|``.

        This is the multi-limit syntax of the ``limits`` library, as used by slowapi and
        Flask-Limiter: ``"10/second;1000/day"``.
        """
        if not isinstance(text, str) or not text.strip():
            raise ConfigurationError(f"invalid rate list {text!r}")
        return tuple(cls.parse(part) for part in _RATE_SEPARATORS.split(text))

    @classmethod
    def of(cls, limit: int, window: Duration) -> Rate:
        return cls(limit, parse_duration(window))

    @property
    def window(self) -> timedelta:
        return timedelta(microseconds=self.window_us)

    @property
    def window_seconds(self) -> float:
        return self.window_us / US_PER_SECOND

    def __str__(self) -> str:
        return f"{self.limit}/{_format_duration(self.window_us)}"


def _format_duration(us: int) -> str:
    for suffix, unit in (("d", 86_400), ("h", 3_600), ("m", 60), ("s", 1)):
        unit_us = unit * US_PER_SECOND
        if us % unit_us == 0:
            return f"{us // unit_us}{suffix}"
    return f"{us // 1_000}ms" if us % 1_000 == 0 else f"{us}us"
