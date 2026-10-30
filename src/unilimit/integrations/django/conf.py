"""The ``UNILIMIT`` setting: which limiter to use and how rejections look.

```python
UNILIMIT = {
    "LIMITER": "myproject.ratelimits.limiter",  # a Limiter, or a dotted path to one
    # or: "URL": "redis://localhost:6379/0", "OPTIONS": {...} for Limiter.from_url
    "RESPONSE": ResponseConfig(...),  # or a dotted path to one
    "REJECTION_VIEW": "myproject.views.rate_limited",  # (request, decision) -> HttpResponse
}
```

Everything is loaded on first use and cached. The cache is cleared when the setting
changes (``override_settings`` in tests).
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.signals import setting_changed
from django.http import HttpRequest, HttpResponseBase
from django.utils.module_loading import import_string

from unilimit.core.errors import ConfigurationError
from unilimit.core.limiter import Limiter
from unilimit.core.result import Decision
from unilimit.http import ResponseConfig

SETTING = "UNILIMIT"
_KEYS = frozenset({"LIMITER", "URL", "OPTIONS", "RESPONSE", "REJECTION_VIEW"})

RejectionView = Callable[[HttpRequest, Decision], HttpResponseBase]


@dataclass(frozen=True)
class Config:
    limiter: Limiter
    response: ResponseConfig
    rejection_view: RejectionView | None
    owns_limiter: bool
    """True when the limiter was built from ``URL``, so UniLimit may close it."""


_lock = threading.Lock()
_config: Config | None = None


def _load(value: Any, what: str) -> Any:
    if isinstance(value, str):
        try:
            return import_string(value)
        except ImportError as exc:
            raise ImproperlyConfigured(f"UNILIMIT[{what!r}]: cannot import {value!r}") from exc
    return value


def load_config() -> Config:
    """Read and validate ``settings.UNILIMIT`` (uncached)."""
    raw = getattr(settings, SETTING, None)
    if raw is None:
        raise ImproperlyConfigured(
            "UniLimit is not configured: set UNILIMIT = {'LIMITER': ...} or "
            "{'URL': 'redis://...'} in your settings"
        )
    if not isinstance(raw, Mapping):
        raise ImproperlyConfigured("settings.UNILIMIT must be a dict")
    unknown = sorted(set(raw) - _KEYS)
    if unknown:
        raise ImproperlyConfigured(
            f"unknown UNILIMIT keys {unknown}; expected some of {sorted(_KEYS)}"
        )
    if ("LIMITER" in raw) == ("URL" in raw):
        raise ImproperlyConfigured("UNILIMIT needs exactly one of 'LIMITER' or 'URL'")
    if "OPTIONS" in raw and "URL" not in raw:
        raise ImproperlyConfigured("UNILIMIT['OPTIONS'] applies to 'URL' only")

    owns = "URL" in raw
    if owns:
        options = raw.get("OPTIONS", {})
        if not isinstance(options, Mapping):
            raise ImproperlyConfigured("UNILIMIT['OPTIONS'] must be a dict")
        try:
            limiter: Any = Limiter.from_url(raw["URL"], **options)
        except (ConfigurationError, TypeError) as exc:
            raise ImproperlyConfigured(f"UNILIMIT['URL']/['OPTIONS']: {exc}") from exc
    else:
        limiter = _load(raw["LIMITER"], "LIMITER")
        if not isinstance(limiter, Limiter):
            raise ImproperlyConfigured("UNILIMIT['LIMITER'] must be a Limiter or its path")

    response = _load(raw.get("RESPONSE"), "RESPONSE") or ResponseConfig()
    if not isinstance(response, ResponseConfig):
        raise ImproperlyConfigured("UNILIMIT['RESPONSE'] must be a ResponseConfig or its path")
    view = _load(raw.get("REJECTION_VIEW"), "REJECTION_VIEW")
    if view is not None and not callable(view):
        raise ImproperlyConfigured("UNILIMIT['REJECTION_VIEW'] must be a callable")
    return Config(limiter, response, view, owns)


def get_config() -> Config:
    global _config
    config = _config
    if config is None:
        with _lock:
            if _config is None:
                _config = load_config()
            config = _config
    return config


def get_limiter() -> Limiter:
    """The limiter configured in ``settings.UNILIMIT``."""
    return get_config().limiter


def reset() -> None:
    """Forget the loaded configuration; closes the sync client of a ``URL`` limiter.

    Async clients are bound to their event loops and are released with them.
    """
    global _config
    with _lock:
        old, _config = _config, None
    if old is not None and old.owns_limiter:
        old.limiter.close()


def _on_setting_changed(*, setting: str, **kwargs: Any) -> None:
    if setting == SETTING:
        reset()


setting_changed.connect(_on_setting_changed, dispatch_uid="unilimit.integrations.django.conf")


__all__ = ["Config", "get_config", "get_limiter", "load_config", "reset"]
