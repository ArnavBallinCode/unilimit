"""Request plumbing shared by the middleware, the decorator and the DRF throttles."""

from __future__ import annotations

from collections.abc import Collection, Sequence
from typing import Any

from asgiref.sync import sync_to_async
from django.core.handlers.asgi import ASGIRequest
from django.http import HttpRequest, HttpResponse, HttpResponseBase

from unilimit._logging import logger
from unilimit.core.context import RequestContext
from unilimit.core.limiter import Limiter
from unilimit.core.result import Decision
from unilimit.core.rule import Rule
from unilimit.http import ResponseConfig, rate_limit_headers, rejection_response
from unilimit.integrations._common import RequestState
from unilimit.integrations.django.conf import get_config

STATE_ATTR = "unilimit"


def http_request(request: Any) -> HttpRequest:
    """The Django ``HttpRequest`` behind ``request`` (which may be a DRF ``Request``)."""
    inner = getattr(request, "_request", request)
    if not isinstance(inner, HttpRequest):
        raise TypeError(f"expected a Django HttpRequest, got {type(request).__name__}")
    return inner


def request_state(request: Any) -> RequestState:
    """UniLimit's state for this request, stored as ``request.unilimit``."""
    http = http_request(request)
    state = http.__dict__.get(STATE_ATTR)
    if not isinstance(state, RequestState):
        state = RequestState()
        setattr(http, STATE_ATTR, state)
    return state


def user_id_of(user: Any) -> str | int | None:
    """The primary key of an authenticated user, else ``None``."""
    if user is None or not getattr(user, "is_authenticated", False):
        return None
    pk = getattr(user, "pk", None)
    if pk is None or isinstance(pk, bool):
        return None
    return pk if isinstance(pk, str | int) else str(pk)


def sync_user_id(request: Any) -> str | int | None:
    """The user id from ``request.user`` (Django's, or DRF's authenticated user)."""
    return user_id_of(getattr(request, "user", None))


async def async_user_id(request: Any) -> str | int | None:
    """The user id without blocking the event loop (``request.auser()``)."""
    http = http_request(request)
    auser = getattr(http, "auser", None)
    if auser is not None:
        return user_id_of(await auser())
    if "user" in http.__dict__:  # set by a custom middleware; may be lazy
        return await sync_to_async(user_id_of)(http.__dict__["user"])
    return None


def build_context(request: Any, user_id: str | int | None) -> RequestContext:
    http = http_request(request)
    match = http.resolver_match
    peer = http.META.get("REMOTE_ADDR") or None
    return RequestContext(
        method=http.method or "GET",
        path=http.path_info,
        peer=peer if isinstance(peer, str) else None,
        headers=http.headers.items(),
        user_id=user_id,
        route=match.route if match is not None else None,
        raw=request,
    )


async def aevaluate(
    limiter: Limiter,
    request: Any,
    ctx: RequestContext,
    rules: Sequence[Rule | str] = (),
    *,
    include_global: bool,
    skip_keys: Collection[str] = (),
) -> Decision:
    """Evaluate without blocking the event loop.

    Under ASGI the server's long-lived loop uses the async backend. Under WSGI, Django
    runs async views in a short-lived loop per request (``async_to_sync``); async Redis
    clients are bound to their loop, so each request would open (and leak) a new
    connection. There the sync backend runs in a worker thread instead.
    """
    if isinstance(http_request(request), ASGIRequest):
        return await limiter.aevaluate(
            ctx, rules, include_global=include_global, skip_keys=skip_keys
        )
    return await sync_to_async(limiter.evaluate, thread_sensitive=False)(
        ctx, rules, include_global=include_global, skip_keys=skip_keys
    )


def rejection(request: Any, decision: Decision, config: ResponseConfig | None = None) -> Any:
    """The response for a rejected decision: ``REJECTION_VIEW`` or the default."""
    settings = get_config()
    config = config or settings.response
    spec = rejection_response(decision, config)
    if settings.rejection_view is not None:
        response = settings.rejection_view(http_request(request), decision)
        set_missing_headers(response, spec.headers)
        return response
    response = HttpResponse(spec.body, status=spec.status_code, content_type=spec.media_type)
    for name, value in spec.headers:
        response[name] = value
    return response


def set_missing_headers(response: HttpResponseBase, headers: Sequence[tuple[str, str]]) -> None:
    for name, value in headers:
        if not response.has_header(name):
            response[name] = value


def add_state_headers(
    response: HttpResponseBase, state: RequestState, config: ResponseConfig
) -> None:
    """Rate-limit headers for everything UniLimit decided in this request."""
    if state.rejection is not None:
        headers = rate_limit_headers(state.rejection.results, config, rejected=True)
    else:
        headers = rate_limit_headers(state.results, config)
    set_missing_headers(response, headers)


_warned: set[str] = set()


def warn_once(message: str) -> None:
    if message not in _warned:
        _warned.add(message)
        logger.warning(message)


__all__ = [
    "add_state_headers",
    "aevaluate",
    "async_user_id",
    "build_context",
    "http_request",
    "rejection",
    "request_state",
    "sync_user_id",
    "user_id_of",
]
