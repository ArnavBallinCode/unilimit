"""``UniLimitMiddleware``: global rules and rate-limit headers for every request."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from asgiref.sync import iscoroutinefunction, markcoroutinefunction
from django.http import HttpRequest, HttpResponseBase

from unilimit.integrations.django._request import (
    add_state_headers,
    aevaluate,
    async_user_id,
    build_context,
    rejection,
    request_state,
    sync_user_id,
)
from unilimit.integrations.django.conf import get_config


class UniLimitMiddleware:
    """Enforces the limiter's exemptions, access lists and global rules on every request,
    and adds rate-limit headers to every response.

    Works in sync (WSGI) and async (ASGI) middleware chains. In an async chain it uses
    only the async backend and resolves the user with ``await request.auser()``.

    Place it **after** ``SessionMiddleware`` and ``AuthenticationMiddleware`` so global
    rules keyed on ``"user"`` see the logged-in user. Global rules run before URL
    resolution, so requests that end in a 404 count too.

    Headers describe the most restrictive counter of every UniLimit step in the request:
    global rules, ``@ratelimit`` and the DRF throttles. Headers already set by the view
    are kept.
    """

    sync_capable = True
    async_capable = True

    def __init__(
        self,
        get_response: Callable[[HttpRequest], Any],
    ) -> None:
        self.get_response = get_response
        self.async_mode = iscoroutinefunction(get_response)
        if self.async_mode:
            markcoroutinefunction(self)

    def __call__(self, request: HttpRequest) -> Any:
        if self.async_mode:
            return self.__acall__(request)
        config = get_config()
        limiter = config.limiter
        state = request_state(request)
        state.limiter, state.response, state.middleware = limiter, config.response, True
        user_id = sync_user_id(request) if limiter.needs_user(include_global=True) else None
        decision = limiter.evaluate(build_context(request, user_id))
        state.record(decision)
        if not decision.allowed:
            return rejection(request, decision, config.response)
        response: HttpResponseBase = self.get_response(request)
        add_state_headers(response, state, config.response)
        return response

    async def __acall__(self, request: HttpRequest) -> HttpResponseBase:
        config = get_config()
        limiter = config.limiter
        state = request_state(request)
        state.limiter, state.response, state.middleware = limiter, config.response, True
        needs_user = limiter.needs_user(include_global=True)
        user_id = await async_user_id(request) if needs_user else None
        ctx = build_context(request, user_id)
        decision = await aevaluate(limiter, request, ctx, include_global=True)
        state.record(decision)
        if not decision.allowed:
            response: HttpResponseBase = rejection(request, decision, config.response)
            return response
        get_response: Callable[[HttpRequest], Awaitable[HttpResponseBase]] = self.get_response
        response = await get_response(request)
        add_state_headers(response, state, config.response)
        return response


__all__ = ["UniLimitMiddleware"]
