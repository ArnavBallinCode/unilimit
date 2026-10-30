"""Framework-free building blocks shared by every integration.

Nothing here imports a web framework. Integrations turn their requests into
:class:`~unilimit.RequestContext` objects themselves, then use these helpers to build
rules, resolve them against a :class:`~unilimit.Limiter` and keep per-request state.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from unilimit.core.errors import ConfigurationError
from unilimit.core.identity import KeySpec
from unilimit.core.limiter import Limiter
from unilimit.core.rate import Duration, Rate
from unilimit.core.result import Decision, RateLimitResult
from unilimit.core.rule import Action, AlgorithmName, CostFunc, FailMode, OnMissing, Predicate, Rule
from unilimit.http import ResponseConfig


def build_rules(
    rules: Sequence[Rule | str],
    *,
    rate: str | Rate | None,
    limit: int | None,
    window: Duration | None,
    key: KeySpec | None,
    default_key: KeySpec,
    algorithm: AlgorithmName,
    burst: int | None,
    cost: int | CostFunc,
    methods: Iterable[str] | None,
    when: Predicate | None,
    on_missing: OnMissing | None,
    fail_mode: FailMode | None,
    action: Action,
    owner: str,
) -> tuple[Rule | str, ...]:
    """Validate the arguments of a decorator or dependency into rules or rule names.

    Inline (``rate=`` or ``limit=``/``window=``): exactly one name, which becomes one
    rule, or one rule per rate for ``"10/second;1000/day"``. Otherwise: ``Rule``
    objects and names of rules registered on the limiter. ``owner`` names the caller in
    error messages.
    """
    if not rules:
        raise ConfigurationError(f"{owner} needs at least one rule or rule name")
    if rate is not None or limit is not None or window is not None:
        if len(rules) != 1 or not isinstance(rules[0], str):
            raise ConfigurationError(
                f"an inline {owner} takes exactly one name: {owner}('name', rate='10/s')"
            )
        options: dict[str, Any] = {
            "algorithm": algorithm,
            "key": key if key is not None else default_key,
            "burst": burst,
            "cost": cost,
            "methods": methods,
            "when": when,
            "on_missing": on_missing,
            "fail_mode": fail_mode,
            "action": action,
        }
        if isinstance(rate, str) and limit is None and window is None:
            return Rule.many(rules[0], rate, **options)
        return (Rule(rules[0], rate, limit=limit, window=window, **options),)
    customised = (key, burst, methods, when, on_missing, fail_mode)
    if any(v is not None for v in customised) or cost != 1 or action != "reject":
        raise ConfigurationError(
            "rule options need an inline rule (rate= or limit=/window=); "
            "set them on the Rule objects instead"
        )
    for r in rules:
        if not isinstance(r, Rule | str):
            raise ConfigurationError(f"expected Rule objects or rule names, got {r!r}")
    return tuple(rules)


def resolve_rules(limiter: Limiter, rules: Sequence[Rule | str]) -> tuple[Rule, ...]:
    """Rule objects for ``rules``; names are looked up on ``limiter``."""
    return tuple(r if isinstance(r, Rule) else limiter.get_rule(r) for r in rules)


def scoped_rule(rule: Rule, scope: str) -> Rule:
    """A copy of ``rule`` with its own counter for ``scope`` (for example a route)."""
    name = f"{rule.name}|{scope}"
    if len(name) > 200:
        name = f"{rule.name[:150]}|{hashlib.sha256(scope.encode()).hexdigest()[:16]}"
    return Rule(
        name,
        rule.rate,
        algorithm=rule.algorithm,
        key=rule.key,
        burst=rule.burst,
        methods=rule.methods,
        paths=rule.paths,
        when=rule.when,
        cost=rule.cost,
        on_missing=rule.on_missing,
        fail_mode=rule.fail_mode,
        action=rule.action,
    )


@dataclass
class RequestState:
    """What UniLimit knows about the current request, shared by its integration parts.

    FastAPI stores it in ``scope["state"]["unilimit"]`` (``request.state.unilimit``);
    Django stores it as ``request.unilimit``.
    """

    limiter: Limiter | None = None
    response: ResponseConfig | None = None
    middleware: bool = False
    results: list[RateLimitResult] = field(default_factory=list)
    """Enforced results of every admitting decision so far, for response headers."""
    consumed: set[str] = field(default_factory=set)
    """Storage keys already consumed in this request; never consumed twice."""
    decisions: list[Decision] = field(default_factory=list)
    rejection: Decision | None = None
    """The decision that rejected the request, if any."""

    def record(self, decision: Decision) -> None:
        self.decisions.append(decision)
        if not decision.allowed:
            self.rejection = decision
            return
        for result in decision.results:
            if result.storage_key is not None and not result.degraded:
                self.consumed.add(result.storage_key)
            if result.enforced:
                self.results.append(result)


__all__ = ["RequestState", "build_rules", "resolve_rules", "scoped_rule"]
