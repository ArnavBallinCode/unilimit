"""Django REST Framework integration for UniLimit.

Requires the ``drf`` extra: ``pip install "unilimit[drf]"``. Configure the limiter
with ``settings.UNILIMIT`` as for the Django integration.

```python
REST_FRAMEWORK = {
    "DEFAULT_THROTTLE_CLASSES": [
        "unilimit.integrations.drf.UniLimitAnonRateThrottle",
        "unilimit.integrations.drf.UniLimitUserRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {"anon": "100/day", "user": "1000/day"},
}
```
"""

from __future__ import annotations

try:
    import rest_framework as _rest_framework  # noqa: F401
except ImportError as exc:  # pragma: no cover - exercised only without the extra
    raise ImportError(
        "The DRF integration requires Django REST framework. Install it with: "
        'pip install "unilimit[drf]"'
    ) from exc

from unilimit.integrations.drf.throttling import (
    ServiceUnavailable,
    UniLimitAnonRateThrottle,
    UniLimitBaseThrottle,
    UniLimitRateThrottle,
    UniLimitScopedRateThrottle,
    UniLimitThrottle,
    UniLimitUserRateThrottle,
    parse_drf_rate,
)

__all__ = [
    "ServiceUnavailable",
    "UniLimitAnonRateThrottle",
    "UniLimitBaseThrottle",
    "UniLimitRateThrottle",
    "UniLimitScopedRateThrottle",
    "UniLimitThrottle",
    "UniLimitUserRateThrottle",
    "parse_drf_rate",
]
