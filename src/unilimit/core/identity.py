"""Identities, key sources, client-IP resolution and trusted proxies."""

from __future__ import annotations

import ipaddress
import logging
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import cached_property
from typing import Literal, TypeAlias

from unilimit._logging import logger, throttled
from unilimit.core.context import RequestContext
from unilimit.core.errors import ConfigurationError
from unilimit.core.keys import validate_kind

IPAddress: TypeAlias = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork: TypeAlias = ipaddress.IPv4Network | ipaddress.IPv6Network

MAX_IDENTITY_LENGTH = 1024

BuiltinKey = Literal["ip", "user", "user_or_ip", "api_key", "tenant", "global"]
KeyFunc = Callable[[RequestContext], "str | int | None"]
KeySource = Literal["ip", "user", "api_key", "tenant", "global"] | str | KeyFunc
"""A built-in source, ``"header:<Name>"`` (stored lower-cased), or a callable."""
KeySpec = BuiltinKey | str | KeyFunc | Sequence["BuiltinKey | str | KeyFunc"]

HEADER_PREFIX = "header:"
_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")  # an RFC 9110 token

_BUILTIN_SOURCES = frozenset({"ip", "user", "api_key", "tenant", "global"})


# -- identities --------------------------------------------------------------------------


def coerce_identity_value(value: object) -> str | None:
    """Return a valid identity string, or ``None`` if ``value`` is missing or malformed.

    Valid: ``str`` (non-blank, at most 1024 characters) or ``int`` (not ``bool``).
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str) and value.strip() and len(value) <= MAX_IDENTITY_LENGTH:
        return value
    return None


@dataclass(frozen=True, slots=True)
class Identity:
    """Who a counter belongs to. The value never appears in reprs, logs or keys."""

    kind: str
    value: str = field(repr=False)

    def __post_init__(self) -> None:
        validate_kind(self.kind)
        if coerce_identity_value(self.value) != self.value:
            raise ValueError(
                "identity value must be a non-blank str of at most "
                f"{MAX_IDENTITY_LENGTH} characters"
            )

    @classmethod
    def of(cls, identity: str | Identity) -> Identity:
        return identity if isinstance(identity, Identity) else cls("custom", identity)


def normalise_key_spec(spec: KeySpec) -> tuple[KeySource, ...]:
    """Validate a key spec and flatten it into an ordered fallback chain."""
    items: Sequence[BuiltinKey | str | KeyFunc]
    if isinstance(spec, str):
        items = (spec,)
    elif isinstance(spec, Sequence):
        items = spec
    elif callable(spec):
        items = (spec,)
    else:
        raise ConfigurationError(f"invalid key spec: {spec!r}")
    if not items:
        raise ConfigurationError("key spec must not be empty")
    out: list[KeySource] = []
    for item in items:
        if item == "user_or_ip":
            out.extend(("user", "ip"))
        elif isinstance(item, str) and item.lower().startswith(HEADER_PREFIX):
            name = item[len(HEADER_PREFIX) :].strip()
            if not _HEADER_NAME.fullmatch(name):
                raise ConfigurationError(f"invalid header key {item!r}; use 'header:<Name>'")
            out.append(HEADER_PREFIX + name.lower())
        elif isinstance(item, str):
            if item not in _BUILTIN_SOURCES:
                raise ConfigurationError(
                    f"unknown key {item!r}; expected one of 'ip', 'user', 'user_or_ip', "
                    "'api_key', 'tenant', 'global', 'header:<Name>' or a callable"
                )
            out.append(item)
        elif callable(item):
            out.append(item)
        else:
            raise ConfigurationError(f"invalid key source: {item!r}")
    return tuple(dict.fromkeys(out))


# -- IP handling -------------------------------------------------------------------------


def parse_ip(text: str) -> IPAddress | None:
    """Parse an address from a header or socket value.

    Accepts optional quotes, ``[v6]:port``, ``v4:port`` and zone ids. IPv4-mapped IPv6
    addresses are normalised to IPv4. Returns ``None`` for anything else, including
    RFC 7239 obfuscated identifiers (``_hidden``) and ``unknown``.
    """
    s = text.strip().strip('"').strip()
    if s.startswith("["):
        end = s.find("]")
        if end == -1:
            return None
        s = s[1:end]
    elif s.count(":") == 1:
        s = s.split(":", 1)[0]
    s = s.split("%", 1)[0]
    try:
        addr = ipaddress.ip_address(s)
    except ValueError:
        return None
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    return addr


def normalise_client_ip(addr: IPAddress, ipv6_prefix: int, ipv4_prefix: int = 32) -> str:
    """The address, or its network when grouped: IPv6 by ``/ipv6_prefix`` (default
    /64), IPv4 by ``/ipv4_prefix`` (default /32, meaning no grouping)."""
    if isinstance(addr, ipaddress.IPv4Address):
        if ipv4_prefix == 32:
            return str(addr)
        return str(ipaddress.IPv4Network((addr, ipv4_prefix), strict=False))
    if ipv6_prefix == 128:
        return str(addr)
    return str(ipaddress.IPv6Network((addr, ipv6_prefix), strict=False))


_PROXY_HEADERS = {"x-forwarded-for", "x-real-ip", "forwarded"}


class TrustedProxies:
    """Derive the client IP from forwarding headers set by proxies you control.

    The header is read **only** when the transport peer is inside ``networks``. Entries
    are walked from right to left, skipping trusted addresses; the first untrusted
    address is the client. A malformed entry stops the walk and the last trusted hop is
    used, so the request is never attributed to a forged value.
    """

    __slots__ = ("header", "networks")

    def __init__(self, networks: Iterable[str], *, header: str = "X-Forwarded-For") -> None:
        if isinstance(networks, str):
            networks = (networks,)
        parsed: list[IPNetwork] = []
        for net in networks:
            try:
                parsed.append(ipaddress.ip_network(net, strict=False))
            except ValueError as exc:
                raise ConfigurationError(f"invalid trusted proxy network {net!r}") from exc
        if not parsed:
            raise ConfigurationError("TrustedProxies requires at least one network")
        if any(n.prefixlen == 0 for n in parsed):
            logger.warning(
                "TrustedProxies trusts every address (/0). Any client can spoof its IP "
                "unless all traffic reaches this app only through your proxies."
            )
        lheader = header.lower()
        if lheader not in _PROXY_HEADERS:
            raise ConfigurationError(
                f"unsupported proxy header {header!r}; use X-Forwarded-For, X-Real-IP or Forwarded"
            )
        self.networks: tuple[IPNetwork, ...] = tuple(parsed)
        self.header = lheader

    def is_trusted(self, addr: IPAddress) -> bool:
        return any(addr.version == net.version and addr in net for net in self.networks)

    def _entries(self, raw: str) -> list[str | None]:
        if self.header == "x-real-ip":
            return [raw]
        if self.header == "x-forwarded-for":
            return [*raw.split(",")]
        entries: list[str | None] = []
        for element in raw.split(","):
            value: str | None = None
            for pair in element.split(";"):
                name, sep, val = pair.strip().partition("=")
                if sep and name.strip().lower() == "for":
                    value = val
            entries.append(value)
        return entries

    def resolve(self, peer: IPAddress, headers: Mapping[str, str]) -> IPAddress:
        if not self.is_trusted(peer):
            return peer
        raw = headers.get(self.header)
        if raw is None:
            return peer
        candidate = peer
        for entry in reversed(self._entries(raw)):
            addr = parse_ip(entry) if entry is not None else None
            if addr is None:
                return candidate
            candidate = addr
            if not self.is_trusted(addr):
                return addr
        return candidate

    def __repr__(self) -> str:
        nets = ", ".join(str(n) for n in self.networks)
        return f"TrustedProxies([{nets}], header={self.header!r})"


# -- resolution --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IdentityConfig:
    trusted_proxies: TrustedProxies | None = None
    ipv6_prefix: int = 64
    ipv4_prefix: int = 32
    api_key_header: str = "x-api-key"
    tenant_header: str | None = None

    def __post_init__(self) -> None:
        if (
            isinstance(self.ipv6_prefix, bool)
            or not isinstance(self.ipv6_prefix, int)
            or not 1 <= self.ipv6_prefix <= 128
        ):
            raise ConfigurationError("ipv6_prefix must be an integer between 1 and 128")
        if (
            isinstance(self.ipv4_prefix, bool)
            or not isinstance(self.ipv4_prefix, int)
            or not 1 <= self.ipv4_prefix <= 32
        ):
            raise ConfigurationError("ipv4_prefix must be an integer between 1 and 32")
        object.__setattr__(self, "api_key_header", self.api_key_header.lower())
        if self.tenant_header is not None:
            object.__setattr__(self, "tenant_header", self.tenant_header.lower())


class IdentityResolver:
    """Resolves identities for one request, computing the client IP at most once."""

    def __init__(self, ctx: RequestContext, config: IdentityConfig) -> None:
        self.ctx = ctx
        self.config = config

    @cached_property
    def client_addr(self) -> IPAddress | str | None:
        """The client address before IPv6 aggregation.

        It is an IP address, an opaque transport identifier (e.g. a unix socket path),
        or ``None`` when unknown.
        """
        ctx, cfg = self.ctx, self.config
        if ctx.client_ip is not None:
            explicit = parse_ip(ctx.client_ip)
            return explicit if explicit is not None else coerce_identity_value(ctx.client_ip)
        if ctx.peer is None:
            return None
        peer = parse_ip(ctx.peer)
        if peer is None:
            # Non-IP transport peers (unix sockets, test clients) come from the server,
            # not the client, so they are safe to use as an opaque identity.
            return coerce_identity_value(ctx.peer)
        if cfg.trusted_proxies is not None:
            return cfg.trusted_proxies.resolve(peer, ctx.headers)
        return peer

    @cached_property
    def client_ip(self) -> str | None:
        """The ``ip`` identity value: the client address with IPv6 aggregated."""
        addr = self.client_addr
        if addr is None or isinstance(addr, str):
            return addr
        return normalise_client_ip(addr, self.config.ipv6_prefix, self.config.ipv4_prefix)

    def _raw(self, source: KeySource) -> tuple[str, object]:
        ctx, cfg = self.ctx, self.config
        if callable(source):
            return "custom", source(ctx)
        if source == "ip":
            return "ip", self.client_ip
        if source == "user":
            return "user", ctx.user_id
        if source == "api_key":
            return "api_key", ctx.headers.get(cfg.api_key_header)
        if source == "tenant":
            if cfg.tenant_header is None:
                raise ConfigurationError("key 'tenant' requires Limiter(tenant_header=...)")
            return "tenant", ctx.headers.get(cfg.tenant_header)
        if source.startswith(HEADER_PREFIX):
            name = source[len(HEADER_PREFIX) :]
            value = ctx.headers.get(name)
            # The header name is part of the identity, so different headers in one
            # fallback chain never share a counter.
            return "header", None if value is None else f"{name}:{value}"
        return "global", "*"

    def resolve(self, sources: Sequence[KeySource], rule_name: str) -> Identity | None:
        """First identity in the fallback chain that resolves, or ``None``."""
        for source in sources:
            kind, raw = self._raw(source)
            value = coerce_identity_value(raw)
            if value is not None:
                return Identity(kind, value)
            if raw is not None:
                throttled.log(
                    logging.WARNING,
                    f"malformed_identity:{rule_name}",
                    "rule %r: malformed %s identity ignored (value redacted)",
                    rule_name,
                    kind,
                )
        return None
