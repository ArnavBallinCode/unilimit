"""The ``RateLimit`` FastAPI dependency: endpoint- and router-level limits."""

from __future__ import annotations

import hashlib
import inspect
from collections.abc import Callable, Iterable
from typing import Any

from fastapi import Depends, HTTPException
from starlette.requests import HTTPConnection, Request
from starlette.responses import Response

from unilimit._logging import logger
from unilimit.core.errors import ConfigurationError
from unilimit.core.errors import RateLimitExceeded as CoreRateLimitExceeded
from unilimit.core.identity import KeySpec
from unilimit.core.limiter import Limiter
from unilimit.core.rate import Duration, Rate
from unilimit.core.result import Decision
from unilimit.core.rule import Action, AlgorithmName, CostFunc, FailMode, OnMissing, Predicate, Rule
from unilimit.http import ResponseConfig, ResponseSpec, rate_limit_headers, rejection_response
from unilimit.integrations.fastapi._state import (
    RequestState,
    build_context,
    request_state,
    scope_user,
)

APP_STATE_LIMITER = "unilimit"
APP_STATE_RESPONSE = "unilimit_response"


class RateLimitExceeded(HTTPException, CoreRateLimitExceeded):
    """Raised by :class:`RateLimit` when a request is not admitted.

    It is an ``HTTPException``, so FastAPI answers with the right status and headers
    even without :func:`setup`. ``setup`` installs a handler that also renders the
    configured response body.
    """

    def __init__(self, decision: Decision, spec: ResponseSpec) -> None:
        HTTPException.__init__(
            self, status_code=spec.status_code, detail=spec.detail, headers=dict(spec.headers)
        )
        CoreRateLimitExceeded.__init__(self, decision, spec.detail)
        self.spec = spec


class Policies:
    """Rule definition shared by :class:`RateLimit` and :class:`WebSocketRateLimit`.

    Rules can be given three ways::

        RateLimit("profile", rate="30/minute")           # inline rule
        RateLimit("profile", rate="10/second;1000/day")  # several inline rules (limits syntax)
        RateLimit("free-tier", "burst")                  # rules registered on the Limiter
        RateLimit(Rule("search", "10/s"))                # Rule objects, shared across routes

    All rules of one instance are consumed atomically, all or nothing. Rules with the
    same name share one counter wherever they are used; ``per_route=True`` gives every
    route its own counter instead.

    ``identity`` is a FastAPI dependency (sync or async, with its own dependencies)
    returning the authenticated user id (``str``/``int``) or ``None``. FastAPI resolves
    it **before** the limit is evaluated and caches it per request, and its value becomes
    the ``"user"`` identity. Inline rules key on ``"user"`` when ``identity`` is given and
    on ``"ip"`` otherwise; pass ``key=`` for anything else, such as ``"user_or_ip"`` or a
    callable.

    Subclasses define the connection parameters FastAPI injects and the call itself.
    """

    def __init__(
        self,
        *rules: Rule | str,
        rate: str | Rate | None = None,
        limit: int | None = None,
        window: Duration | None = None,
        identity: Callable[..., Any] | None = None,
        key: KeySpec | None = None,
        algorithm: AlgorithmName = "sliding_window",
        burst: int | None = None,
        cost: int | CostFunc = 1,
        methods: Iterable[str] | None = None,
        when: Predicate | None = None,
        on_missing: OnMissing | None = None,
        fail_mode: FailMode | None = None,
        action: Action = "reject",
        per_route: bool = False,
        limiter: Limiter | None = None,
    ) -> None:
        kind = type(self).__name__
        if not rules:
            raise ConfigurationError(f"{kind} needs at least one rule or rule name")
        if rate is not None or limit is not None or window is not None:
            if len(rules) != 1 or not isinstance(rules[0], str):
                raise ConfigurationError(
                    f"an inline {kind} takes exactly one name: {kind}('name', rate='10/s')"
                )
            options: dict[str, Any] = {
                "algorithm": algorithm,
                "key": key if key is not None else ("user" if identity else "ip"),
                "burst": burst,
                "cost": cost,
                "methods": methods,
                "when": when,
                "on_missing": on_missing,
                "fail_mode": fail_mode,
                "action": action,
            }
            if isinstance(rate, str) and limit is None and window is None:
                # "10/second;1000/day": several rules, consumed atomically together.
                self._rules: tuple[Rule | str, ...] = Rule.many(rules[0], rate, **options)
            else:
                self._rules = (Rule(rules[0], rate, limit=limit, window=window, **options),)
        else:
            customised = (key, burst, methods, when, on_missing, fail_mode)
            if any(v is not None for v in customised) or cost != 1 or action != "reject":
                raise ConfigurationError(
                    "rule options need an inline rule (rate= or limit=/window=); "
                    "set them on the Rule objects instead"
                )
            for r in rules:
                if not isinstance(r, Rule | str):
                    raise ConfigurationError(f"expected Rule objects or rule names, got {r!r}")
            self._rules = rules
        if identity is not None and not callable(identity):
            raise ConfigurationError("identity must be a FastAPI dependency (a callable)")
        self._limiter = limiter
        self._per_route = per_route
        self._route_rules: dict[tuple[str, str], tuple[Rule, ...]] = {}
        if limiter is not None:
            self._resolve(limiter)  # unknown rule names fail at import time, not per request

        params = self._connection_parameters()
        if identity is not None:
            params.append(
                inspect.Parameter(
                    "identity", inspect.Parameter.KEYWORD_ONLY, default=Depends(identity)
                )
            )
        self._has_identity = identity is not None
        self.__signature__ = inspect.Signature(params, return_annotation=Decision)

    def _connection_parameters(self) -> list[inspect.Parameter]:
        raise NotImplementedError

    def __repr__(self) -> str:
        names = ", ".join(r.name if isinstance(r, Rule) else repr(r) for r in self._rules)
        return f"{type(self).__name__}({names})"

    def _resolve(self, limiter: Limiter) -> tuple[Rule, ...]:
        return tuple(r if isinstance(r, Rule) else limiter.get_rule(r) for r in self._rules)

    def _rules_for(self, limiter: Limiter, conn: HTTPConnection) -> tuple[Rule, ...]:
        rules = self._resolve(limiter)
        if not self._per_route:
            return rules
        route = conn.scope.get("route")
        path = str(getattr(route, "path", conn.scope.get("path", "/")))
        default_method = conn.scope.get("method", "WEBSOCKET")
        methods = ",".join(sorted(getattr(route, "methods", None) or [default_method]))
        cache_key = (methods, path)
        cached = self._route_rules.get(cache_key)
        if cached is None:
            cached = tuple(_scoped(rule, f"{methods} {path}") for rule in rules)
            self._route_rules[cache_key] = cached
        return cached

    def _limiter_for(self, conn: HTTPConnection, state: RequestState) -> Limiter:
        limiter = self._limiter or state.limiter or _app_state(conn, APP_STATE_LIMITER)
        if not isinstance(limiter, Limiter):
            raise ConfigurationError(
                "no Limiter found: call unilimit.integrations.fastapi.setup(app, limiter) "
                f"or pass {type(self).__name__}(..., limiter=limiter)"
            )
        return limiter


class RateLimit(Policies):
    """A FastAPI dependency that rate-limits the HTTP routes it is attached to.

    See :class:`Policies` for how rules and identities are given. The dependency
    returns the :class:`~unilimit.Decision`; rejections raise :class:`RateLimitExceeded`.
    """

    def _connection_parameters(self) -> list[inspect.Parameter]:
        return [
            inspect.Parameter("request", inspect.Parameter.KEYWORD_ONLY, annotation=Request),
            inspect.Parameter("response", inspect.Parameter.KEYWORD_ONLY, annotation=Response),
        ]

    async def __call__(
        self, *, request: Request, response: Response, identity: Any = None
    ) -> Decision:
        state = request_state(request.scope)
        limiter = self._limiter_for(request, state)
        config = state.response or _app_state(request, APP_STATE_RESPONSE) or ResponseConfig()
        if not state.middleware and limiter.global_rules:
            _warn_once(
                "the Limiter has global rules but UniLimitMiddleware is not installed, so "
                "they are not enforced; call setup(app, limiter)"
            )

        user_id = identity if self._has_identity else scope_user(request)
        decision = await limiter.aevaluate(
            build_context(request, user_id),
            self._rules_for(limiter, request),
            include_global=False,
            skip_keys=state.consumed,
        )
        state.record(decision)
        if not decision.allowed:
            raise RateLimitExceeded(decision, rejection_response(decision, config))
        if not state.middleware:
            # Without the middleware, headers can only reach responses FastAPI builds;
            # responses returned directly by the endpoint do not get them.
            for name, value in rate_limit_headers(state.results, config):
                response.headers[name] = value
        return decision


def _scoped(rule: Rule, route: str) -> Rule:
    name = f"{rule.name}|{route}"
    if len(name) > 200:
        name = f"{rule.name[:150]}|{hashlib.sha256(route.encode()).hexdigest()[:16]}"
    return Rule(
        name,
        rule.rate,
        algorithm=rule.algorithm,
        key=rule.key,
        burst=rule.burst,
        methods=rule.methods,
        paths=rule.paths,
        when=rule.when,
        cost=rule.cost,
        on_missing=rule.on_missing,
        fail_mode=rule.fail_mode,
        action=rule.action,
    )


def _app_state(conn: HTTPConnection, name: str) -> Any:
    return getattr(conn.app.state, name, None) if "app" in conn.scope else None


_warned: set[str] = set()


def _warn_once(message: str) -> None:
    if message not in _warned:
        _warned.add(message)
        logger.warning(message)


__all__ = ["Policies", "RateLimit", "RateLimitExceeded"]
