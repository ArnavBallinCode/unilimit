"""Rule definitions shared by the FastAPI ``RateLimit`` dependency and the WebSocket limits.

Only Starlette is imported here; FastAPI is needed only for ``identity=`` dependencies.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Iterable
from typing import Any

from starlette.requests import HTTPConnection

from unilimit._logging import logger
from unilimit.core.errors import ConfigurationError
from unilimit.core.identity import KeySpec
from unilimit.core.limiter import Limiter
from unilimit.core.rate import Duration, Rate
from unilimit.core.result import Decision
from unilimit.core.rule import Action, AlgorithmName, CostFunc, FailMode, OnMissing, Predicate, Rule
from unilimit.integrations._common import build_rules, resolve_rules, scoped_rule
from unilimit.integrations.starlette._state import RequestState

APP_STATE_LIMITER = "unilimit"
APP_STATE_RESPONSE = "unilimit_response"


def identity_parameter(identity: Callable[..., Any]) -> inspect.Parameter:
    """The keyword parameter through which FastAPI injects an ``identity`` dependency."""
    try:
        from fastapi import Depends
    except ImportError:
        raise ConfigurationError(
            "identity= is a FastAPI dependency and needs FastAPI "
            '(pip install "unilimit[fastapi]"); without FastAPI pass user_id= instead'
        ) from None
    return inspect.Parameter("identity", inspect.Parameter.KEYWORD_ONLY, default=Depends(identity))


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
        self._rules = build_rules(
            rules,
            rate=rate,
            limit=limit,
            window=window,
            key=key,
            default_key="user" if identity else "ip",
            algorithm=algorithm,
            burst=burst,
            cost=cost,
            methods=methods,
            when=when,
            on_missing=on_missing,
            fail_mode=fail_mode,
            action=action,
            owner=type(self).__name__,
        )
        if identity is not None and not callable(identity):
            raise ConfigurationError("identity must be a FastAPI dependency (a callable)")
        self._limiter = limiter
        self._per_route = per_route
        self._route_rules: dict[tuple[str, str], tuple[Rule, ...]] = {}
        if limiter is not None:
            self._resolve(limiter)  # unknown rule names fail at import time, not per request

        params = self._connection_parameters()
        if identity is not None:
            params.append(identity_parameter(identity))
        self._has_identity = identity is not None
        self.__signature__ = inspect.Signature(params, return_annotation=Decision)

    def _connection_parameters(self) -> list[inspect.Parameter]:
        raise NotImplementedError

    def __repr__(self) -> str:
        names = ", ".join(r.name if isinstance(r, Rule) else repr(r) for r in self._rules)
        return f"{type(self).__name__}({names})"

    def _resolve(self, limiter: Limiter) -> tuple[Rule, ...]:
        return resolve_rules(limiter, self._rules)

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
            cached = tuple(scoped_rule(rule, f"{methods} {path}") for rule in rules)
            self._route_rules[cache_key] = cached
        return cached

    def _limiter_for(self, conn: HTTPConnection, state: RequestState) -> Limiter:
        limiter = self._limiter or state.limiter or app_state(conn, APP_STATE_LIMITER)
        if not isinstance(limiter, Limiter):
            raise ConfigurationError(
                "no Limiter found: call unilimit.integrations.fastapi.setup(app, limiter) "
                f"or pass {type(self).__name__}(..., limiter=limiter)"
            )
        return limiter


def app_state(conn: HTTPConnection, name: str) -> Any:
    return getattr(conn.app.state, name, None) if "app" in conn.scope else None


_warned: set[str] = set()


def warn_once(message: str) -> None:
    if message not in _warned:
        _warned.add(message)
        logger.warning(message)


__all__ = [
    "APP_STATE_LIMITER",
    "APP_STATE_RESPONSE",
    "Policies",
    "app_state",
    "identity_parameter",
    "warn_once",
]
