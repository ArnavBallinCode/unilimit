from __future__ import annotations

from django.apps import AppConfig, apps


class UniLimitConfig(AppConfig):
    """Optional app: adding ``"unilimit.integrations.django"`` to ``INSTALLED_APPS``
    enables UniLimit's system checks (``manage.py check``)."""

    name = "unilimit.integrations.django"
    label = "unilimit"
    verbose_name = "UniLimit"

    def ready(self) -> None:
        from unilimit.integrations.django import checks  # noqa: F401  (registers checks)

        if apps.is_installed("rest_framework"):
            from unilimit.integrations.drf import checks as drf_checks  # noqa: F401
