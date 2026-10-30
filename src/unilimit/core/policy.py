"""Sans-I/O policy evaluation shared by the sync and async drivers.

``plan()`` turns a request and its candidate rules into a batch of backend operations,
or short-circuits to a :class:`Decision`. ``decide()`` turns the backend's batch result
into a :class:`Decision`. Neither performs I/O.
"""

from __future__ import annotations

import ipaddress
import logging
from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import dataclass
from fnmatch import fnmatchcase

from unilimit._logging import throttled
from unilimit.backends.base import BatchResult, ConsumeOp, OpResult
from unilimit.core.context import RequestContext
from unilimit.core.errors import ConfigurationError, IdentityError
from unilimit.core.identity import IdentityConfig, IdentityResolver, IPNetwork
from unilimit.core.keys import identity_digest, rate_limit_key
from unilimit.core.rate import US_PER_SECOND
from unilimit.core.result import Decision, RateLimitResult, Reason
from unilimit.core.rule import FailMode, OnMissing, Rule


class AccessList:
    """Static allow- or denylist matched against the client IP, user id or API key.

    Values live only in process memory; they are never written to the backend.
    """

    __slots__ = ("api_keys", "networks", "users")

    def __init__(
        self,
        *,
        networks: Iterable[str] = (),
        users: Iterable[str | int] = (),
        api_keys: Iterable[str] = (),
    ) -> None:
        if isinstance(networks, str) or isinstance(users, str) or isinstance(api_keys, str):
            raise ConfigurationError("AccessList fields must be collections, not strings")
        try:
            self.networks: tuple[IPNetwork, ...] = tuple(
                ipaddress.ip_network(n, strict=False) for n in networks
            )
        except ValueError as exc:
            raise ConfigurationError(f"invalid network in AccessList: {exc}") from exc
        self.users = frozenset(str(u) for u in users)
        self.api_keys = frozenset(api_keys)

    def matches(self, resolver: IdentityResolver) -> bool:
        addr = resolver.client_addr
        if (
            addr is not None
            and not isinstance(addr, str)
            and any(addr.version == n.version and addr in n for n in self.networks)
        ):
            return True
        user = resolver.ctx.user_id
        if user is not None and not isinstance(user, bool) and str(user) in self.users:
            return True
        if self.api_keys:
            api_key = resolver.ctx.headers.get(resolver.config.api_key_header)
            if api_key is not None and api_key in self.api_keys:
                return True
        return False

    def __repr__(self) -> str:
        return (
            f"AccessList(networks={len(self.networks)}, users={len(self.users)}, "
            f"api_keys={len(self.api_keys)})"
        )


@dataclass(frozen=True, slots=True)
class PolicyConfig:
    namespace: str
    identity: IdentityConfig
    identity_secret: bytes | None
    on_missing: OnMissing
    fail_mode: FailMode
    allow: AccessList | None = None
    deny: AccessList | None = None
    exempt_paths: tuple[str, ...] = ()
    exempt: Callable[[RequestContext], bool] | None = None


@dataclass(frozen=True, slots=True)
class PlannedOp:
    rule: Rule
    op: ConsumeOp


@dataclass(frozen=True, slots=True)
class Plan:
    entries: tuple[PlannedOp, ...]

    @property
    def ops(self) -> tuple[ConsumeOp, ...]:
        return tuple(e.op for e in self.entries)


def _applies(rule: Rule, ctx: RequestContext) -> bool:
    if rule.methods is not None and ctx.method not in rule.methods:
        return False
    if rule.paths is not None and not any(fnmatchcase(ctx.path, p) for p in rule.paths):
        return False
    return rule.when is None or bool(rule.when(ctx))


def resolve_cost(rule: Rule, ctx: RequestContext, override_cost: int | None = None) -> int:
    if override_cost is not None:
        if (
            isinstance(override_cost, bool)
            or not isinstance(override_cost, int)
            or override_cost < 1
        ):
            raise ValueError(f"cost must be a positive integer, got {override_cost!r}")
        return override_cost
    if isinstance(rule.cost, int):
        return rule.cost
    cost = rule.cost(ctx)
    if isinstance(cost, bool) or not isinstance(cost, int) or cost < 1:
        raise ValueError(f"rule {rule.name!r}: cost callable returned {cost!r}")
    return cost


def plan(
    ctx: RequestContext,
    rules: Sequence[Rule],
    config: PolicyConfig,
    skip_keys: Collection[str] = (),
    cost: int | None = None,
) -> Plan | Decision:
    """Pipeline steps 1-5 of ARCHITECTURE.md §6.1. Deterministic and side-effect free
    apart from logging and calling user-supplied predicates.

    Counters whose storage key is in ``skip_keys`` were already consumed earlier in the
    same request (for example by middleware) and are left out.
    """
    if any(fnmatchcase(ctx.path, p) for p in config.exempt_paths) or (
        config.exempt is not None and config.exempt(ctx)
    ):
        return Decision(allowed=True, reason=Reason.EXEMPT)

    resolver = IdentityResolver(ctx, config.identity)
    if config.deny is not None and config.deny.matches(resolver):
        return Decision(allowed=False, reason=Reason.DENIED)
    if config.allow is not None and config.allow.matches(resolver):
        return Decision(allowed=True, reason=Reason.EXEMPT)

    entries: dict[str, PlannedOp] = {}
    for rule in rules:
        if not _applies(rule, ctx):
            continue
        identity = resolver.resolve(rule.key, rule.name)
        if identity is None:
            mode: OnMissing = (rule.on_missing or config.on_missing) if rule.enforced else "skip"
            if rule.enforced:
                throttled.log(
                    logging.WARNING,
                    f"missing_identity:{rule.name}",
                    "rule %r: no identity resolved (on_missing=%s)",
                    rule.name,
                    mode,
                )
            if mode == "skip":
                continue
            if mode == "raise":
                raise IdentityError(f"rule {rule.name!r}: no identity could be resolved")
            return Decision(allowed=False, reason=Reason.MISSING_IDENTITY, rule=rule)

        digest = identity_digest(identity.kind, identity.value, config.identity_secret)
        key = rate_limit_key(config.namespace, rule.name, rule.algorithm, digest)
        if key in skip_keys:
            continue
        op = ConsumeOp(key, rule.spec, resolve_cost(rule, ctx, cost), rule.enforced)
        existing = entries.get(key)
        if existing is not None:
            if existing.op == op:
                continue
            raise ConfigurationError(
                f"rules named {rule.name!r} with the same algorithm but different settings "
                "apply to the same request; rule names must be unique per configuration"
            )
        entries[key] = PlannedOp(rule, op)
    return Plan(tuple(entries.values()))


def _seconds(us: int) -> float:
    return us / US_PER_SECOND


def to_result(rule: Rule, result: OpResult, key: str | None = None) -> RateLimitResult:
    return RateLimitResult(
        rule=rule,
        allowed=result.allowed,
        limit=rule.capacity,
        remaining=result.remaining,
        reset_after=_seconds(result.reset_after_us),
        retry_after=None if result.retry_after_us is None else _seconds(result.retry_after_us),
        enforced=rule.enforced,
        storage_key=key,
    )


def degraded_result(rule: Rule, allowed: bool, key: str | None = None) -> RateLimitResult:
    return RateLimitResult(
        rule=rule,
        allowed=allowed,
        limit=rule.capacity,
        remaining=0,
        reset_after=0.0,
        retry_after=None,
        enforced=rule.enforced,
        degraded=True,
        storage_key=key,
    )


def decide(planned: Plan, batch: BatchResult) -> Decision:
    """Pipeline step 7: build the Decision and select the binding result."""
    if len(batch.results) != len(planned.entries):
        raise ValueError("batch result does not match the plan")
    results = tuple(
        to_result(e.rule, r, e.op.key) for e, r in zip(planned.entries, batch.results, strict=True)
    )
    if batch.committed:
        enforced = [r for r in results if r.enforced]
        # min() returns the first minimal element, so ties follow declaration order.
        binding = min(enforced, key=lambda r: (r.remaining, r.reset_after)) if enforced else None
        return Decision(allowed=True, reason=Reason.ALLOWED, results=results, binding=binding)
    rejecting = [r for r in results if r.enforced and not r.allowed]
    binding = (
        max(
            rejecting,
            key=lambda r: float("inf") if r.retry_after is None else r.retry_after,
        )
        if rejecting
        else None
    )
    return Decision(
        allowed=False,
        reason=Reason.RATE_LIMITED,
        results=results,
        binding=binding,
        partial_commit=batch.partial_commit,
    )


def is_fail_closed(rule: Rule, default: FailMode) -> bool:
    return (rule.fail_mode or default) == "closed"


def decide_unavailable(
    planned: Plan, default: FailMode, *, partial_commit: bool = False
) -> Decision:
    """The backend was unavailable: apply each rule's fail mode (ARCHITECTURE.md §10)."""
    closed = any(e.rule.enforced and is_fail_closed(e.rule, default) for e in planned.entries)
    results = tuple(degraded_result(e.rule, not closed, e.op.key) for e in planned.entries)
    if closed:
        return Decision(
            allowed=False,
            reason=Reason.BACKEND_UNAVAILABLE,
            results=results,
            degraded=True,
            partial_commit=partial_commit,
        )
    return Decision(
        allowed=True,
        reason=Reason.ALLOWED,
        results=results,
        degraded=True,
        partial_commit=partial_commit,
    )
