"""Request-scoped plumbing shared by the middleware and the ``RateLimit`` dependency."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from starlette.requests import HTTPConnection
from starlette.types import Scope

from unilimit.core.context import RequestContext
from unilimit.core.limiter import Limiter
from unilimit.core.result import Decision, RateLimitResult
from unilimit.http import ResponseConfig

STATE_KEY = "unilimit"

UserResolver = Callable[[HTTPConnection], "str | int | None"]


@dataclass
class RequestState:
    """What UniLimit knows about the current request.

    Stored in ``scope["state"]["unilimit"]``, so endpoints can read it as
    ``request.state.unilimit``.
    """

    limiter: Limiter | None = None
    response: ResponseConfig | None = None
    middleware: bool = False
    results: list[RateLimitResult] = field(default_factory=list)
    """Enforced results of every admitting decision so far, for response headers."""
    consumed: set[str] = field(default_factory=set)
    """Storage keys already consumed in this request; never consumed twice."""
    decisions: list[Decision] = field(default_factory=list)

    def record(self, decision: Decision) -> None:
        self.decisions.append(decision)
        if decision.allowed:
            for result in decision.results:
                if result.storage_key is not None and not result.degraded:
                    self.consumed.add(result.storage_key)
                if result.enforced:
                    self.results.append(result)


def request_state(scope: Scope) -> RequestState:
    state = scope.setdefault("state", {})
    current = state.get(STATE_KEY)
    if not isinstance(current, RequestState):
        current = RequestState()
        state[STATE_KEY] = current
    return current


def scope_user(conn: HTTPConnection) -> str | int | None:
    """The user id set by Starlette's ``AuthenticationMiddleware``, if any."""
    if "user" not in conn.scope:
        return None
    user = conn.scope["user"]
    if not getattr(user, "is_authenticated", False):
        return None
    try:
        identity = user.identity
    except NotImplementedError:
        identity = getattr(user, "display_name", None)
    return identity if isinstance(identity, str | int) and not isinstance(identity, bool) else None


def build_context(conn: HTTPConnection, user_id: Any) -> RequestContext:
    scope = conn.scope
    client = scope.get("client")
    route = scope.get("route")
    return RequestContext(
        method=scope.get("method", "GET"),
        path=scope.get("path", "/"),
        peer=client[0] if client else None,
        headers=[(k.decode("latin-1"), v.decode("latin-1")) for k, v in scope.get("headers", ())],
        user_id=user_id,
        route=getattr(route, "path", None),
        raw=conn,
    )
