"""The ``RateLimit`` FastAPI dependency: endpoint- and router-level limits."""

from __future__ import annotations

import inspect
from typing import Any

from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import Response

from unilimit.core.errors import RateLimitExceeded as CoreRateLimitExceeded
from unilimit.core.result import Decision
from unilimit.http import ResponseConfig, ResponseSpec, rate_limit_headers, rejection_response
from unilimit.integrations.starlette._policies import (
    APP_STATE_LIMITER,
    APP_STATE_RESPONSE,
    Policies,
    app_state,
    warn_once,
)
from unilimit.integrations.starlette._state import build_context, request_state, scope_user


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
        config = state.response or app_state(request, APP_STATE_RESPONSE) or ResponseConfig()
        if not state.middleware and limiter.global_rules:
            warn_once(
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


__all__ = ["APP_STATE_LIMITER", "APP_STATE_RESPONSE", "Policies", "RateLimit", "RateLimitExceeded"]
