from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from types import MappingProxyType
from typing import Any, get_args
from urllib.parse import urlsplit

from unilimit._logging import logger
from unilimit.backends.base import Backend, BatchResult, ConsumeOp, OpResult
from unilimit.backends.memory import MemoryBackend
from unilimit.core.context import RequestContext
from unilimit.core.errors import BackendUnavailable, ConfigurationError, UnknownRuleError
from unilimit.core.identity import Identity, IdentityConfig, TrustedProxies
from unilimit.core.keys import (
    identity_digest,
    normalise_secret,
    rate_limit_key,
    validate_namespace,
)
from unilimit.core.policy import (
    AccessList,
    Plan,
    PolicyConfig,
    decide,
    decide_unavailable,
    degraded_result,
    is_fail_closed,
    plan,
    to_result,
)
from unilimit.core.result import Decision, RateLimitResult
from unilimit.core.rule import FailMode, OnMissing, Rule

_REDIS_OPTIONS = (
    "cluster",
    "socket_timeout",
    "socket_connect_timeout",
    "max_connections",
    "pool_timeout",
    "cooldown",
    "clock",
    "client_options",
)


class Limiter:
    """Entry point for rate limiting, usable from both sync and async code.

    Every async method is the ``a``-prefixed twin of a sync method with identical
    semantics. Sync methods never need an event loop; async methods never block it on
    backend I/O.
    """

    def __init__(
        self,
        backend: Backend,
        *,
        rules: Iterable[Rule] = (),
        global_rules: Iterable[Rule | str] = (),
        namespace: str = "unilimit",
        fail_mode: FailMode = "open",
        on_missing: OnMissing = "deny",
        trusted_proxies: TrustedProxies | None = None,
        ipv6_prefix: int = 64,
        identity_secret: str | bytes | None = None,
        api_key_header: str = "X-API-Key",
        tenant_header: str | None = None,
        allow: AccessList | None = None,
        deny: AccessList | None = None,
        exempt_paths: Iterable[str] = (),
        exempt: Callable[[RequestContext], bool] | None = None,
    ) -> None:
        if not isinstance(backend, Backend):
            raise ConfigurationError(
                "backend must implement both the sync and async backend protocols"
            )
        if fail_mode not in get_args(FailMode):
            raise ConfigurationError(f"fail_mode must be 'open' or 'closed', got {fail_mode!r}")
        if on_missing not in get_args(OnMissing):
            raise ConfigurationError(
                f"on_missing must be 'deny', 'raise' or 'skip', got {on_missing!r}"
            )
        if isinstance(exempt_paths, str):
            raise ConfigurationError("exempt_paths must be a collection of patterns")

        registry: dict[str, Rule] = {}
        for rule in rules:
            if not isinstance(rule, Rule):
                raise ConfigurationError(f"rules must contain Rule objects, got {rule!r}")
            if rule.name in registry and registry[rule.name] != rule:
                raise ConfigurationError(f"duplicate rule name {rule.name!r}")
            registry[rule.name] = rule
        self._rules: Mapping[str, Rule] = MappingProxyType(registry)
        self._global_rules: tuple[Rule, ...] = tuple(self._lookup(r) for r in global_rules)

        self.backend = backend
        self._config = PolicyConfig(
            namespace=validate_namespace(namespace),
            identity=IdentityConfig(
                trusted_proxies=trusted_proxies,
                ipv6_prefix=ipv6_prefix,
                api_key_header=api_key_header,
                tenant_header=tenant_header,
            ),
            identity_secret=normalise_secret(identity_secret),
            on_missing=on_missing,
            fail_mode=fail_mode,
            allow=allow,
            deny=deny,
            exempt_paths=tuple(exempt_paths),
            exempt=exempt,
        )

    @classmethod
    def from_url(cls, url: str, **kwargs: Any) -> Limiter:
        """Build a limiter from a backend URL.

        Schemes: ``memory://``, ``redis://``, ``rediss://`` and ``unix://``. Backend
        options (``cluster``, ``socket_timeout``, ``socket_connect_timeout``,
        ``max_connections``, ``pool_timeout``, ``cooldown``, ``clock``,
        ``client_options``) are passed to
        the backend; every other keyword goes to :class:`Limiter`.
        """
        scheme = urlsplit(url).scheme.lower()
        if scheme == "memory":
            memory_kwargs = {k: kwargs.pop(k) for k in ("clock", "max_keys") if k in kwargs}
            redis_only = sorted(k for k in _REDIS_OPTIONS if k in kwargs and k != "clock")
            if redis_only:
                raise ConfigurationError(f"options {redis_only} do not apply to memory://")
            return cls(MemoryBackend(**memory_kwargs), **kwargs)
        if scheme in {"redis", "rediss", "unix"}:
            try:
                from unilimit.backends.redis import RedisBackend
            except ImportError as exc:
                raise ConfigurationError(
                    'Redis URLs require redis-py: pip install "unilimit[redis]"'
                ) from exc
            backend_kwargs = {k: kwargs.pop(k) for k in _REDIS_OPTIONS if k in kwargs}
            return cls(RedisBackend(url, **backend_kwargs), **kwargs)
        raise ConfigurationError(f"unsupported backend URL scheme {scheme!r}")

    # -- configuration -----------------------------------------------------------------

    @property
    def rules(self) -> Mapping[str, Rule]:
        """Named rules registered on this limiter."""
        return self._rules

    @property
    def global_rules(self) -> tuple[Rule, ...]:
        return self._global_rules

    @property
    def namespace(self) -> str:
        return self._config.namespace

    @property
    def fail_mode(self) -> FailMode:
        return self._config.fail_mode

    def _lookup(self, rule: Rule | str) -> Rule:
        if isinstance(rule, Rule):
            return rule
        if isinstance(rule, str):
            try:
                return self._rules[rule]
            except KeyError:
                raise UnknownRuleError(f"no rule named {rule!r} is registered") from None
        raise ConfigurationError(f"expected a Rule or a rule name, got {rule!r}")

    def key_for(self, rule: Rule | str, identity: str | Identity) -> str:
        """The storage key for ``rule`` and ``identity`` (useful for operations tooling)."""
        r = self._lookup(rule)
        ident = Identity.of(identity)
        digest = identity_digest(ident.kind, ident.value, self._config.identity_secret)
        return rate_limit_key(self._config.namespace, r.name, r.algorithm, digest)

    def _single_op(
        self, rule: Rule | str, identity: str | Identity, cost: int
    ) -> tuple[Rule, ConsumeOp]:
        r = self._lookup(rule)
        return r, ConsumeOp(self.key_for(r, identity), r.spec, cost, enforce=True)

    def _unavailable_result(self, rule: Rule, exc: BackendUnavailable) -> RateLimitResult:
        closed = is_fail_closed(rule, self._config.fail_mode)
        logger.warning(
            "rate limit backend unavailable; failing %s for rule %r: %s",
            "closed" if closed else "open",
            rule.name,
            exc,
            extra={"unilimit_rule": rule.name},
        )
        return degraded_result(rule, allowed=not closed)

    def _unavailable_decision(self, planned: Plan, exc: BackendUnavailable) -> Decision:
        decision = decide_unavailable(
            planned, self._config.fail_mode, partial_commit=exc.partial_commit
        )
        logger.warning(
            "rate limit backend unavailable; failing %s: %s",
            "open" if decision.allowed else "closed",
            exc,
        )
        return decision

    def _rules_for(self, rules: Iterable[Rule | str], include_global: bool) -> list[Rule]:
        selected = list(self._global_rules) if include_global else []
        selected.extend(self._lookup(r) for r in rules)
        return selected

    # -- single-rule API ---------------------------------------------------------------

    def consume(
        self, rule: Rule | str, identity: str | Identity, *, cost: int = 1
    ) -> RateLimitResult:
        """Atomically evaluate ``rule`` and consume ``cost`` only if it allows.

        A plain string identity is treated as ``Identity("custom", value)``. The rule's
        ``action`` is ignored here: the caller decides what to do with the result.
        """
        r, op = self._single_op(rule, identity, cost)
        try:
            batch = self.backend.consume([op])
        except BackendUnavailable as exc:
            return self._unavailable_result(r, exc)
        return to_result(r, batch.results[0])

    async def aconsume(
        self, rule: Rule | str, identity: str | Identity, *, cost: int = 1
    ) -> RateLimitResult:
        r, op = self._single_op(rule, identity, cost)
        try:
            batch = await self.backend.aconsume([op])
        except BackendUnavailable as exc:
            return self._unavailable_result(r, exc)
        return to_result(r, batch.results[0])

    def peek(self, rule: Rule | str, identity: str | Identity, *, cost: int = 1) -> RateLimitResult:
        """Evaluate without consuming. Never mutates state; never use as an admission check."""
        r, op = self._single_op(rule, identity, cost)
        try:
            results: tuple[OpResult, ...] = self.backend.peek([op])
        except BackendUnavailable as exc:
            return self._unavailable_result(r, exc)
        return to_result(r, results[0])

    async def apeek(
        self, rule: Rule | str, identity: str | Identity, *, cost: int = 1
    ) -> RateLimitResult:
        r, op = self._single_op(rule, identity, cost)
        try:
            results = await self.backend.apeek([op])
        except BackendUnavailable as exc:
            return self._unavailable_result(r, exc)
        return to_result(r, results[0])

    def reset(self, rule: Rule | str, identity: str | Identity) -> None:
        self.backend.reset([self.key_for(rule, identity)])

    async def areset(self, rule: Rule | str, identity: str | Identity) -> None:
        await self.backend.areset([self.key_for(rule, identity)])

    # -- request API -------------------------------------------------------------------

    def evaluate(
        self,
        ctx: RequestContext,
        rules: Sequence[Rule | str] = (),
        *,
        include_global: bool = True,
    ) -> Decision:
        """Evaluate a request against the global rules and ``rules``.

        All applicable rules are consumed atomically: quota is consumed only if every
        enforcing rule allows the request.
        """
        planned = plan(ctx, self._rules_for(rules, include_global), self._config)
        if isinstance(planned, Decision):
            return planned
        if not planned.entries:
            return decide(planned, BatchResult(True, ()))
        try:
            batch = self.backend.consume(planned.ops)
        except BackendUnavailable as exc:
            return self._unavailable_decision(planned, exc)
        return decide(planned, batch)

    async def aevaluate(
        self,
        ctx: RequestContext,
        rules: Sequence[Rule | str] = (),
        *,
        include_global: bool = True,
    ) -> Decision:
        planned = plan(ctx, self._rules_for(rules, include_global), self._config)
        if isinstance(planned, Decision):
            return planned
        if not planned.entries:
            return decide(planned, BatchResult(True, ()))
        try:
            batch = await self.backend.aconsume(planned.ops)
        except BackendUnavailable as exc:
            return self._unavailable_decision(planned, exc)
        return decide(planned, batch)

    # -- lifecycle ---------------------------------------------------------------------

    def close(self) -> None:
        self.backend.close()

    async def aclose(self) -> None:
        await self.backend.aclose()
