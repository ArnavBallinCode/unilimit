"""DRF authentication for the tests (its own module: DRF imports it while loading views)."""

from __future__ import annotations

from dataclasses import dataclass

from rest_framework.authentication import BaseAuthentication
from rest_framework.request import Request


@dataclass(frozen=True)
class HeaderUser:
    """A user authenticated by DRF (not by Django's session middleware)."""

    pk: str
    is_authenticated: bool = True


class HeaderAuthentication(BaseAuthentication):
    """``Authorization: User <name>``: stands in for token or JWT authentication."""

    def authenticate(self, request: Request) -> tuple[HeaderUser, None] | None:
        header = request.META.get("HTTP_AUTHORIZATION", "")
        if not header.startswith("User "):
            return None
        return HeaderUser(header.removeprefix("User ")), None


SEEN_EXCEPTIONS: list[type[Exception]] = []


def recording_exception_handler(exc: Exception, context: dict[str, object]) -> object:
    """A project exception handler, as configured with ``EXCEPTION_HANDLER``."""
    from rest_framework.views import exception_handler

    SEEN_EXCEPTIONS.append(type(exc))
    return exception_handler(exc, context)
