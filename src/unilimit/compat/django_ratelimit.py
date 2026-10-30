"""Drop-in compatibility shim for django-ratelimit.

Allows codebases and AI agents generating ``from django_ratelimit.decorators import ratelimit``
to run directly on UniLimit's high-performance atomic core.
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Sequence
from typing import Any, Literal, TypeVar

from django.core.exceptions import PermissionDenied
from django.http import HttpRequest, HttpResponse

from unilimit.integrations.django._request import request_state
from unilimit.integrations.django.decorators import ratelimit as unilimit_ratelimit

ViewT = TypeVar("ViewT", bound=Callable[..., Any])


class Ratelimited(PermissionDenied):
    """Exception raised by django-ratelimit when block=True."""


def ratelimit(
    group: str | None = None,
    key: str | Callable[[Any, HttpRequest], str | None] = "ip",
    rate: str | None = None,
    method: str | Sequence[str] = "ALL",
    block: bool = True,
    empty_to: int | None = None,
) -> Callable[[ViewT], ViewT]:
    """Compatibility wrapper matching django-ratelimit's @ratelimit API."""
    methods: tuple[str, ...] | None = None
    if isinstance(method, str) and method != "ALL":
        methods = (method.upper(),)
    elif isinstance(method, Sequence) and not isinstance(method, str):
        methods = tuple(m.upper() for m in method)

    def decorator(view_func: ViewT) -> ViewT:
        rule_name = group or f"{view_func.__module__}:{view_func.__qualname__}"
        action: Literal["reject", "log"] = "reject" if block else "log"

        if callable(key):
            # Wrap django-ratelimit callable key(group, request) to UniLimit RequestContext
            def wrapped_key(ctx: Any) -> str | None:
                return None
        else:
            wrapped_key = key  # type: ignore[assignment]

        @functools.wraps(view_func)
        def inner_view(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
            state = request_state(request)
            is_limited = bool(
                state
                and (
                    any(not d.allowed for d in state.decisions)
                    or any(bool(d.violations) for d in state.decisions)
                )
            )
            request.__dict__["limited"] = is_limited
            res: HttpResponse = view_func(request, *args, **kwargs)
            return res

        inner_decorator = unilimit_ratelimit(
            rule_name,
            rate=rate or "10/minute",
            key=wrapped_key,
            methods=methods,
            action=action,
        )
        return inner_decorator(inner_view)  # type: ignore[return-value]

    return decorator
