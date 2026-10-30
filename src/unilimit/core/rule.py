from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Literal, get_args

from unilimit.algorithms import Algorithm, LimitSpec
from unilimit.core.context import RequestContext
from unilimit.core.errors import ConfigurationError
from unilimit.core.identity import KeySource, KeySpec, normalise_key_spec
from unilimit.core.rate import Duration, Rate

OnMissing = Literal["deny", "raise", "skip"]
FailMode = Literal["open", "closed"]
Action = Literal["reject", "log"]
CostFunc = Callable[[RequestContext], int]
Predicate = Callable[[RequestContext], bool]
AlgorithmName = Literal["fixed_window", "sliding_window", "token_bucket"]

MAX_RULE_NAME_LENGTH = 200


def _check_choice(value: object, allowed: tuple[str, ...], what: str) -> None:
    if value not in allowed:
        raise ConfigurationError(f"{what} must be one of {allowed}, got {value!r}")


@dataclass(frozen=True, slots=True, init=False)
class Rule:
    """A named, immutable rate-limiting policy.

    The name together with the algorithm identifies the counter: changing ``limit`` or
    ``window`` keeps existing counters, while changing ``algorithm`` starts fresh state.

    Specify the rate either as ``rate="100/minute"`` or as ``limit=100, window="1m"``.
    """

    name: str
    rate: Rate
    algorithm: Algorithm
    key: tuple[KeySource, ...]
    burst: int | None
    methods: frozenset[str] | None
    paths: tuple[str, ...] | None
    when: Predicate | None
    cost: int | CostFunc
    on_missing: OnMissing | None
    fail_mode: FailMode | None
    action: Action
    spec: LimitSpec = field(repr=False, compare=False)
    """The validated limit passed to backends (derived from rate, algorithm, burst)."""

    def __init__(
        self,
        name: str,
        rate: str | Rate | None = None,
        *,
        limit: int | None = None,
        window: Duration | None = None,
        algorithm: AlgorithmName | Algorithm = "sliding_window",
        key: KeySpec = "ip",
        burst: int | None = None,
        methods: Iterable[str] | None = None,
        paths: Iterable[str] | None = None,
        when: Predicate | None = None,
        cost: int | CostFunc = 1,
        on_missing: OnMissing | None = None,
        fail_mode: FailMode | None = None,
        action: Action = "reject",
    ) -> None:
        if (
            not isinstance(name, str)
            or not name.strip()
            or len(name) > MAX_RULE_NAME_LENGTH
            or not name.isprintable()
        ):
            raise ConfigurationError(
                f"rule name must be a non-blank printable string of at most "
                f"{MAX_RULE_NAME_LENGTH} characters, got {name!r}"
            )

        if rate is not None and (limit is not None or window is not None):
            raise ConfigurationError(f"rule {name!r}: pass either rate= or limit=/window=")
        if isinstance(rate, Rate):
            parsed = rate
        elif isinstance(rate, str):
            parsed = Rate.parse(rate)
        elif rate is None:
            if limit is None or window is None:
                raise ConfigurationError(f"rule {name!r}: a rate or both limit= and window=")
            parsed = Rate.of(limit, window)
        else:
            raise ConfigurationError(f"rule {name!r}: invalid rate {rate!r}")

        try:
            algo = Algorithm(algorithm)
        except ValueError:
            raise ConfigurationError(
                f"rule {name!r}: unknown algorithm {algorithm!r}; expected one of "
                f"{[a.value for a in Algorithm]}"
            ) from None

        if burst is not None:
            if algo is not Algorithm.TOKEN_BUCKET:
                raise ConfigurationError(f"rule {name!r}: burst= applies to token_bucket only")
            if isinstance(burst, bool) or not isinstance(burst, int) or burst < 1:
                raise ConfigurationError(f"rule {name!r}: burst must be a positive integer")
        capacity = burst if burst is not None else parsed.limit

        try:
            spec = LimitSpec(algo, parsed.limit, parsed.window_us, capacity)
        except ValueError as exc:
            raise ConfigurationError(f"rule {name!r}: {exc}") from None

        if isinstance(cost, bool) or not (isinstance(cost, int) or callable(cost)):
            raise ConfigurationError(f"rule {name!r}: cost must be an int or a callable")
        if isinstance(cost, int) and not 1 <= cost <= capacity:
            raise ConfigurationError(
                f"rule {name!r}: cost {cost} must be between 1 and the capacity ({capacity})"
            )

        if on_missing is not None:
            _check_choice(on_missing, get_args(OnMissing), "on_missing")
        if fail_mode is not None:
            _check_choice(fail_mode, get_args(FailMode), "fail_mode")
        _check_choice(action, get_args(Action), "action")
        if when is not None and not callable(when):
            raise ConfigurationError(f"rule {name!r}: when must be callable")
        if isinstance(methods, str) or isinstance(paths, str):
            raise ConfigurationError(f"rule {name!r}: methods and paths must be collections")

        object.__setattr__(self, "name", name)
        object.__setattr__(self, "rate", parsed)
        object.__setattr__(self, "algorithm", algo)
        object.__setattr__(self, "key", normalise_key_spec(key))
        object.__setattr__(self, "burst", burst)
        object.__setattr__(
            self, "methods", None if methods is None else frozenset(m.upper() for m in methods)
        )
        object.__setattr__(self, "paths", None if paths is None else tuple(paths))
        object.__setattr__(self, "when", when)
        object.__setattr__(self, "cost", cost)
        object.__setattr__(self, "on_missing", on_missing)
        object.__setattr__(self, "fail_mode", fail_mode)
        object.__setattr__(self, "action", action)
        object.__setattr__(self, "spec", spec)

    @property
    def limit(self) -> int:
        return self.rate.limit

    @property
    def window_us(self) -> int:
        return self.rate.window_us

    @property
    def capacity(self) -> int:
        """Maximum quota available at once (``burst`` for token bucket, else ``limit``)."""
        return self.burst if self.burst is not None else self.rate.limit

    @property
    def enforced(self) -> bool:
        return self.action == "reject"
