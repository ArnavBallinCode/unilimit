"""Compatibility adapter for django-ratelimit's ``@ratelimit`` decorator.

``from unilimit.compat.django_ratelimit import ratelimit`` accepts django-ratelimit's
arguments (``group``, ``key``, ``rate``, ``method``, ``block``) and enforces them through
UniLimit's Django integration, so configure ``UNILIMIT`` settings as for the native
decorator. Differences from upstream are listed in ``docs/migration.md``.
"""

from __future__ import annotations

import functools
import inspect
import re
from collections.abc import Callable, Sequence
from typing import Any, Literal, TypeVar, cast

from django.core.exceptions import PermissionDenied
from django.utils.module_loading import import_string

from unilimit.core.context import RequestContext
from unilimit.core.errors import ConfigurationError
from unilimit.core.identity import KeyFunc, KeySpec
from unilimit.core.rate import Rate
from unilimit.integrations.django._request import http_request, request_state
from unilimit.integrations.django.decorators import _is_async_class_view_method
from unilimit.integrations.django.decorators import ratelimit as unilimit_ratelimit

ViewT = TypeVar("ViewT", bound=Callable[..., Any])

RateValue = str | tuple[int, int] | None
RateArg = RateValue | Callable[[str, Any], RateValue]
KeyArg = str | Callable[[str, Any], Any]

ALL: tuple[None] = (None,)
"""Every HTTP method (django-ratelimit's ``ALL``)."""
UNSAFE: tuple[str, ...] = ("DELETE", "PATCH", "POST", "PUT")
"""The methods that change state (django-ratelimit's ``UNSAFE``)."""

_SIMPLE_KEYS = frozenset({"ip", "user", "user_or_ip"})
# django-ratelimit's grammar: "5/m", "100/2h"; no unit means seconds. Like upstream, only
# the first letter of the period counts, so "5/month" is five per minute.
_RATE_RE = re.compile(r"(\d+)/(\d*)([smhd])?")
_PERIODS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


class Ratelimited(PermissionDenied):
    """django-ratelimit's exception, for code that imports or catches it.

    The adapter itself answers a blocked request with UniLimit's rejection response
    (429, or ``UNILIMIT["REJECTION_VIEW"]``) instead of raising this.
    """


def ratelimit(
    group: str | None = None,
    key: KeyArg = "ip",
    rate: RateArg = None,
    method: str | Sequence[str | None] | None = ALL,
    block: bool = True,
) -> Callable[[ViewT], ViewT]:
    """django-ratelimit's ``@ratelimit``, enforced by UniLimit.

    - ``key``: ``"ip"``, ``"user"``, ``"user_or_ip"``, ``"header:<name>"``,
      ``"get:<name>"``, ``"post:<name>"``, a dotted path to a callable, or a callable
      ``key(group, request)``. As upstream, an empty value is one shared bucket.
    - ``rate``: ``"5/m"``, ``"100/2h"``, ``(count, seconds)``, a dotted path or a callable
      ``rate(group, request)``. A rate resolving to ``None`` leaves the request unlimited.
    - ``block=False`` counts without rejecting and sets ``request.limited``.

    Each rate has its own counter, so stacked decorators on one view are independent
    limits, as upstream. Sync and async views are supported.
    """
    if rate is None:
        raise ConfigurationError("ratelimit needs rate=, for example rate='5/m'")
    methods = _methods(method)
    action: Literal["reject", "log"] = "reject" if block else "log"
    key_for = _key_factory(key)
    rate_fn = _rate_callable(rate)
    static = None if rate_fn is not None or callable(rate) else _parse_rate(rate)
    # method_decorator() decorates again on every request: build each rule only once.
    natives: dict[tuple[str, Rate], Callable[[Callable[..., Any]], Callable[..., Any]]] = {}

    def native(name: str, parsed: Rate) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        decorate = natives.get((name, parsed))
        if decorate is None:
            decorate = unilimit_ratelimit(
                f"{name}:{parsed}",
                rate=parsed,
                key=key_for(name),
                methods=methods,
                action=action,
            )
            natives[(name, parsed)] = decorate
        return decorate

    def decorator(view: ViewT) -> ViewT:
        name = group or _group_name(view)
        is_async = inspect.iscoroutinefunction(view) or _is_async_class_view_method(view)
        inner = _async_inner(view) if is_async else _sync_inner(view)

        if static is not None:
            return functools.wraps(view)(native(name, static)(inner))  # type: ignore[return-value]

        assert rate_fn is not None
        limited_views: dict[Rate, Callable[..., Any]] = {}

        def resolve(request: Any) -> Callable[..., Any]:
            value = rate_fn(name, request)
            if value is None:
                request.limited = getattr(request, "limited", False)
                return view
            parsed = _parse_rate(value)
            limited = limited_views.get(parsed)
            if limited is None:
                limited = limited_views[parsed] = native(name, parsed)(inner)
            return limited

        if is_async:

            @functools.wraps(view)
            async def async_wrapper(request: Any, *args: Any, **kwargs: Any) -> Any:
                response = resolve(request)(request, *args, **kwargs)
                return await response if inspect.isawaitable(response) else response

            return async_wrapper  # type: ignore[return-value]

        @functools.wraps(view)
        def sync_wrapper(request: Any, *args: Any, **kwargs: Any) -> Any:
            return resolve(request)(request, *args, **kwargs)

        return sync_wrapper  # type: ignore[return-value]

    return decorator


ratelimit.ALL = ALL  # type: ignore[attr-defined]
ratelimit.UNSAFE = UNSAFE  # type: ignore[attr-defined]


def _sync_inner(view: Callable[..., Any]) -> Callable[..., Any]:
    def inner(request: Any, *args: Any, **kwargs: Any) -> Any:
        _mark_limited(request)
        return view(request, *args, **kwargs)

    return inner


def _async_inner(view: Callable[..., Any]) -> Callable[..., Any]:
    async def inner(request: Any, *args: Any, **kwargs: Any) -> Any:
        _mark_limited(request)
        response = view(request, *args, **kwargs)
        return await response if inspect.isawaitable(response) else response

    return inner


def _mark_limited(request: Any) -> None:
    # As upstream: limited by this decorator, or by an earlier one on the same request.
    decision = request_state(request).decisions[-1]
    limited = not decision.allowed or bool(decision.violations)
    request.limited = limited or getattr(request, "limited", False)


def _methods(method: str | Sequence[str | None] | None) -> tuple[str, ...] | None:
    if method is None or method == ALL or method == "ALL":
        return None
    if isinstance(method, str):
        return (method.upper(),)
    return tuple(m.upper() for m in method if m is not None)


def _group_name(view: Callable[..., Any]) -> str:
    """The default group, computed like django-ratelimit's."""
    fn: Any = view.func if isinstance(view, functools.partial) else view
    parts = []
    if hasattr(fn, "__module__"):
        parts.append(fn.__module__)
    if hasattr(fn, "__self__"):
        parts.append(fn.__self__.__class__.__name__)
    parts.append(fn.__qualname__)
    return ".".join(parts)


def _rate_callable(rate: RateArg) -> Callable[[str, Any], RateValue] | None:
    if callable(rate):
        return rate
    if isinstance(rate, str) and "." in rate:
        return _lazy_import(rate)
    return None


def _parse_rate(rate: str | tuple[int, int]) -> Rate:
    if isinstance(rate, tuple):
        count, seconds = rate
        return Rate.of(count, seconds)
    match = _RATE_RE.match(rate) if isinstance(rate, str) else None
    if match is None:
        raise ConfigurationError(f"invalid rate {rate!r}; expected e.g. '5/m' or (5, 60)")
    seconds = _PERIODS[match[3] or "s"] * int(match[2] or 1)
    return Rate.of(int(match[1]), seconds)


def _key_factory(key: KeyArg) -> Callable[[str], KeySpec]:
    """Validate ``key`` now and return the UniLimit key for a group."""
    if callable(key):
        return functools.partial(_bucket, fn=key)
    if not isinstance(key, str) or not key:
        raise ConfigurationError(f"invalid ratelimit key {key!r}")
    if key in _SIMPLE_KEYS:
        return lambda group: key
    if ":" in key:
        accessor, name = key.split(":", 1)
        if accessor == "header":
            meta = "HTTP_" + name.replace("-", "_").upper()
            return functools.partial(
                _bucket, fn=lambda _, request: http_request(request).META.get(meta, "")
            )
        if accessor == "get":
            return functools.partial(
                _bucket, fn=lambda _, request: http_request(request).GET.get(name, "")
            )
        if accessor == "post":
            return functools.partial(
                _bucket, fn=lambda _, request: http_request(request).POST.get(name, "")
            )
        raise ConfigurationError(f"unknown ratelimit key {key!r}")
    if "." in key:
        return functools.partial(_bucket, fn=_lazy_import(key))
    raise ConfigurationError(f"could not understand ratelimit key {key!r}")


def _bucket(group: str, fn: Callable[[str, Any], Any]) -> KeyFunc:
    """A UniLimit key for django-ratelimit values; the prefix makes the empty value,
    which upstream treats as one shared bucket, a valid identity."""

    def key(ctx: RequestContext) -> str | None:
        value = fn(group, ctx.raw)
        return None if value is None else f"={value}"

    return key


def _lazy_import(path: str) -> Callable[[str, Any], Any]:
    # Resolved on first use: the views module may be imported before the target's module.
    @functools.cache
    def target() -> Callable[[str, Any], Any]:
        fn = import_string(path)
        if not callable(fn):
            raise ConfigurationError(f"{path!r} is not callable")
        return cast(Callable[[str, Any], Any], fn)

    def call(group: str, request: Any) -> Any:
        return target()(group, request)

    return call


__all__ = ["ALL", "UNSAFE", "Ratelimited", "ratelimit"]
