"""Compatibility adapter for SlowAPI (FastAPI and Starlette).

``from unilimit.compat.slowapi import Limiter, ...`` accepts SlowAPI's main API: the
``limit`` and ``shared_limit`` decorators, ``exempt``, ``default_limits`` and
``application_limits`` (enforced by :class:`SlowAPIMiddleware`), ``headers_enabled``,
``enabled`` and the 429 handler. Options it cannot honour raise ``ConfigurationError``
instead of being ignored. Differences from upstream are listed in ``docs/migration.md``.
"""

from __future__ import annotations

import functools
import hashlib
import inspect
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Literal, TypeVar

from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Match
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from unilimit.core.context import RequestContext
from unilimit.core.errors import ConfigurationError
from unilimit.core.identity import KeySpec
from unilimit.core.limiter import Limiter as CoreLimiter
from unilimit.core.rate import Rate
from unilimit.core.result import Decision, RateLimitResult, Reason
from unilimit.core.rule import AlgorithmName, CostFunc, Predicate, Rule
from unilimit.http import ResponseConfig, rate_limit_headers, rejection_response
from unilimit.integrations.starlette._state import build_context

CallableT = TypeVar("CallableT", bound=Callable[..., Any])
LimitValue = str | Callable[..., str]

_STRATEGIES: dict[str, AlgorithmName] = {
    "fixed-window": "fixed_window",
    "sliding-window-counter": "sliding_window",
}
# SlowAPI options the adapter cannot honour, with what to do instead.
_UNSUPPORTED = {
    "auto_check": "limits are always checked by the decorator",
    "swallow_errors": "use fail_mode='open' (the default) for backend outages",
    "in_memory_fallback": "use in_memory_fallback_enabled=True; it reuses the same limits",
    "key_prefix": "pass namespace='...' instead",
    "config_filename": "pass options to Limiter directly; .env files are not read",
}


def get_remote_address(request: Request) -> str:
    """SlowAPI's default key: the client address.

    Used as ``key_func``, the adapter replaces it with UniLimit's ``"ip"`` key, which
    honours ``trusted_proxies=`` and groups IPv6 clients by /64.
    """
    if request.client and request.client.host:
        return request.client.host
    return "127.0.0.1"


class RateLimitExceeded(HTTPException):
    """Raised when a request is not admitted; render it with
    :func:`_rate_limit_exceeded_handler`.

    ``status_code`` is 429 for rate limits. A missing key (``key_func`` returned nothing)
    gives 403 and a backend outage under ``fail_mode="closed"`` gives 503.
    """

    def __init__(
        self,
        detail: str = "Rate limit exceeded",
        decision: Decision | RateLimitResult | None = None,
        *,
        status_code: int = 429,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(status_code=status_code, detail=detail, headers=headers)
        self.decision = decision


def _rate_limit_exceeded_handler(request: Request, exc: Exception) -> Response:
    """SlowAPI's exception handler: a JSON error with the rate-limit headers."""
    detail = getattr(exc, "detail", "Rate limit exceeded")
    status_code = getattr(exc, "status_code", 429)
    headers = dict(getattr(exc, "headers", None) or {})
    decision = getattr(exc, "decision", None)
    if not headers and decision is not None:
        results = decision.results if isinstance(decision, Decision) else (decision,)
        headers = dict(rate_limit_headers(results, rejected=True))
    error = f"Rate limit exceeded: {detail}" if status_code == 429 else detail
    return JSONResponse(status_code=status_code, content={"error": error}, headers=headers)


class _LimitGroup:
    """One ``limit``/``shared_limit`` declaration (or the default/application limits)."""

    def __init__(
        self,
        limiter: Limiter,
        limit_value: LimitValue,
        scope: str,
        *,
        key: KeySpec,
        key_func: Callable[..., str],
        per_method: bool = False,
        methods: Sequence[str] | None = None,
        error_message: str | Callable[[], str] | None = None,
        exempt_when: Callable[..., bool] | None = None,
        cost: int | Callable[..., int] = 1,
        override_defaults: bool = True,
    ) -> None:
        self._limiter = limiter
        self._limit_value = limit_value
        self._scope = scope
        self._key = key
        self._key_call = _with_request(key_func)
        self._per_method = per_method
        self._methods = None if methods is None else [m.upper() for m in methods]
        self._error_message = error_message
        self._when = _exemption(exempt_when) if exempt_when is not None else None
        self._cost: int | CostFunc = cost if isinstance(cost, int) else _request_call(cost)
        self.override_defaults = override_defaults
        self._cache: dict[tuple[str, str, str | None], tuple[Rule, ...]] = {}
        if not callable(limit_value):
            self._build(scope, limit_value, None)  # invalid rates fail at import time

    def rules(self, request: Request, scope: str | None = None) -> tuple[Rule, ...]:
        provider = self._limit_value
        if callable(provider):
            takes_key = "key" in inspect.signature(provider).parameters
            value = provider(self._key_call(request)) if takes_key else provider()
        else:
            value = provider
        method = request.method if self._per_method else None
        return self._build(scope or self._scope, value, method)

    def _build(self, scope: str, value: str, method: str | None) -> tuple[Rule, ...]:
        rules = self._cache.get((scope, value, method))
        if rules is None:
            base = scope if method is None else f"{scope}:{method}"
            rules = tuple(
                Rule(
                    _rule_name(base, str(rate)),
                    rate,
                    algorithm=self._limiter._algorithm,
                    key=self._key,
                    methods=self._methods,
                    when=self._when,
                    cost=self._cost,
                )
                for rate in Rate.parse_many(value)
            )
            self._cache[(scope, value, method)] = rules
        return rules

    def owns(self, rule: Rule) -> bool:
        return any(rule is r for rules in self._cache.values() for r in rules)

    def message(self, rule: Rule) -> str:
        message = self._error_message
        if message is None:
            return _describe(rule.rate)
        return message() if callable(message) else message


class Limiter:
    """SlowAPI's ``Limiter``, enforced by UniLimit's core.

    Storage is ``storage_uri`` (``memory://`` by default, or a Redis URL) or an existing
    UniLimit ``limiter=``. Extra keyword ``options`` go to ``unilimit.Limiter.from_url``,
    for example ``trusted_proxies=``, ``namespace=`` or ``identity_secret=``.
    """

    def __init__(
        self,
        key_func: Callable[..., str] | None = None,
        default_limits: Sequence[LimitValue] = (),
        application_limits: Sequence[LimitValue] = (),
        headers_enabled: bool = False,
        strategy: str | None = None,
        storage_uri: str | None = None,
        storage_options: Mapping[str, Any] | None = None,
        *,
        in_memory_fallback_enabled: bool = False,
        retry_after: str | None = None,
        enabled: bool = True,
        key_style: Literal["endpoint", "url"] = "endpoint",
        limiter: CoreLimiter | None = None,
        **options: Any,
    ) -> None:
        for name, hint in _UNSUPPORTED.items():
            if name in options:
                raise ConfigurationError(f"SlowAPI option {name}= is not supported: {hint}")
        if key_style != "endpoint":
            raise ConfigurationError(
                "key_style='url' gives every URL its own counter, so clients could avoid a "
                "limit by varying path parameters; the adapter counts per endpoint"
            )
        if strategy is not None and strategy not in _STRATEGIES:
            raise ConfigurationError(
                f"unsupported strategy {strategy!r}; use one of {sorted(_STRATEGIES)} "
                "(moving-window has no UniLimit equivalent; sliding-window-counter is closest)"
            )
        self._algorithm: AlgorithmName = _STRATEGIES[strategy] if strategy else "sliding_window"
        self.key_func = key_func or get_remote_address
        self.enabled = enabled
        self._headers_enabled = headers_enabled
        self._response = ResponseConfig(
            retry_after_format="http-date" if retry_after == "http-date" else "seconds"
        )

        options = {**(storage_options or {}), **options}
        if limiter is not None:
            if storage_uri or options or in_memory_fallback_enabled:
                raise ConfigurationError("pass either limiter= or storage options, not both")
            self._core_limiter = limiter
        else:
            if in_memory_fallback_enabled:
                options["fallback"] = "memory"
            self._core_limiter = CoreLimiter.from_url(storage_uri or "memory://", **options)

        self._decorated: dict[
            int, tuple[Callable[..., Any], Callable[..., Any], tuple[_LimitGroup, ...]]
        ] = {}
        self._exempt: dict[int, Callable[..., Any]] = {}
        self._default_limits = [self._group(v, "default") for v in default_limits]
        self._application_limits = [self._group(v, "application") for v in application_limits]

    def _group(self, value: LimitValue, scope: str, **kwargs: Any) -> _LimitGroup:
        key_func = kwargs.pop("key_func", None) or self.key_func
        return _LimitGroup(self, value, scope, key=_key_spec(key_func), key_func=key_func, **kwargs)

    # -- decorators ----------------------------------------------------------------------

    def limit(
        self,
        limit_value: LimitValue,
        key_func: Callable[..., str] | None = None,
        per_method: bool = False,
        methods: Sequence[str] | None = None,
        error_message: str | Callable[[], str] | None = None,
        exempt_when: Callable[..., bool] | None = None,
        cost: int | Callable[..., int] = 1,
        override_defaults: bool = True,
    ) -> Callable[[CallableT], CallableT]:
        """Limit one endpoint: ``@limiter.limit("5/minute")``.

        The endpoint needs a ``request: Request`` parameter. Stacked ``limit`` decorators
        on one endpoint are consumed in one atomic batch.
        """

        def decorator(func: CallableT) -> CallableT:
            scope = _endpoint_name(self._original(func))
            group = self._group(
                limit_value,
                scope,
                key_func=key_func,
                per_method=per_method,
                methods=methods,
                error_message=error_message,
                exempt_when=exempt_when,
                cost=cost,
                override_defaults=override_defaults,
            )
            return self._decorate(func, group)

        return decorator

    def shared_limit(
        self,
        limit_value: LimitValue,
        scope: str,
        key_func: Callable[..., str] | None = None,
        error_message: str | Callable[[], str] | None = None,
        exempt_when: Callable[..., bool] | None = None,
        cost: int | Callable[..., int] = 1,
        override_defaults: bool = True,
    ) -> Callable[[CallableT], CallableT]:
        """One limit shared by every endpoint decorated with the same ``scope``."""
        if not isinstance(scope, str):
            raise ConfigurationError("shared_limit scope must be a string")

        def decorator(func: CallableT) -> CallableT:
            group = self._group(
                limit_value,
                f"shared:{scope}",
                key_func=key_func,
                error_message=error_message,
                exempt_when=exempt_when,
                cost=cost,
                override_defaults=override_defaults,
            )
            return self._decorate(func, group)

        return decorator

    def exempt(self, func: CallableT) -> CallableT:
        """Exempt an endpoint from the default and application limits."""
        self._exempt[id(func)] = func
        return func

    def _original(self, func: Callable[..., Any]) -> Callable[..., Any]:
        entry = self._decorated.get(id(func))
        return func if entry is None else entry[1]

    def _decorate(self, func: CallableT, group: _LimitGroup) -> CallableT:
        entry = self._decorated.get(id(func))
        original, inner = (func, ()) if entry is None else entry[1:]
        groups = (*inner, group)
        parameters = list(inspect.signature(original).parameters)
        if "request" not in parameters:
            raise ConfigurationError(
                f'{_endpoint_name(original)} needs a "request: Request" parameter to be '
                "rate limited"
            )
        index = parameters.index("request")

        def find_request(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Request:
            request = kwargs.get("request", args[index] if index < len(args) else None)
            if not isinstance(request, Request):
                raise TypeError("parameter `request` must be an instance of Request")
            return request

        wrapper: Callable[..., Any]
        if inspect.iscoroutinefunction(original):

            @functools.wraps(original)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                request = find_request(args, kwargs)
                decision = await self._acheck(request, groups)
                response = await original(*args, **kwargs)
                return self._inject(response, kwargs, decision)

            wrapper = async_wrapper
        else:

            @functools.wraps(original)
            def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
                request = find_request(args, kwargs)
                decision = self._check(request, groups)
                return self._inject(original(*args, **kwargs), kwargs, decision)

            wrapper = sync_wrapper
        # The outermost wrapper evaluates every stacked limit and calls the endpoint. The
        # entry holds the wrapper, so its id is never reused while registered.
        self._decorated[id(wrapper)] = (wrapper, original, groups)
        return wrapper  # type: ignore[return-value]

    # -- enforcement ---------------------------------------------------------------------

    def _context(self, request: Request) -> RequestContext:
        return build_context(request, None)

    def _check(self, request: Request, groups: Sequence[_LimitGroup]) -> Decision | None:
        if not self.enabled:
            return None
        rules = [rule for group in groups for rule in group.rules(request)]
        decision = self._core_limiter.evaluate(self._context(request), rules, include_global=False)
        return self._admit(decision, groups)

    async def _acheck(self, request: Request, groups: Sequence[_LimitGroup]) -> Decision | None:
        if not self.enabled:
            return None
        rules = [rule for group in groups for rule in group.rules(request)]
        decision = await self._core_limiter.aevaluate(
            self._context(request), rules, include_global=False
        )
        return self._admit(decision, groups)

    def _admit(self, decision: Decision, groups: Sequence[_LimitGroup]) -> Decision:
        if decision.allowed:
            return decision
        spec = rejection_response(decision, self._response)
        detail = spec.detail
        binding = decision.binding
        if decision.reason is Reason.RATE_LIMITED and binding is not None:
            owner = next((g for g in groups if g.owns(binding.rule)), None)
            detail = owner.message(binding.rule) if owner else _describe(binding.rule.rate)
        raise RateLimitExceeded(
            detail, decision, status_code=spec.status_code, headers=dict(spec.headers)
        )

    def _inject(self, response: Any, kwargs: dict[str, Any], decision: Decision | None) -> Any:
        if not self._headers_enabled or decision is None:
            return response
        target = response if isinstance(response, Response) else kwargs.get("response")
        if not isinstance(target, Response):
            raise TypeError(
                "headers_enabled=True needs the endpoint to return a Response or to take "
                "a `response: Response` parameter"
            )
        for name, value in rate_limit_headers(decision.results, self._response):
            target.headers[name] = value
        return response

    def _groups_of(self, endpoint: Any) -> tuple[_LimitGroup, ...] | None:
        for fn in _unwrap(endpoint):
            entry = self._decorated.get(id(fn))
            if entry is not None:
                return entry[2]
        return None

    async def _middleware_check(self, request: Request, app: Any) -> Decision | None:
        """Default and application limits for one request (see :class:`SlowAPIMiddleware`)."""
        if not self.enabled:
            return None
        endpoint = _route_endpoint(app, request.scope)
        if endpoint is not None and any(id(fn) in self._exempt for fn in _unwrap(endpoint)):
            return None
        rules = [rule for group in self._application_limits for rule in group.rules(request)]
        route_groups = self._groups_of(endpoint) if endpoint is not None else None
        if not route_groups or all(not g.override_defaults for g in route_groups):
            scope = f"default:{_endpoint_name(endpoint) if endpoint is not None else '-'}"
            rules += [rule for g in self._default_limits for rule in g.rules(request, scope)]
        if not rules:
            return None
        decision = await self._core_limiter.aevaluate(
            self._context(request), rules, include_global=False
        )
        return self._admit(decision, [*self._application_limits, *self._default_limits])


class SlowAPIMiddleware:
    """Enforces ``application_limits`` and ``default_limits``, like SlowAPI's middleware.

    Install with ``app.state.limiter = limiter`` and ``app.add_middleware(SlowAPIMiddleware)``.
    Default limits apply to endpoints without their own ``limit`` (each endpoint has its
    own counter); application limits are one counter shared by every endpoint. Endpoints
    marked with ``limiter.exempt`` skip both. The middleware's limits and a decorator's
    limits are separate atomic steps. It is pure ASGI, so it is also SlowAPI's
    ``SlowAPIASGIMiddleware``.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        app = scope.get("app")
        limiter = getattr(getattr(app, "state", None), "limiter", None)
        if not isinstance(limiter, Limiter):
            raise ConfigurationError("SlowAPIMiddleware needs app.state.limiter = Limiter(...)")
        request = Request(scope, receive)
        try:
            decision = await limiter._middleware_check(request, app)
        except RateLimitExceeded as exc:
            handlers = getattr(app, "exception_handlers", {})
            handler = handlers.get(RateLimitExceeded, _rate_limit_exceeded_handler)
            response = handler(request, exc)
            if inspect.isawaitable(response):
                response = await response
            await response(scope, receive, send)
            return
        if decision is None or not limiter._headers_enabled:
            await self.app(scope, receive, send)
            return
        headers = rate_limit_headers(decision.results, limiter._response)

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                existing = {k.lower() for k, _ in message.get("headers", ())}
                extra = [
                    (k.lower().encode("latin-1"), v.encode("latin-1"))
                    for k, v in headers
                    if k.lower().encode("latin-1") not in existing
                ]
                message = {**message, "headers": [*message.get("headers", ()), *extra]}
            await send(message)

        await self.app(scope, receive, send_with_headers)


SlowAPIASGIMiddleware = SlowAPIMiddleware


def _with_request(fn: Callable[..., Any]) -> Callable[[Any], Any]:
    """Call a SlowAPI callback with the request if it takes an argument, as upstream."""
    if inspect.signature(fn).parameters:
        return fn
    return lambda request: fn()


def _key_spec(key_func: Callable[..., str]) -> KeySpec:
    if key_func is get_remote_address:
        return "ip"
    call = _with_request(key_func)
    return lambda ctx: call(ctx.raw)


def _exemption(exempt_when: Callable[..., bool]) -> Predicate:
    call = _with_request(exempt_when)
    return lambda ctx: not call(ctx.raw)


def _request_call(fn: Callable[..., int]) -> CostFunc:
    return lambda ctx: fn(ctx.raw)


def _endpoint_name(endpoint: Any) -> str:
    qualname = getattr(endpoint, "__qualname__", None) or type(endpoint).__qualname__
    return f"{getattr(endpoint, '__module__', None) or type(endpoint).__module__}.{qualname}"


def _rule_name(base: str, rate: str) -> str:
    name = f"{base}:{rate}"
    if len(name) > 200:
        name = f"{base[:150]}|{hashlib.sha256(name.encode()).hexdigest()[:16]}:{rate}"
    return name


def _unwrap(fn: Any) -> list[Any]:
    chain = [fn]
    while (fn := getattr(fn, "__wrapped__", None)) is not None and len(chain) < 32:
        chain.append(fn)
    return chain


def _route_endpoint(app: Any, scope: Scope) -> Any:
    for route in getattr(app, "routes", ()):
        match, child = route.matches(scope)
        if match is Match.FULL:
            return child.get("endpoint", getattr(route, "endpoint", None))
    return None


def _describe(rate: Rate) -> str:
    """A rate as SlowAPI words it: ``"5 per 1 minute"``."""
    for unit, seconds in (("day", 86_400), ("hour", 3_600), ("minute", 60), ("second", 1)):
        if rate.window_us % (seconds * 1_000_000) == 0:
            return f"{rate.limit} per {rate.window_us // (seconds * 1_000_000)} {unit}"
    return f"{rate.limit} per {rate.window_seconds:g} second"


__all__ = [
    "Limiter",
    "RateLimitExceeded",
    "SlowAPIASGIMiddleware",
    "SlowAPIMiddleware",
    "_rate_limit_exceeded_handler",
    "get_remote_address",
]
