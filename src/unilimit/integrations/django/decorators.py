"""The ``@ratelimit`` view decorator."""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable, Iterable
from typing import Any, TypeVar

from django.http import HttpRequest

from unilimit.core.errors import ConfigurationError
from unilimit.core.identity import KeySpec
from unilimit.core.limiter import Limiter
from unilimit.core.rate import Duration, Rate
from unilimit.core.result import Decision
from unilimit.core.rule import Action, AlgorithmName, CostFunc, FailMode, OnMissing, Predicate, Rule
from unilimit.integrations._common import build_rules, resolve_rules
from unilimit.integrations.django._request import (
    add_state_headers,
    aevaluate,
    async_user_id,
    build_context,
    rejection,
    request_state,
    sync_user_id,
    warn_once,
)
from unilimit.integrations.django.conf import get_config

ViewT = TypeVar("ViewT", bound=Callable[..., Any])

_NO_MIDDLEWARE = (
    "the Limiter has global rules but UniLimitMiddleware is not in MIDDLEWARE, so they are "
    "not enforced; add 'unilimit.integrations.django.UniLimitMiddleware'"
)


def ratelimit(
    *rules: Rule | str,
    rate: str | Rate | None = None,
    limit: int | None = None,
    window: Duration | None = None,
    key: KeySpec | None = None,
    algorithm: AlgorithmName = "sliding_window",
    burst: int | None = None,
    cost: int | CostFunc = 1,
    methods: Iterable[str] | None = None,
    when: Predicate | None = None,
    on_missing: OnMissing | None = None,
    fail_mode: FailMode | None = None,
    action: Action = "reject",
    limiter: Limiter | None = None,
) -> Callable[[ViewT], ViewT]:
    """Rate-limit a view (function, or class-based view method).

    Rules can be given like ``RateLimit`` in FastAPI::

        @ratelimit(rate="5/minute")                     # inline; named after the view
        @ratelimit("login", rate="5/minute", key="ip")  # inline, named (shared counter)
        @ratelimit("search", rate="10/second;1000/day") # several rates, one batch
        @ratelimit("free-tier", "burst")                # rules registered on the Limiter
        @ratelimit(Rule("export", "2/minute", key="user"))

    All rules of one decorator are consumed atomically. Inline rules key on ``"ip"``
    unless ``key=`` says otherwise (``"user"``, ``"user_or_ip"``, ``"header:X-Api-Key"``
    or a callable taking the :class:`~unilimit.RequestContext`, whose ``raw`` is the
    request).

    A rejected request gets a response straight away (429 by default, or
    ``UNILIMIT["REJECTION_VIEW"]``); the view does not run. Async views are limited
    without blocking the event loop.

    The user is ``request.user``, so ``"user"`` keys need Django's
    ``AuthenticationMiddleware``. For DRF token or JWT authentication use the DRF
    throttles instead: they run after DRF authenticates.
    """
    if rate is None and limit is None and window is None and not rules:
        raise ConfigurationError("ratelimit needs a rule, a rule name or rate=")

    policies: dict[tuple[Rule | str, ...], _ViewPolicy] = {}

    def policy_for(names: tuple[Rule | str, ...]) -> _ViewPolicy:
        # method_decorator() applies the decorator again on every call, so build the
        # rules once per name, not once per decoration.
        policy = policies.get(names)
        if policy is None:
            defined = build_rules(
                names,
                rate=rate,
                limit=limit,
                window=window,
                key=key,
                default_key="ip",
                algorithm=algorithm,
                burst=burst,
                cost=cost,
                methods=methods,
                when=when,
                on_missing=on_missing,
                fail_mode=fail_mode,
                action=action,
                owner="ratelimit",
            )
            policy = _ViewPolicy(defined, limiter)
            if limiter is not None:
                policy.rules_for(limiter)  # unknown rule names fail at import time
            policies[names] = policy
        return policy

    if rules:
        policy_for(rules)  # invalid arguments fail here, at import time

    def decorate(view: ViewT) -> ViewT:
        if inspect.isclass(view):
            raise TypeError(
                "@ratelimit decorates functions; for class-based views use "
                "method_decorator(ratelimit(...), name='dispatch')"
            )
        policy = policy_for(rules or (f"{view.__module__}.{view.__qualname__}",))

        if inspect.iscoroutinefunction(view):

            @functools.wraps(view)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                request = _find_request(args)
                denied = await policy.acheck(request)
                if denied is not None:
                    return denied
                return policy.finish(request, await view(*args, **kwargs))

            return async_wrapper  # type: ignore[return-value]

        is_async_dispatch = _is_async_class_view_method(view)

        @functools.wraps(view)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            request = _find_request(args)
            if is_async_dispatch:
                # dispatch() of an async class-based view returns a coroutine; return
                # one too, and limit inside it without blocking the event loop.
                return _async_dispatch(policy, view, request, args, kwargs)
            denied = policy.check(request)
            if denied is not None:
                return denied
            return policy.finish(request, view(*args, **kwargs))

        return sync_wrapper  # type: ignore[return-value]

    return decorate


async def _async_dispatch(
    policy: _ViewPolicy,
    view: Callable[..., Any],
    request: Any,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> Any:
    denied = await policy.acheck(request)
    if denied is not None:
        return denied
    response = view(*args, **kwargs)
    if inspect.isawaitable(response):
        response = await response
    return policy.finish(request, response)


class _ViewPolicy:
    def __init__(self, rules: tuple[Rule | str, ...], limiter: Limiter | None) -> None:
        self._rules = rules
        self._limiter = limiter
        self._resolved: dict[int, tuple[Rule, ...]] = {}

    def rules_for(self, limiter: Limiter) -> tuple[Rule, ...]:
        resolved = self._resolved.get(id(limiter))
        if resolved is None:
            resolved = resolve_rules(limiter, self._rules)
            self._resolved = {id(limiter): resolved}  # settings may swap the limiter
        return resolved

    def _prepare(self, request: Any) -> tuple[Limiter, tuple[Rule, ...]]:
        state = request_state(request)
        limiter = self._limiter or state.limiter or get_config().limiter
        if not state.middleware and limiter.global_rules:
            warn_once(_NO_MIDDLEWARE)
        return limiter, self.rules_for(limiter)

    def check(self, request: Any) -> Any:
        limiter, rules = self._prepare(request)
        state = request_state(request)
        user_id = sync_user_id(request) if limiter.needs_user(rules) else None
        decision = limiter.evaluate(
            build_context(request, user_id), rules, include_global=False, skip_keys=state.consumed
        )
        return self._record(request, decision)

    async def acheck(self, request: Any) -> Any:
        limiter, rules = self._prepare(request)
        state = request_state(request)
        user_id = await async_user_id(request) if limiter.needs_user(rules) else None
        decision = await aevaluate(
            limiter,
            request,
            build_context(request, user_id),
            rules,
            include_global=False,
            skip_keys=state.consumed,
        )
        return self._record(request, decision)

    def _record(self, request: Any, decision: Decision) -> Any:
        state = request_state(request)
        state.record(decision)
        if decision.allowed:
            return None
        return rejection(request, decision, state.response)

    def finish(self, request: Any, response: Any) -> Any:
        state = request_state(request)
        if not state.middleware:
            add_state_headers(response, state, state.response or get_config().response)
        return response


def _find_request(args: tuple[Any, ...]) -> Any:
    """The request among a view's positional arguments (``self`` comes first on methods)."""
    for arg in args[:2]:
        inner = getattr(arg, "_request", arg)
        if isinstance(inner, HttpRequest):
            return arg
    raise TypeError("@ratelimit: the decorated view was not called with a request")


def _is_async_class_view_method(view: Callable[..., Any]) -> bool:
    """Whether ``view`` is a method of an async class-based view, as passed by
    ``method_decorator`` (a ``partial`` of the bound method)."""
    bound = getattr(view, "func", view)
    owner = getattr(bound, "__self__", None)
    return bool(getattr(owner, "view_is_async", False))
