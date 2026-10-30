"""Drop-in compatibility shim for SlowAPI (FastAPI / Starlette).

Enables AI agents and legacy codebases using ``from slowapi import Limiter`` to
migrate seamlessly to UniLimit's atomic Redis core without changing decorator
patterns or endpoint signatures.
"""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable, Sequence
from typing import Any, TypeVar

from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from unilimit.backends.memory import MemoryBackend
from unilimit.core.limiter import Limiter as CoreLimiter
from unilimit.core.result import Decision, RateLimitResult
from unilimit.core.rule import Rule
from unilimit.http import rate_limit_headers

CallableT = TypeVar("CallableT", bound=Callable[..., Any])


def get_remote_address(request: Request) -> str:
    """SlowAPI's default IP extraction helper."""
    if request.client and request.client.host:
        return request.client.host
    return "127.0.0.1"


class RateLimitExceeded(HTTPException):
    """Exception raised when a limit is exceeded in SlowAPI compatibility mode."""

    def __init__(
        self,
        detail: str = "Rate limit exceeded",
        decision: Decision | RateLimitResult | None = None,
    ) -> None:
        super().__init__(status_code=429, detail=detail)
        self.decision = decision


def _rate_limit_exceeded_handler(request: Request, exc: Exception) -> Response:
    """Default SlowAPI exception handler rendering standard 429 response."""
    detail = getattr(exc, "detail", "Rate limit exceeded")
    headers: dict[str, str] = {}
    decision = getattr(exc, "decision", None)
    if decision is not None:
        headers = dict(rate_limit_headers([decision], rejected=True))
    return JSONResponse(status_code=429, content={"error": detail}, headers=headers)


class Limiter:
    """SlowAPI-compatible Limiter class powered by UniLimit's core engine."""

    def __init__(
        self,
        key_func: Callable[[Request], str] | None = None,
        default_limits: Sequence[str] | None = None,
        storage_uri: str | None = None,
        headers_enabled: bool = True,
        limiter: CoreLimiter | None = None,
        **backend_options: Any,
    ) -> None:
        self.key_func = key_func or get_remote_address
        self.default_limits = list(default_limits) if default_limits else []
        self.headers_enabled = headers_enabled

        if limiter is not None:
            self._core_limiter = limiter
        elif storage_uri:
            self._core_limiter = CoreLimiter.from_url(storage_uri, **backend_options)
        else:
            self._core_limiter = CoreLimiter(MemoryBackend())

    def limit(
        self,
        limit_value: str | Callable[[], str],
        key_func: Callable[[Request], str] | None = None,
        methods: Sequence[str] | None = None,
        cost: int = 1,
    ) -> Callable[[CallableT], CallableT]:
        """SlowAPI-style decorator: ``@limiter.limit("5/minute")``."""

        def decorator(func: CallableT) -> CallableT:
            rule_name = f"{func.__module__}:{func.__qualname__}"
            rate_str = limit_value() if callable(limit_value) else limit_value
            rule = Rule(rule_name, rate=rate_str, cost=cost)

            active_key_func = key_func or self.key_func
            is_async = inspect.iscoroutinefunction(func)

            if is_async:

                @functools.wraps(func)
                async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                    request = self._find_request(args, kwargs)
                    if request is not None and (not methods or request.method in methods):
                        ident = active_key_func(request)
                        decision = await self._core_limiter.aconsume(rule, identity=ident)
                        if not decision.allowed:
                            raise RateLimitExceeded(
                                detail=f"Rate limit exceeded: {rate_str}", decision=decision
                            )
                    return await func(*args, **kwargs)

                return async_wrapper  # type: ignore[return-value]

            @functools.wraps(func)
            def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
                request = self._find_request(args, kwargs)
                if request is not None and (not methods or request.method in methods):
                    ident = active_key_func(request)
                    decision = self._core_limiter.consume(rule, identity=ident)
                    if not decision.allowed:
                        raise RateLimitExceeded(
                            detail=f"Rate limit exceeded: {rate_str}", decision=decision
                        )
                return func(*args, **kwargs)

            return sync_wrapper  # type: ignore[return-value]

        return decorator

    @staticmethod
    def _find_request(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Request | None:
        for arg in args:
            if isinstance(arg, Request):
                return arg
        for val in kwargs.values():
            if isinstance(val, Request):
                return val
        return None
