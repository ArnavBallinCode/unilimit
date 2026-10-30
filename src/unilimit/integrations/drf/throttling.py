"""DRF throttles backed by UniLimit.

They implement DRF's ``BaseThrottle`` interface, so they go in ``throttle_classes`` or
``DEFAULT_THROTTLE_CLASSES`` and reject with DRF's own ``Throttled`` exception (429
with ``Retry-After``). Unlike DRF's built-in throttles they share counters across
processes atomically, and every UniLimit throttle of a view is consumed in **one**
atomic batch: a request rejected by one throttle consumes nothing from the others.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any, ClassVar

from django.core.exceptions import ImproperlyConfigured
from rest_framework.exceptions import APIException, PermissionDenied
from rest_framework.settings import api_settings
from rest_framework.throttling import BaseThrottle, ScopedRateThrottle, SimpleRateThrottle

from unilimit.core.errors import ConfigurationError
from unilimit.core.identity import KeySpec
from unilimit.core.limiter import Limiter
from unilimit.core.rate import Rate
from unilimit.core.result import Decision, Reason
from unilimit.core.rule import AlgorithmName, FailMode, OnMissing, Predicate, Rule
from unilimit.integrations._common import resolve_rules
from unilimit.integrations.django._request import (
    build_context,
    http_request,
    request_state,
    sync_user_id,
    warn_once,
)
from unilimit.integrations.django.conf import get_config

_DRF_PERIODS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_DRF_RATE = re.compile(r"^\s*(\d+)\s*/\s*([A-Za-z]+)\s*$")
_EVALUATION_ATTR = "_unilimit_drf"


class ServiceUnavailable(APIException):
    """The rate-limit backend is unavailable and a rule fails closed."""

    status_code = 503
    default_detail = "Service temporarily unavailable, try again later."
    default_code = "service_unavailable"


def parse_drf_rate(rate: str) -> tuple[Rate, ...]:
    """Parse a rate the way DRF does, then fall back to UniLimit's grammar.

    ``"100/day"`` and ``"3/min"`` mean what they mean in DRF. DRF only looks at the
    first letter of the period, so ``"1/month"`` is one per **minute** there, and here
    too, so migrated settings keep their behaviour (``manage.py check`` warns about it).
    Anything DRF cannot parse, such as ``"5/30s"`` or ``"10/second;1000/day"``, uses
    UniLimit's rate grammar.
    """
    match = _DRF_RATE.match(rate)
    if match is not None and match.group(2)[0] in _DRF_PERIODS:
        return (Rate.of(int(match.group(1)), _DRF_PERIODS[match.group(2)[0]]),)
    try:
        return Rate.parse_many(rate)
    except ConfigurationError as exc:
        raise ImproperlyConfigured(f"invalid throttle rate {rate!r}: {exc}") from exc


def _is_anonymous(ctx: Any) -> bool:
    return bool(ctx.user_id is None)


class UniLimitBaseThrottle(BaseThrottle):
    """Base class: subclasses return their rules from :meth:`get_rules`.

    ``limiter`` (class attribute) overrides the limiter from ``settings.UNILIMIT``.
    """

    limiter: ClassVar[Limiter | None] = None

    def __init__(self) -> None:
        self._wait: float | None = None

    def get_rules(self, request: Any, view: Any) -> Sequence[Rule | str]:
        raise NotImplementedError

    def get_limiter(self) -> Limiter:
        return type(self).limiter or get_config().limiter

    def allow_request(self, request: Any, view: Any) -> bool:
        limiter = self.get_limiter()
        mine = resolve_rules(limiter, self.get_rules(request, view))
        if not mine:
            return True
        decision = _evaluate(self, limiter, request, view)
        if decision.reason in (Reason.DENIED, Reason.MISSING_IDENTITY):
            raise PermissionDenied()
        if decision.reason is Reason.BACKEND_UNAVAILABLE:
            raise ServiceUnavailable()
        names = {rule.name for rule in mine}
        rejecting = [
            r
            for r in decision.results
            if r.rule.name in names and r.enforced and not r.allowed and not r.degraded
        ]
        if not rejecting:
            return True
        waits = [r.retry_after for r in rejecting if r.retry_after is not None]
        self._wait = max(waits) if waits else None
        return False

    def wait(self) -> float | None:
        return self._wait


def _evaluate(
    throttle: UniLimitBaseThrottle, limiter: Limiter, request: Any, view: Any
) -> Decision:
    """One atomic batch for every UniLimit throttle of ``view`` using ``limiter``.

    DRF calls ``allow_request`` on every throttle in turn; the first UniLimit throttle
    evaluates the rules of all of them together, and the others read that decision.
    """
    http = http_request(request)
    cache: dict[tuple[int, int], Decision] = http.__dict__.setdefault(_EVALUATION_ATTR, {})
    cache_key = (id(view), id(limiter))
    decision = cache.get(cache_key)
    if decision is not None:
        return decision

    throttles: list[Any] = list(view.get_throttles()) if hasattr(view, "get_throttles") else []
    _refuse_double_throttling(throttles, view)
    group = [
        t for t in throttles if isinstance(t, UniLimitBaseThrottle) and t.get_limiter() is limiter
    ]
    if not any(type(t) is type(throttle) for t in group):
        group = [throttle]  # called outside DRF's normal flow
    rules: dict[str, Rule] = {}
    for t in group:
        for rule in resolve_rules(limiter, t.get_rules(request, view)):
            rules.setdefault(rule.name, rule)

    state = request_state(request)
    if not state.middleware and limiter.global_rules:
        warn_once(
            "the Limiter has global rules but UniLimitMiddleware is not in MIDDLEWARE, so "
            "they are not enforced; add 'unilimit.integrations.django.UniLimitMiddleware'"
        )
    selected = tuple(rules.values())
    user_id = sync_user_id(request) if limiter.needs_user(selected) else None
    decision = limiter.evaluate(
        build_context(request, user_id), selected, include_global=False, skip_keys=state.consumed
    )
    state.record(decision)
    cache[cache_key] = decision
    return decision


def _builtin_scope(throttle: Any, view: Any) -> Any:
    if isinstance(throttle, ScopedRateThrottle):
        return getattr(view, throttle.scope_attr, None)
    return getattr(throttle, "scope", None)


def _refuse_double_throttling(throttles: Sequence[Any], view: Any) -> None:
    ours = {
        t.get_scope(view)
        for t in throttles
        if isinstance(t, UniLimitRateThrottle) and t.get_scope(view) is not None
    }
    builtin = {
        _builtin_scope(t, view)
        for t in throttles
        if isinstance(t, SimpleRateThrottle) and not isinstance(t, UniLimitBaseThrottle)
    }
    clash = sorted(str(s) for s in ours & builtin if s is not None)
    if clash:
        raise ImproperlyConfigured(
            f"{type(view).__name__} throttles scope(s) {clash} with both a DRF built-in "
            "throttle and a UniLimit throttle, so each request would be counted twice. "
            "Replace the built-in throttle with its UniLimit equivalent."
        )


class UniLimitThrottle(UniLimitBaseThrottle):
    """Throttles with the rules in the view's ``unilimit_rules``: ``Rule`` objects or
    names of rules registered on the limiter.

    ```python
    class ItemView(APIView):
        throttle_classes = [UniLimitThrottle]
        unilimit_rules = [Rule("items", "100/minute", key="user_or_ip")]
    ```
    """

    def get_rules(self, request: Any, view: Any) -> Sequence[Rule | str]:
        rules = getattr(view, "unilimit_rules", None)
        if rules is None:
            raise ImproperlyConfigured(
                f"{type(view).__name__} uses UniLimitThrottle but has no unilimit_rules"
            )
        if isinstance(rules, Rule | str):
            return (rules,)
        return tuple(rules)


class UniLimitRateThrottle(UniLimitBaseThrottle):
    """A drop-in for DRF's ``SimpleRateThrottle`` conventions.

    - ``scope`` selects the rate in ``REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]`` (or
      ``THROTTLE_RATES`` on the class); ``rate`` on the class overrides it. A ``None``
      rate disables the throttle, as in DRF.
    - Counters are named ``"drf:<scope>"`` and keyed by ``key`` (default: the user,
      else the client IP, like DRF's user and scoped throttles).
    - ``algorithm``, ``fail_mode``, ``on_missing`` and ``when`` are UniLimit rule
      options. ``key`` may be a callable taking the :class:`~unilimit.RequestContext`
      (``ctx.raw`` is the DRF request); set it with ``staticmethod``.

    Client IPs come from the limiter's ``trusted_proxies``, not from DRF's
    ``NUM_PROXIES``.
    """

    scope: ClassVar[str | None] = None
    rate: ClassVar[str | None] = None
    THROTTLE_RATES: ClassVar[Mapping[str, str | None] | None] = None
    key: ClassVar[KeySpec] = "user_or_ip"
    algorithm: ClassVar[AlgorithmName] = "sliding_window"
    fail_mode: ClassVar[FailMode | None] = None
    on_missing: ClassVar[OnMissing | None] = None
    when: ClassVar[Predicate | None] = None

    _rule_cache: ClassVar[dict[tuple[Any, ...], tuple[Rule, ...]]] = {}

    def get_scope(self, view: Any) -> str | None:
        return type(self).scope

    def get_rate(self, scope: str) -> str | None:
        cls = type(self)
        if cls.rate is not None:
            return cls.rate
        rates: Mapping[str, Any] = (
            cls.THROTTLE_RATES
            if cls.THROTTLE_RATES is not None
            else (api_settings.DEFAULT_THROTTLE_RATES or {})
        )
        try:
            rate = rates[scope]
        except KeyError:
            raise ImproperlyConfigured(
                f"No default throttle rate set for '{scope}' scope"
            ) from None
        if rate is not None and not isinstance(rate, str):
            raise ImproperlyConfigured(f"throttle rate for scope {scope!r} must be a string")
        return rate

    def get_rules(self, request: Any, view: Any) -> Sequence[Rule | str]:
        scope = self.get_scope(view)
        if scope is None:
            return ()
        rate = self.get_rate(scope)
        if rate is None:
            return ()
        cls = type(self)
        cache_key = (cls, scope, rate)
        rules = cls._rule_cache.get(cache_key)
        if rules is None:
            rules = self._build_rules(scope, rate)
            cls._rule_cache[cache_key] = rules
        return rules

    def _build_rules(self, scope: str, rate: str) -> tuple[Rule, ...]:
        cls = type(self)
        rates = parse_drf_rate(rate)
        name = f"drf:{scope}"
        options: dict[str, Any] = {
            "key": cls.key,
            "algorithm": cls.algorithm,
            "fail_mode": cls.fail_mode,
            "on_missing": cls.on_missing,
            "when": cls.when,
        }
        if len(rates) == 1:
            return (Rule(name, rates[0], **options),)
        return tuple(Rule(f"{name}:{r}", r, **options) for r in rates)


class UniLimitAnonRateThrottle(UniLimitRateThrottle):
    """Replaces DRF's ``AnonRateThrottle``: anonymous requests, by client IP."""

    scope = "anon"
    key = "ip"
    when = staticmethod(_is_anonymous)  # type: ignore[assignment]  # mypy binds ClassVar callables


class UniLimitUserRateThrottle(UniLimitRateThrottle):
    """Replaces DRF's ``UserRateThrottle``: by user, or by client IP when anonymous."""

    scope = "user"


class UniLimitScopedRateThrottle(UniLimitRateThrottle):
    """Replaces DRF's ``ScopedRateThrottle``: the scope is the view's ``throttle_scope``;
    views without one are not throttled."""

    scope_attr: ClassVar[str] = "throttle_scope"

    def get_scope(self, view: Any) -> str | None:
        scope = getattr(view, type(self).scope_attr, None)
        return scope if isinstance(scope, str) else None


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
