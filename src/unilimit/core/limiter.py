from __future__ import annotations

import contextlib
import logging
import threading
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import replace
from time import perf_counter
from types import MappingProxyType
from typing import Any, Literal, get_args
from urllib.parse import urlsplit

from unilimit._logging import throttled
from unilimit.backends.base import (
    AsyncLeaseBackend,
    Backend,
    BatchResult,
    ConsumeOp,
    OpResult,
    SyncLeaseBackend,
)
from unilimit.backends.memory import MemoryBackend
from unilimit.core.concurrency import ConcurrencyLimit, LeaseDecision, generate_lease_id
from unilimit.core.context import RequestContext
from unilimit.core.errors import BackendUnavailable, ConfigurationError, UnknownRuleError
from unilimit.core.identity import Identity, IdentityConfig, TrustedProxies
from unilimit.core.keys import (
    concurrency_key,
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
from unilimit.observability import (
    BackendUnavailableEvent,
    DecisionEvent,
    Emitter,
    Event,
    FallbackEvent,
    Observer,
    RuleOutcome,
    normalise_observers,
    now,
)

_REDIS_OPTIONS = (
    "cluster",
    "sentinels",
    "service_name",
    "sentinel_kwargs",
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
        ipv4_prefix: int = 32,
        identity_secret: str | bytes | None = None,
        api_key_header: str = "X-API-Key",
        tenant_header: str | None = None,
        allow: AccessList | None = None,
        deny: AccessList | None = None,
        exempt_paths: Iterable[str] = (),
        exempt: Callable[[RequestContext], bool] | None = None,
        observer: Observer | Sequence[Observer] | None = None,
        fallback: Literal["memory"] | MemoryBackend | None = None,
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
        self._backend_name = str(getattr(backend, "name", type(backend).__name__.lower()))
        self._emitter = Emitter(normalise_observers(observer))
        attach = getattr(backend, "attach_emitter", None)
        if attach is not None:
            attach(self._emitter)
        self._fallback = self._make_fallback(fallback, backend)
        self._fb_lock = threading.Lock()
        self._fb_active = False
        self._fb_count = 0
        self._config = PolicyConfig(
            namespace=validate_namespace(namespace),
            identity=IdentityConfig(
                trusted_proxies=trusted_proxies,
                ipv6_prefix=ipv6_prefix,
                ipv4_prefix=ipv4_prefix,
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

    @staticmethod
    def _make_fallback(
        fallback: Literal["memory"] | MemoryBackend | None, backend: Backend
    ) -> MemoryBackend | None:
        if fallback is None:
            return None
        if isinstance(backend, MemoryBackend):
            raise ConfigurationError("fallback only applies to networked backends, not memory")
        if fallback == "memory":
            return MemoryBackend()
        if isinstance(fallback, MemoryBackend):
            return fallback
        raise ConfigurationError("fallback must be None, 'memory' or a MemoryBackend")

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
        if scheme in {"redis", "rediss", "unix", "sentinel", "redis+sentinel"}:
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

    def needs_user(self, rules: Iterable[Rule] = (), *, include_global: bool = False) -> bool:
        """Whether evaluating ``rules`` (and optionally the global rules) may read
        ``RequestContext.user_id``.

        True when a rule keys on ``"user"``, or has a callable key, ``when`` or
        ``cost`` (any of which may read ``ctx.user_id``), or an access list names users.
        Integrations use it to skip an authentication lookup (a session or database
        query) when nothing needs the user.
        """
        selected = [*rules, *(self._global_rules if include_global else ())]
        for rule in selected:
            if rule.when is not None or callable(rule.cost):
                return True
            if any(src == "user" or callable(src) for src in rule.key):
                return True
        lists = (self._config.allow, self._config.deny)
        return any(access is not None and bool(access.users) for access in lists)

    def get_rule(self, name: str) -> Rule:
        """The registered rule called ``name`` (raises :class:`UnknownRuleError`)."""
        return self._lookup(name)

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
        return self._key_for_rule(r, identity)

    def _key_for_rule(self, r: Rule, identity: str | Identity) -> str:
        ident = identity if isinstance(identity, Identity) else Identity.of(identity)
        digest = identity_digest(ident.kind, ident.value, self._config.identity_secret)
        return rate_limit_key(self._config.namespace, r.name, r.algorithm, digest)

    def _single_op(
        self, rule: Rule | str, identity: str | Identity, cost: int
    ) -> tuple[Rule, ConsumeOp]:
        r = self._lookup(rule)
        return r, ConsumeOp(self._key_for_rule(r, identity), r.spec, cost, enforce=True)

    # -- observability, failure handling and fallback ----------------------------------

    @property
    def fallback_active(self) -> bool:
        """True while the process-local fallback is serving decisions."""
        return self._fb_active

    def add_observer(self, observer: Observer) -> None:
        """Start delivering events to ``observer`` (see :mod:`unilimit.observability`)."""
        self._emitter.add(observer)

    def remove_observer(self, observer: Observer) -> None:
        self._emitter.remove(observer)

    def _clock(self) -> float | None:
        return perf_counter() if self._emitter.enabled else None

    def _decision_event(
        self, decision: Decision, source: str, duration: float | None
    ) -> DecisionEvent:
        binding = decision.binding.rule if decision.binding is not None else decision.rule
        return DecisionEvent(
            timestamp=now(),
            allowed=decision.allowed,
            reason=decision.reason.value,
            source=source,  # type: ignore[arg-type]
            backend=self._backend_name,
            rules=tuple(
                RuleOutcome(r.rule.name, r.rule.algorithm.value, r.allowed, r.enforced)
                for r in decision.results
            ),
            binding_rule=None if binding is None else binding.name,
            fail_mode=(
                ("open" if decision.allowed else "closed") if source == "degraded" else None
            ),
            partial_commit=decision.partial_commit,
            duration=duration,
        )

    def _emit_decision(self, decision: Decision, source: str, started: float | None) -> None:
        if self._emitter.enabled:
            duration = None if started is None else perf_counter() - started
            self._emitter.emit(lambda: self._decision_event(decision, source, duration))

    def _emit_result(self, result: RateLimitResult, source: str, started: float | None) -> None:
        if not self._emitter.enabled:
            return
        duration = None if started is None else perf_counter() - started

        def make() -> Event:
            return DecisionEvent(
                timestamp=now(),
                allowed=result.allowed,
                reason="allowed" if result.allowed else "rate_limited",
                source=source,  # type: ignore[arg-type]
                backend=self._backend_name,
                rules=(
                    RuleOutcome(
                        result.rule.name,
                        result.rule.algorithm.value,
                        result.allowed,
                        result.enforced,
                    ),
                ),
                binding_rule=result.rule.name,
                fail_mode=(
                    ("open" if result.allowed else "closed") if source == "degraded" else None
                ),
                partial_commit=False,
                duration=duration,
            )

        self._emitter.emit(make)

    def _on_unavailable(
        self, exc: BackendUnavailable, started: float | None, operation: str
    ) -> None:
        if self._emitter.enabled:
            duration = 0.0 if started is None else perf_counter() - started
            cause = exc.__cause__ or exc
            self._emitter.emit(
                lambda: BackendUnavailableEvent(
                    timestamp=now(),
                    backend=self._backend_name,
                    operation=operation,  # type: ignore[arg-type]
                    error=type(cause).__name__,
                    partial_commit=exc.partial_commit,
                    duration=duration,
                )
            )

    def _log_unavailable(self, outcome: str, exc: BackendUnavailable) -> None:
        # One category per outcome: a fail-closed outage is more serious than fail-open.
        throttled.log(
            logging.WARNING,
            f"backend_unavailable:{outcome}",
            "rate limit backend unavailable; %s: %s",
            {"open": "failing open", "closed": "failing closed", "fallback": "using fallback"}[
                outcome
            ],
            exc,
        )

    def _on_success(self, started: float | None) -> None:
        """A backend call succeeded: the backend is serving again."""
        if self._fb_active:
            self._deactivate_fallback()

    def _can_fallback(self, rules: Iterable[Rule]) -> bool:
        """The fallback may only tighten fail-open. A fail-closed rule stays closed: the
        fallback would admit traffic that fail-closed rejects."""
        return self._fallback is not None and not any(
            r.enforced and is_fail_closed(r, self._config.fail_mode) for r in rules
        )

    def _fallback_served(self) -> None:
        with self._fb_lock:
            self._fb_count += 1
            first = not self._fb_active
            self._fb_active = True
        if first:
            throttled.log(
                logging.WARNING,
                "fallback_activated",
                "rate limit backend unavailable: enforcing limits with the process-local "
                "fallback (counters are per process, not shared)",
            )
            self._emitter.emit(lambda: FallbackEvent(now(), self._backend_name, "activated", 0))

    def _deactivate_fallback(self) -> None:
        with self._fb_lock:
            if not self._fb_active:
                return
            served, self._fb_active, self._fb_count = self._fb_count, False, 0
        throttled.log(
            logging.WARNING,
            "fallback_deactivated",
            "rate limit backend is available again; the process-local fallback served %d "
            "decisions and is no longer used",
            served,
        )
        self._emitter.emit(lambda: FallbackEvent(now(), self._backend_name, "deactivated", served))
        throttled.flush()

    def _peek_degraded(
        self, rule: Rule, identity: str | Identity, exc: BackendUnavailable
    ) -> RateLimitResult:
        """``peek`` is advisory: it follows the fail mode and never uses the fallback."""
        closed = is_fail_closed(rule, self._config.fail_mode)
        self._log_unavailable("closed" if closed else "open", exc)
        return degraded_result(rule, not closed, self.key_for(rule, identity))

    def _failed_result(
        self,
        rule: Rule,
        identity: str | Identity,
        exc: BackendUnavailable,
        started: float | None,
    ) -> RateLimitResult:
        closed = is_fail_closed(rule, self._config.fail_mode)
        self._log_unavailable("closed" if closed else "open", exc)
        result = degraded_result(rule, not closed, self.key_for(rule, identity))
        self._emit_result(result, "degraded", started)
        return result

    def _fallback_result(self, rule: Rule, op: ConsumeOp, batch: BatchResult) -> RateLimitResult:
        self._fallback_served()
        return replace(to_result(rule, batch.results[0], op.key), fallback=True)

    def _failed_decision(
        self, planned: Plan, exc: BackendUnavailable, started: float | None
    ) -> Decision:
        decision = decide_unavailable(
            planned, self._config.fail_mode, partial_commit=exc.partial_commit
        )
        self._log_unavailable("open" if decision.allowed else "closed", exc)
        self._emit_decision(decision, "degraded", started)
        return decision

    def _fallback_decision(
        self, planned: Plan, batch: BatchResult, exc: BackendUnavailable, started: float | None
    ) -> Decision:
        self._log_unavailable("fallback", exc)
        self._fallback_served()
        base = decide(planned, batch)
        binding = base.binding
        decision = replace(
            base,
            results=tuple(replace(r, fallback=True) for r in base.results),
            binding=None if binding is None else replace(binding, fallback=True),
            fallback=True,
            partial_commit=exc.partial_commit,
        )
        self._emit_decision(decision, "fallback", started)
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
        started = self._clock()
        try:
            batch = self.backend.consume([op])
        except BackendUnavailable as exc:
            self._on_unavailable(exc, started, "consume")
            if self._fallback is not None and self._can_fallback((r,)):
                self._log_unavailable("fallback", exc)
                result = self._fallback_result(r, op, self._fallback.consume([op]))
                self._emit_result(result, "fallback", started)
                return result
            return self._failed_result(r, identity, exc, started)
        self._on_success(started)
        result = to_result(r, batch.results[0], op.key)
        self._emit_result(result, "backend", started)
        return result

    async def aconsume(
        self, rule: Rule | str, identity: str | Identity, *, cost: int = 1
    ) -> RateLimitResult:
        r, op = self._single_op(rule, identity, cost)
        started = self._clock()
        try:
            batch = await self.backend.aconsume([op])
        except BackendUnavailable as exc:
            self._on_unavailable(exc, started, "consume")
            if self._fallback is not None and self._can_fallback((r,)):
                self._log_unavailable("fallback", exc)
                result = self._fallback_result(r, op, await self._fallback.aconsume([op]))
                self._emit_result(result, "fallback", started)
                return result
            return self._failed_result(r, identity, exc, started)
        self._on_success(started)
        result = to_result(r, batch.results[0], op.key)
        self._emit_result(result, "backend", started)
        return result

    def peek(self, rule: Rule | str, identity: str | Identity, *, cost: int = 1) -> RateLimitResult:
        """Evaluate without consuming. Never mutates state; never use as an admission check."""
        r, op = self._single_op(rule, identity, cost)
        started = self._clock()
        try:
            results: tuple[OpResult, ...] = self.backend.peek([op])
        except BackendUnavailable as exc:
            self._on_unavailable(exc, started, "peek")
            return self._peek_degraded(r, identity, exc)
        return to_result(r, results[0], op.key)

    async def apeek(
        self, rule: Rule | str, identity: str | Identity, *, cost: int = 1
    ) -> RateLimitResult:
        r, op = self._single_op(rule, identity, cost)
        started = self._clock()
        try:
            results = await self.backend.apeek([op])
        except BackendUnavailable as exc:
            self._on_unavailable(exc, started, "peek")
            return self._peek_degraded(r, identity, exc)
        return to_result(r, results[0], op.key)

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
        skip_keys: Collection[str] = (),
        cost: int | None = None,
    ) -> Decision:
        """Evaluate a request against the global rules and ``rules``.

        All applicable rules are consumed atomically: quota is consumed only if every
        enforcing rule allows the request. Counters whose storage key is in
        ``skip_keys`` (already consumed earlier in the same request) are left out.
        """
        planned = plan(
            ctx, self._rules_for(rules, include_global), self._config, skip_keys, cost=cost
        )
        if isinstance(planned, Decision):
            self._emit_decision(planned, "policy", None)
            return planned
        if not planned.entries:
            decision = decide(planned, BatchResult(True, ()))
            self._emit_decision(decision, "policy", None)
            return decision
        started = self._clock()
        try:
            batch = self.backend.consume(planned.ops)
        except BackendUnavailable as exc:
            self._on_unavailable(exc, started, "consume")
            if self._fallback is not None and self._can_fallback(e.rule for e in planned.entries):
                return self._fallback_decision(
                    planned, self._fallback.consume(planned.ops), exc, started
                )
            return self._failed_decision(planned, exc, started)
        self._on_success(started)
        decision = decide(planned, batch)
        self._emit_decision(decision, "backend", started)
        return decision

    async def aevaluate(
        self,
        ctx: RequestContext,
        rules: Sequence[Rule | str] = (),
        *,
        include_global: bool = True,
        skip_keys: Collection[str] = (),
        cost: int | None = None,
    ) -> Decision:
        planned = plan(
            ctx, self._rules_for(rules, include_global), self._config, skip_keys, cost=cost
        )
        if isinstance(planned, Decision):
            self._emit_decision(planned, "policy", None)
            return planned
        if not planned.entries:
            decision = decide(planned, BatchResult(True, ()))
            self._emit_decision(decision, "policy", None)
            return decision
        started = self._clock()
        try:
            batch = await self.backend.aconsume(planned.ops)
        except BackendUnavailable as exc:
            self._on_unavailable(exc, started, "consume")
            if self._fallback is not None and self._can_fallback(e.rule for e in planned.entries):
                return self._fallback_decision(
                    planned, await self._fallback.aconsume(planned.ops), exc, started
                )
            return self._failed_decision(planned, exc, started)
        self._on_success(started)
        decision = decide(planned, batch)
        self._emit_decision(decision, "backend", started)
        return decision

    # -- concurrency lease API ---------------------------------------------------------

    def resolve_identity(
        self, ctx: RequestContext, key: str | Callable[..., Any] | Sequence[Any] = "ip"
    ) -> Identity | None:
        """Resolve an Identity from a RequestContext according to this Limiter's configuration."""
        from unilimit.core.identity import IdentityResolver

        resolver = IdentityResolver(ctx, self._config.identity)
        sources = [key] if (isinstance(key, str) or callable(key)) else list(key)
        return resolver.resolve(sources, "concurrency")

    def lease_key_for(self, limit: ConcurrencyLimit | str, identity: str | Identity) -> str:
        """The exact storage key that will be used for a concurrency lease."""
        name = limit.name if isinstance(limit, ConcurrencyLimit) else limit
        ident = identity if isinstance(identity, Identity) else Identity("custom", identity)
        digest = identity_digest(ident.kind, ident.value, self._config.identity_secret)
        return concurrency_key(self.namespace, name, digest)

    def acquire_lease(
        self,
        limit: ConcurrencyLimit,
        identity: str | Identity,
        lease_id: str | None = None,
    ) -> LeaseDecision:
        """Attempt to acquire a concurrent connection lease for an identity."""
        if not isinstance(self.backend, SyncLeaseBackend):
            raise ConfigurationError(
                f"Backend {getattr(self.backend, 'name', type(self.backend).__name__)!r} "
                "does not support concurrency leases"
            )
        ident = identity if isinstance(identity, Identity) else Identity("custom", identity)
        key = self.lease_key_for(limit, ident)
        lid = lease_id if lease_id is not None else generate_lease_id()
        ttl_us = int(limit.lease_ttl * 1_000_000)
        started = self._clock()
        try:
            res = self.backend.acquire_lease(key, lid, limit.max_concurrent, ttl_us, 0)
            self._on_success(started)
        except BackendUnavailable as exc:
            self._on_unavailable(exc, started, "acquire_lease")
            if self.fail_mode == "open":
                self._log_unavailable("fail_open", exc)
                return LeaseDecision(
                    allowed=True,
                    current=1,
                    max_concurrent=limit.max_concurrent,
                    lease_id=lid,
                    key=key,
                    identity=ident,
                    limit_name=limit.name,
                )
            raise
        retry = (res.retry_after_us / 1_000_000.0) if res.retry_after_us is not None else None
        return LeaseDecision(
            allowed=res.allowed,
            current=res.current,
            max_concurrent=res.max_concurrent,
            lease_id=lid,
            key=key,
            identity=ident,
            limit_name=limit.name,
            retry_after=retry,
        )

    async def aacquire_lease(
        self,
        limit: ConcurrencyLimit,
        identity: str | Identity,
        lease_id: str | None = None,
    ) -> LeaseDecision:
        """Asynchronously attempt to acquire a concurrent connection lease for an identity."""
        if not isinstance(self.backend, AsyncLeaseBackend):
            raise ConfigurationError(
                f"Backend {getattr(self.backend, 'name', type(self.backend).__name__)!r} "
                "does not support concurrency leases"
            )
        ident = identity if isinstance(identity, Identity) else Identity("custom", identity)
        key = self.lease_key_for(limit, ident)
        lid = lease_id if lease_id is not None else generate_lease_id()
        ttl_us = int(limit.lease_ttl * 1_000_000)
        started = self._clock()
        try:
            res = await self.backend.aacquire_lease(key, lid, limit.max_concurrent, ttl_us, 0)
            self._on_success(started)
        except BackendUnavailable as exc:
            self._on_unavailable(exc, started, "acquire_lease")
            if self.fail_mode == "open":
                self._log_unavailable("fail_open", exc)
                return LeaseDecision(
                    allowed=True,
                    current=1,
                    max_concurrent=limit.max_concurrent,
                    lease_id=lid,
                    key=key,
                    identity=ident,
                    limit_name=limit.name,
                )
            raise
        retry = (res.retry_after_us / 1_000_000.0) if res.retry_after_us is not None else None
        return LeaseDecision(
            allowed=res.allowed,
            current=res.current,
            max_concurrent=res.max_concurrent,
            lease_id=lid,
            key=key,
            identity=ident,
            limit_name=limit.name,
            retry_after=retry,
        )

    def renew_lease(self, key: str, lease_id: str, ttl_seconds: float) -> bool:
        """Extend the TTL of an active lease."""
        if not isinstance(self.backend, SyncLeaseBackend):
            return False
        ttl_us = int(ttl_seconds * 1_000_000)
        try:
            return self.backend.renew_lease(key, lease_id, ttl_us, 0)
        except BackendUnavailable:
            return False

    async def arenew_lease(self, key: str, lease_id: str, ttl_seconds: float) -> bool:
        """Asynchronously extend the TTL of an active lease."""
        if not isinstance(self.backend, AsyncLeaseBackend):
            return False
        ttl_us = int(ttl_seconds * 1_000_000)
        try:
            return await self.backend.arenew_lease(key, lease_id, ttl_us, 0)
        except BackendUnavailable:
            return False

    def release_lease(self, key: str, lease_id: str) -> None:
        """Release an active lease."""
        if not isinstance(self.backend, SyncLeaseBackend):
            return
        with contextlib.suppress(BackendUnavailable):
            self.backend.release_lease(key, lease_id)

    async def arelease_lease(self, key: str, lease_id: str) -> None:
        """Asynchronously release an active lease."""
        if not isinstance(self.backend, AsyncLeaseBackend):
            return
        with contextlib.suppress(BackendUnavailable):
            await self.backend.arelease_lease(key, lease_id)

    # -- lifecycle ---------------------------------------------------------------------

    def close(self) -> None:
        self.backend.close()
        throttled.flush()

    async def aclose(self) -> None:
        await self.backend.aclose()
