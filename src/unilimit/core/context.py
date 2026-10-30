from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

HeadersInput = Mapping[str, str] | Iterable[tuple[str, str]]


def normalise_headers(headers: HeadersInput | None) -> Mapping[str, str]:
    """Lower-case header names; repeated headers are joined with ``", "`` in order.

    Joining preserves the order of multiple ``X-Forwarded-For`` lines, which matters for
    trusted-proxy resolution.
    """
    if headers is None:
        return MappingProxyType({})
    pairs = headers.items() if isinstance(headers, Mapping) else headers
    merged: dict[str, str] = {}
    for name, value in pairs:
        lname = name.lower()
        merged[lname] = f"{merged[lname]}, {value}" if lname in merged else value
    return MappingProxyType(merged)


@dataclass(frozen=True, slots=True, eq=False, init=False)
class RequestContext:
    """Framework-neutral view of a request.

    Integrations build this from framework requests. Framework-independent callers can
    build it directly.

    ``peer`` is the transport-level client address. ``client_ip`` is normally left as
    ``None`` and derived from ``peer`` and the limiter's trusted proxies. Setting it
    explicitly asserts that it is already trustworthy.
    """

    method: str
    path: str
    peer: str | None
    headers: Mapping[str, str]
    user_id: str | int | None
    route: str | None
    client_ip: str | None
    raw: Any

    def __init__(
        self,
        *,
        method: str = "GET",
        path: str = "/",
        peer: str | None = None,
        headers: HeadersInput | None = None,
        user_id: str | int | None = None,
        route: str | None = None,
        client_ip: str | None = None,
        raw: Any = None,
    ) -> None:
        object.__setattr__(self, "method", method.upper())
        object.__setattr__(self, "path", path)
        object.__setattr__(self, "peer", peer)
        object.__setattr__(self, "headers", normalise_headers(headers))
        object.__setattr__(self, "user_id", user_id)
        object.__setattr__(self, "route", route)
        object.__setattr__(self, "client_ip", client_ip)
        object.__setattr__(self, "raw", raw)
