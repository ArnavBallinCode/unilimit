"""Django system checks (``manage.py check``) for UniLimit's configuration."""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.core.checks import CheckMessage, Error, Warning, register
from django.core.exceptions import ImproperlyConfigured
from django.utils.module_loading import import_string

from unilimit.core.errors import ConfigurationError
from unilimit.integrations.django.conf import SETTING, load_config

AUTH_MIDDLEWARE = "django.contrib.auth.middleware.AuthenticationMiddleware"


def _middleware_index(cls: type) -> int | None:
    for index, path in enumerate(getattr(settings, "MIDDLEWARE", None) or ()):
        try:
            if import_string(path) is cls:
                return index
        except ImportError:
            continue
    return None


@register("unilimit")
def check_settings(app_configs: Any = None, **kwargs: Any) -> list[CheckMessage]:
    from unilimit.integrations.django.middleware import UniLimitMiddleware

    if getattr(settings, SETTING, None) is None:
        return [
            Error(
                "UniLimit is installed but settings.UNILIMIT is missing.",
                hint="Set UNILIMIT = {'URL': 'redis://...'} or {'LIMITER': 'path.to.limiter'}.",
                id="unilimit.E001",
            )
        ]
    try:
        config = load_config()
    except (ImproperlyConfigured, ConfigurationError) as exc:
        return [Error(f"Invalid UNILIMIT setting: {exc}", id="unilimit.E001")]

    messages: list[CheckMessage] = []
    limiter = config.limiter
    ours = _middleware_index(UniLimitMiddleware)
    middleware = list(getattr(settings, "MIDDLEWARE", None) or ())
    if ours is None and limiter.global_rules:
        messages.append(
            Warning(
                "The Limiter has global rules but UniLimitMiddleware is not in MIDDLEWARE, "
                "so they are not enforced.",
                hint="Add 'unilimit.integrations.django.UniLimitMiddleware' to MIDDLEWARE.",
                id="unilimit.W002",
            )
        )
    if ours is not None and limiter.needs_user(include_global=True):
        auth = middleware.index(AUTH_MIDDLEWARE) if AUTH_MIDDLEWARE in middleware else None
        if auth is None or auth > ours:
            messages.append(
                Warning(
                    "UniLimitMiddleware runs before AuthenticationMiddleware, but global "
                    "rules need the user: every request will look anonymous to them.",
                    hint="Move UniLimitMiddleware after AuthenticationMiddleware.",
                    id="unilimit.W001",
                )
            )
    return messages
