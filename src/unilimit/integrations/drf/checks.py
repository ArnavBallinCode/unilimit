"""System checks for the DRF throttles (registered when ``rest_framework`` is installed)."""

from __future__ import annotations

from typing import Any

from django.core.checks import CheckMessage, Error, Warning, register
from rest_framework.settings import api_settings
from rest_framework.throttling import ScopedRateThrottle, SimpleRateThrottle

from unilimit.integrations.drf.throttling import (
    UniLimitBaseThrottle,
    UniLimitRateThrottle,
    UniLimitScopedRateThrottle,
)

_PERIOD_WORDS = {
    "s": {"s", "sec", "secs", "second", "seconds"},
    "m": {"m", "min", "mins", "minute", "minutes"},
    "h": {"h", "hr", "hrs", "hour", "hours"},
    "d": {"d", "day", "days"},
}
_BY_VIEW = "<view.throttle_scope>"


def _scope(cls: type) -> str | None:
    if issubclass(cls, UniLimitScopedRateThrottle | ScopedRateThrottle):
        return _BY_VIEW
    scope = getattr(cls, "scope", None)
    return scope if isinstance(scope, str) else None


@register("unilimit")
def check_drf_settings(app_configs: Any = None, **kwargs: Any) -> list[CheckMessage]:
    messages: list[CheckMessage] = []
    classes: list[Any] = list(api_settings.DEFAULT_THROTTLE_CLASSES or ())
    ours = {
        _scope(c) for c in classes if isinstance(c, type) and issubclass(c, UniLimitRateThrottle)
    }
    builtin = {
        _scope(c)
        for c in classes
        if isinstance(c, type)
        and issubclass(c, SimpleRateThrottle)
        and not issubclass(c, UniLimitBaseThrottle)
    }
    clash = sorted(s for s in ours & builtin if s is not None)
    if clash:
        messages.append(
            Error(
                f"DEFAULT_THROTTLE_CLASSES throttles scope(s) {clash} with both a DRF "
                "built-in throttle and a UniLimit throttle: requests would be counted twice.",
                hint="Remove the built-in throttle; its UniLimit equivalent replaces it.",
                id="unilimit.E002",
            )
        )
    rates: dict[str, Any] = dict(api_settings.DEFAULT_THROTTLE_RATES or {})
    for scope, rate in rates.items():
        if not isinstance(rate, str) or rate.count("/") != 1:
            continue
        period = rate.split("/")[1].strip()
        word = period.lstrip("0123456789").strip().lower()
        if period[:1] in _PERIOD_WORDS and word not in _PERIOD_WORDS[period[0]]:
            messages.append(
                Warning(
                    f"Throttle rate {rate!r} for scope {scope!r}: DRF reads only the first "
                    f"letter of the period, so this means per {period[0]!r}, not per "
                    f"{period!r}.",
                    hint="Use second, minute, hour or day (or a UniLimit rate such as "
                    "'5/30s' with the UniLimit throttles).",
                    id="unilimit.W003",
                )
            )
    return messages
