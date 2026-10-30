"""Core models for concurrent connection limits and leases."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from unilimit.core.errors import ConfigurationError
from unilimit.core.identity import Identity


@dataclass(frozen=True, slots=True)
class ConcurrencyLimit:
    """A concurrency limit defining maximum simultaneous active connections.

    Unlike a rate limit (which restricts operations over a moving or fixed time window),
    a concurrency limit bounds the number of simultaneous active leases held by an
    identity at any single moment.
    """

    name: str
    max_concurrent: int
    key: str | Callable[..., Any] = "ip"
    lease_ttl: float = 30.0
    heartbeat_interval: float | None = None

    def __post_init__(self) -> None:
        if not self.name or not isinstance(self.name, str):
            raise ConfigurationError(
                f"ConcurrencyLimit name must be a non-empty string, got {self.name!r}"
            )
        if not isinstance(self.max_concurrent, int) or self.max_concurrent < 1:
            raise ConfigurationError(
                f"max_concurrent must be a positive integer >= 1, got {self.max_concurrent!r}"
            )
        if not isinstance(self.lease_ttl, (int, float)) or self.lease_ttl <= 0:
            raise ConfigurationError(
                f"lease_ttl must be a positive number of seconds, got {self.lease_ttl!r}"
            )
        hb = self.heartbeat_interval
        if hb is not None and (not isinstance(hb, (int, float)) or hb <= 0 or hb >= self.lease_ttl):
            raise ConfigurationError(
                f"heartbeat_interval must be positive and strictly less than lease_ttl "
                f"({self.lease_ttl}s), got {hb!r}"
            )

    @property
    def effective_heartbeat_interval(self) -> float:
        """Interval between lease renewal heartbeats (defaults to half of lease_ttl)."""
        if self.heartbeat_interval is not None:
            return float(self.heartbeat_interval)
        return float(self.lease_ttl) / 2.0


@dataclass(frozen=True, slots=True)
class LeaseDecision:
    """The outcome of an attempt to acquire or verify a concurrency lease."""

    allowed: bool
    current: int
    max_concurrent: int
    lease_id: str
    key: str
    identity: Identity
    limit_name: str
    retry_after: float | None = None

    @property
    def remaining(self) -> int:
        """Available concurrency slots remaining after this evaluation."""
        if not self.allowed:
            return 0
        return max(0, self.max_concurrent - self.current)


def generate_lease_id() -> str:
    """Generate a unique identifier for an individual connection lease."""
    return uuid.uuid4().hex
