"""Limiter pipeline tests. Every scenario runs through both the sync and async APIs."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence

import pytest

from unilimit import (
    AccessList,
    BackendError,
    BackendUnavailable,
    ConfigurationError,
    Decision,
    Identity,
    IdentityError,
    Limiter,
    MemoryBackend,
    RateLimitResult,
    Reason,
    RequestContext,
    Rule,
    TrustedProxies,
    UnknownRuleError,
)
from unilimit.backends.base import BatchResult, ConsumeOp, OpResult
from unilimit.testing import ManualClock


class API:
    """Calls the sync or async half of a Limiter with one awaitable interface."""

    def __init__(self, limiter: Limiter, mode: str) -> None:
        self.limiter = limiter
        self.mode = mode

    async def consume(
        self, rule: Rule | str, ident: str | Identity, cost: int = 1
    ) -> RateLimitResult:
        if self.mode == "sync":
            return self.limiter.consume(rule, ident, cost=cost)
        return await self.limiter.aconsume(rule, ident, cost=cost)

    async def peek(self, rule: Rule | str, ident: str | Identity) -> RateLimitResult:
        if self.mode == "sync":
            return self.limiter.peek(rule, ident)
        return await self.limiter.apeek(rule, ident)

    async def reset(self, rule: Rule | str, ident: str | Identity) -> None:
        if self.mode == "sync":
            self.limiter.reset(rule, ident)
        else:
            await self.limiter.areset(rule, ident)

    async def evaluate(
        self, ctx: RequestContext, rules: Sequence[Rule | str] = (), include_global: bool = True
    ) -> Decision:
        if self.mode == "sync":
            return self.limiter.evaluate(ctx, rules, include_global=include_global)
        return await self.limiter.aevaluate(ctx, rules, include_global=include_global)

    async def close(self) -> None:
        if self.mode == "sync":
            self.limiter.close()
        else:
            await self.limiter.aclose()


@pytest.fixture(params=["sync", "async"])
def mode(request: pytest.FixtureRequest) -> str:
    return str(request.param)


Factory = Callable[..., API]


@pytest.fixture
def make(mode: str, clock: ManualClock) -> Factory:
    def factory(backend: MemoryBackend | None = None, **kwargs: object) -> API:
        b = backend if backend is not None else MemoryBackend(clock=clock)
        return API(Limiter(b, **kwargs), mode)  # type: ignore[arg-type]

    return factory


def req(**kwargs: object) -> RequestContext:
    kwargs.setdefault("peer", "203.0.113.9")
    return RequestContext(**kwargs)  # type: ignore[arg-type]


# -- direct API ------------------------------------------------------------------------


async def test_consume_peek_reset(make: Factory, clock: ManualClock) -> None:
    api = make(rules=[Rule("api", "2/minute")])
    first = await api.consume("api", "u1")
    assert (first.allowed, first.remaining, first.limit, first.retry_after) == (True, 1, 2, 0.0)
    assert (await api.consume("api", "u1")).allowed
    rejected = await api.consume("api", "u1")
    assert not rejected.allowed
    assert rejected.retry_after is not None
    assert rejected.retry_after > 0
    peeked = await api.peek("api", "u1")
    assert (peeked.allowed, peeked.remaining) == (False, 0)
    assert (await api.consume("api", "u2")).allowed  # other identity unaffected
    await api.reset("api", "u1")
    assert (await api.consume("api", "u1")).allowed


async def test_peek_does_not_consume(make: Factory) -> None:
    api = make()
    rule = Rule("r", "1/minute")
    for _ in range(5):
        assert (await api.peek(rule, "x")).allowed
    assert (await api.consume(rule, "x")).allowed


async def test_cost(make: Factory) -> None:
    api = make()
    rule = Rule("r", "5/minute", algorithm="fixed_window")
    assert (await api.consume(rule, "x", cost=4)).remaining == 1
    rejected = await api.consume(rule, "x", cost=2)
    assert not rejected.allowed
    impossible = await api.consume(rule, "y", cost=6)
    assert not impossible.allowed
    assert impossible.retry_after is None
    with pytest.raises(ValueError, match="cost"):
        await api.consume(rule, "x", cost=0)


async def test_identity_objects_share_counters_with_requests(make: Factory) -> None:
    api = make()
    rule = Rule("r", "2/minute", key="user")
    assert (await api.evaluate(req(user_id=42), [rule])).allowed
    assert (await api.consume(rule, Identity("user", "42"))).remaining == 0
    assert (await api.consume(rule, "42")).remaining == 1  # plain str is kind "custom"


async def test_unknown_rule_name(make: Factory) -> None:
    api = make()
    with pytest.raises(UnknownRuleError) as info:
        await api.consume("nope", "x")
    assert isinstance(info.value, LookupError)
    assert isinstance(info.value, ConfigurationError)


async def test_invalid_identity(make: Factory) -> None:
    api = make()
    with pytest.raises(ValueError, match="identity value"):
        await api.consume(Rule("r", "1/s"), "")


# -- request pipeline ------------------------------------------------------------------


async def test_multi_rule_rejection_consumes_nothing(make: Factory, clock: ManualClock) -> None:
    api = make()
    burst = Rule("burst", "2/second", key="user", algorithm="fixed_window")
    daily = Rule("daily", "10/day", key="user", algorithm="fixed_window")
    ctx = req(user_id="u")
    for _ in range(2):
        assert (await api.evaluate(ctx, [burst, daily])).allowed
    for _ in range(20):  # hammering the burst limit must not burn the daily quota
        decision = await api.evaluate(ctx, [burst, daily])
        assert not decision.allowed
        assert decision.reason is Reason.RATE_LIMITED
    assert (await api.peek(daily, Identity("user", "u"))).remaining == 8
    clock.advance(1)
    decision = await api.evaluate(ctx, [burst, daily])
    assert decision.allowed
    assert (await api.peek(daily, Identity("user", "u"))).remaining == 7


async def test_binding_result_selection(make: Factory) -> None:
    api = make()
    loose = Rule("loose", "100/minute", key="user", algorithm="fixed_window")
    tight = Rule("tight", "3/minute", key="user", algorithm="fixed_window")
    decision = await api.evaluate(req(user_id="u"), [loose, tight])
    assert decision.binding is not None
    assert decision.binding.rule is tight  # smallest remaining
    assert decision.retry_after is None

    short = Rule("short", "1/second", key="user", algorithm="fixed_window")
    long = Rule("long", "1/hour", key="user", algorithm="fixed_window")
    await api.evaluate(req(user_id="v"), [short, long])
    decision = await api.evaluate(req(user_id="v"), [short, long])
    assert not decision.allowed
    assert decision.binding is not None
    assert decision.binding.rule is long  # largest retry_after
    assert decision.retry_after == 3600.0


async def test_binding_prefers_never_over_any_retry(make: Factory) -> None:
    api = make()
    small = Rule("small", "2/hour", key="user", cost=lambda ctx: 3, algorithm="fixed_window")
    other = Rule("other", "1/second", key="user", algorithm="fixed_window")
    await api.evaluate(req(user_id="w"), [other])
    decision = await api.evaluate(req(user_id="w"), [other, small])
    assert decision.binding is not None
    assert decision.binding.rule is small
    assert decision.retry_after is None


async def test_global_and_registry_rules(make: Factory) -> None:
    shared = Rule("shared", "2/minute")
    api = make(rules=[shared], global_rules=["shared"])
    assert api.limiter.global_rules == (shared,)
    assert api.limiter.rules["shared"] is shared
    assert (await api.evaluate(req())).allowed
    assert (await api.evaluate(req(), include_global=False)).results == ()
    # The same rule as a global and a route rule is consumed once per request.
    decision = await api.evaluate(req(), ["shared"])
    assert len(decision.results) == 1
    assert decision.results[0].remaining == 0
    assert not (await api.evaluate(req())).allowed


async def test_conflicting_rules_with_same_name(make: Factory) -> None:
    api = make()
    with pytest.raises(ConfigurationError, match="same algorithm but different settings"):
        await api.evaluate(req(), [Rule("x", "1/s"), Rule("x", "2/s")])
    # Same name, different algorithm: separate counters, no conflict.
    decision = await api.evaluate(
        req(), [Rule("x", "1/s"), Rule("x", "1/s", algorithm="token_bucket")]
    )
    assert len(decision.results) == 2


async def test_rule_selection(make: Factory) -> None:
    api = make()
    rules = [
        Rule("posts", "1/minute", methods={"POST"}),
        Rule("api", "1/minute", paths=["/api/*"]),
        Rule("beta", "1/minute", when=lambda ctx: ctx.headers.get("x-beta") == "1"),
    ]
    names = lambda d: [r.rule.name for r in d.results]  # noqa: E731
    assert names(await api.evaluate(req(method="GET", path="/"), rules)) == []
    assert names(await api.evaluate(req(method="POST", path="/api/x"), rules)) == ["posts", "api"]
    beta = req(path="/", headers={"X-Beta": "1"})
    assert names(await api.evaluate(beta, rules)) == ["beta"]


async def test_shadow_rules_report_but_never_block(make: Factory) -> None:
    api = make()
    shadow = Rule("shadow", "1/minute", action="log")
    enforced = Rule("enforced", "100/minute")
    for i in range(3):
        decision = await api.evaluate(req(), [shadow, enforced])
        assert decision.allowed
        assert decision.binding is not None
        assert decision.binding.rule is enforced
        assert len(decision.violations) == (0 if i == 0 else 1)
    only_shadow = await api.evaluate(req(), [shadow])
    assert only_shadow.allowed
    assert only_shadow.binding is None


async def test_custom_and_fallback_keys(make: Factory) -> None:
    api = make(api_key_header="X-Key", tenant_header="X-Tenant")
    rule = Rule("r", "1/minute", key="user_or_ip")
    assert (await api.evaluate(req(), [rule])).allowed
    assert not (await api.evaluate(req(), [rule])).allowed  # anonymous: same IP bucket
    assert (await api.evaluate(req(user_id=1), [rule])).allowed  # user bucket is separate

    by_key = Rule("k", "1/minute", key=["api_key", "ip"])
    assert (await api.evaluate(req(headers={"X-Key": "a"}), [by_key])).allowed
    assert (await api.evaluate(req(headers={"X-Key": "b"}), [by_key])).allowed
    assert not (await api.evaluate(req(headers={"X-Key": "a"}), [by_key])).allowed

    tenant = Rule("t", "1/minute", key="tenant")
    assert (await api.evaluate(req(headers={"X-Tenant": "acme"}), [tenant])).allowed
    assert not (await api.evaluate(req(headers={"X-Tenant": "acme"}), [tenant])).allowed

    custom = Rule("c", "1/minute", key=lambda ctx: ctx.path.split("/")[1])
    assert (await api.evaluate(req(path="/orgA/x"), [custom])).allowed
    assert not (await api.evaluate(req(path="/orgA/y"), [custom])).allowed


async def test_trusted_proxies_feed_ip_keys(make: Factory) -> None:
    api = make(trusted_proxies=TrustedProxies(["10.0.0.0/8"]))
    rule = Rule("r", "1/minute")
    via_lb = lambda client: req(peer="10.0.0.2", headers={"X-Forwarded-For": client})  # noqa: E731
    assert (await api.evaluate(via_lb("198.51.100.1"), [rule])).allowed
    assert (await api.evaluate(via_lb("198.51.100.2"), [rule])).allowed
    assert not (await api.evaluate(via_lb("198.51.100.1"), [rule])).allowed
    # A client spoofing the header without passing through the proxy is keyed by its peer.
    spoof = req(peer="203.0.113.50", headers={"X-Forwarded-For": "198.51.100.9"})
    assert (await api.evaluate(spoof, [rule])).allowed
    assert not (await api.evaluate(spoof, [rule])).allowed


async def test_cost_callable(make: Factory) -> None:
    api = make()
    rule = Rule("r", "10/minute", algorithm="fixed_window", cost=lambda ctx: int(ctx.headers["n"]))
    decision = await api.evaluate(req(headers={"n": "7"}), [rule])
    assert decision.results[0].remaining == 3
    with pytest.raises(ValueError, match="cost callable returned"):
        await api.evaluate(req(headers={"n": "0"}), [rule])


# -- missing identity ------------------------------------------------------------------


async def test_missing_identity_denies_by_default(
    make: Factory, caplog: pytest.LogCaptureFixture
) -> None:
    api = make()
    rule = Rule("members", "10/minute", key="user")
    other = Rule("other", "10/minute")
    with caplog.at_level(logging.WARNING, logger="unilimit"):
        decision = await api.evaluate(req(), [other, rule])
    assert not decision.allowed
    assert decision.reason is Reason.MISSING_IDENTITY
    assert decision.rule is rule
    assert "'members'" in caplog.text
    # Nothing was consumed for the other rule.
    assert (await api.peek(other, Identity("ip", "203.0.113.9"))).remaining == 10


async def test_missing_identity_modes(make: Factory) -> None:
    skip_default = make(on_missing="skip")
    rule = Rule("members", "10/minute", key="user")
    decision = await skip_default.evaluate(req(), [rule])
    assert decision.allowed
    assert decision.results == ()

    api = make()
    with pytest.raises(IdentityError):
        await api.evaluate(req(), [Rule("m", "1/s", key="user", on_missing="raise")])
    assert (await api.evaluate(req(), [Rule("m", "1/s", key="user", on_missing="skip")])).allowed
    denied = await skip_default.evaluate(req(), [Rule("m", "1/s", key="user", on_missing="deny")])
    assert denied.reason is Reason.MISSING_IDENTITY
    # Shadow rules never block, whatever on_missing says.
    shadow = Rule("s", "1/s", key="user", on_missing="deny", action="log")
    assert (await api.evaluate(req(), [shadow])).allowed


async def test_peerless_requests_are_denied_for_ip_rules(make: Factory) -> None:
    api = make()
    decision = await api.evaluate(RequestContext(), [Rule("r", "1/s")])
    assert decision.reason is Reason.MISSING_IDENTITY


# -- exemptions and access lists -------------------------------------------------------


async def test_exemptions_short_circuit_without_io(make: Factory) -> None:
    api = make(
        backend=ExplodingBackend(),
        exempt_paths=["/health", "/static/*"],
        exempt=lambda ctx: ctx.headers.get("x-internal") == "yes",
        allow=AccessList(networks=["192.0.2.0/24"], users=[7], api_keys=["trusted"]),
        deny=AccessList(networks=["198.51.100.0/24"], users=["evil"]),
    )
    rule = Rule("r", "1/s")
    assert (await api.evaluate(req(path="/health"), [rule])).reason is Reason.EXEMPT
    assert (await api.evaluate(req(path="/static/a/b.css"), [rule])).reason is Reason.EXEMPT
    assert (await api.evaluate(req(headers={"x-internal": "yes"}), [rule])).reason is Reason.EXEMPT
    assert (await api.evaluate(req(peer="192.0.2.5"), [rule])).reason is Reason.EXEMPT
    assert (await api.evaluate(req(user_id=7), [rule])).reason is Reason.EXEMPT
    assert (
        await api.evaluate(req(headers={"x-api-key": "trusted"}), [rule])
    ).reason is Reason.EXEMPT

    denied = await api.evaluate(req(peer="198.51.100.5"), [rule])
    assert (denied.allowed, denied.reason) == (False, Reason.DENIED)
    assert (await api.evaluate(req(user_id="evil"), [rule])).reason is Reason.DENIED
    # The denylist wins over the allowlist.
    both = req(peer="198.51.100.5", user_id=7)
    assert (await api.evaluate(both, [rule])).reason is Reason.DENIED


def test_access_list_validation() -> None:
    with pytest.raises(ConfigurationError):
        AccessList(networks=["nope"])
    with pytest.raises(ConfigurationError):
        AccessList(networks="10.0.0.0/8")
    assert "networks=1" in repr(AccessList(networks=["10.0.0.0/8"]))


# -- backend failures ------------------------------------------------------------------


class UnavailableBackend(MemoryBackend):
    def consume(self, ops: Sequence[ConsumeOp]) -> BatchResult:
        raise BackendUnavailable("connection refused")

    def peek(self, ops: Sequence[ConsumeOp]) -> tuple[OpResult, ...]:
        raise BackendUnavailable("connection refused")


class ExplodingBackend(MemoryBackend):
    def consume(self, ops: Sequence[ConsumeOp]) -> BatchResult:
        raise RuntimeError("bug")

    def peek(self, ops: Sequence[ConsumeOp]) -> tuple[OpResult, ...]:
        raise RuntimeError("bug")


async def test_fail_open_by_default(make: Factory, caplog: pytest.LogCaptureFixture) -> None:
    api = make(backend=UnavailableBackend())
    rule = Rule("r", "1/s")
    with caplog.at_level(logging.WARNING, logger="unilimit"):
        decision = await api.evaluate(req(), [rule])
        result = await api.consume(rule, "x")
        peeked = await api.peek(rule, "x")
    assert (decision.allowed, decision.reason, decision.degraded) == (True, Reason.ALLOWED, True)
    assert decision.results[0].degraded
    assert (result.allowed, result.degraded) == (True, True)
    assert peeked.degraded
    assert "failing open" in caplog.text


async def test_fail_closed(make: Factory) -> None:
    closed = make(backend=UnavailableBackend(), fail_mode="closed")
    decision = await closed.evaluate(req(), [Rule("r", "1/s")])
    assert (decision.allowed, decision.reason) == (False, Reason.BACKEND_UNAVAILABLE)
    assert not (await closed.consume(Rule("r", "1/s"), "x")).allowed

    per_rule = make(backend=UnavailableBackend())
    login = Rule("login", "5/minute", fail_mode="closed")
    decision = await per_rule.evaluate(req(), [Rule("r", "1/s"), login])
    assert decision.reason is Reason.BACKEND_UNAVAILABLE
    assert not (await per_rule.consume(login, "x")).allowed
    # A rule can opt back into fail-open on a fail-closed limiter.
    relaxed = Rule("r", "1/s", fail_mode="open")
    assert (await closed.evaluate(req(), [relaxed])).allowed
    # A fail-closed shadow rule cannot block.
    shadow = Rule("s", "1/s", fail_mode="closed", action="log")
    assert (await per_rule.evaluate(req(), [shadow])).allowed


async def test_unexpected_backend_errors_propagate(make: Factory) -> None:
    api = make(backend=ExplodingBackend())
    with pytest.raises(RuntimeError, match="bug"):
        await api.evaluate(req(), [Rule("r", "1/s")])
    with pytest.raises(RuntimeError, match="bug"):
        await api.consume(Rule("r", "1/s"), "x")


async def test_algorithm_mismatch_is_a_backend_error(make: Factory) -> None:
    api = make()
    key_rule = Rule("r", "1/s")
    await api.consume(key_rule, "x")
    backend = api.limiter.backend
    op = ConsumeOp(
        api.limiter.key_for(key_rule, "x"), Rule("r", "1/s", algorithm="token_bucket").spec
    )
    with pytest.raises(BackendError):
        backend.consume([op])


# -- construction ----------------------------------------------------------------------


def test_limiter_validation() -> None:
    backend = MemoryBackend()
    with pytest.raises(ConfigurationError, match="protocols"):
        Limiter(object())  # type: ignore[arg-type]
    with pytest.raises(ConfigurationError, match="fail_mode"):
        Limiter(backend, fail_mode="sometimes")  # type: ignore[arg-type]
    with pytest.raises(ConfigurationError, match="on_missing"):
        Limiter(backend, on_missing="ignore")  # type: ignore[arg-type]
    with pytest.raises(ConfigurationError, match="namespace"):
        Limiter(backend, namespace="bad ns")
    with pytest.raises(ConfigurationError, match="duplicate rule name"):
        Limiter(backend, rules=[Rule("a", "1/s"), Rule("a", "2/s")])
    with pytest.raises(ConfigurationError, match="Rule objects"):
        Limiter(backend, rules=["a"])  # type: ignore[list-item]
    with pytest.raises(UnknownRuleError):
        Limiter(backend, global_rules=["missing"])
    with pytest.raises(ConfigurationError):
        Limiter(backend, global_rules=[5])  # type: ignore[list-item]
    with pytest.raises(ConfigurationError, match="exempt_paths"):
        Limiter(backend, exempt_paths="/health")
    with pytest.raises(ConfigurationError, match="ipv6_prefix"):
        Limiter(backend, ipv6_prefix=0)
    same = Rule("a", "1/s")
    assert Limiter(backend, rules=[same, same]).rules == {"a": same}


def test_from_url() -> None:
    limiter = Limiter.from_url("memory://", namespace="app", fail_mode="closed")
    assert isinstance(limiter.backend, MemoryBackend)
    assert limiter.namespace == "app"
    assert limiter.fail_mode == "closed"
    clock = ManualClock()
    with_clock = Limiter.from_url("memory://", clock=clock, max_keys=10)
    assert isinstance(with_clock.backend, MemoryBackend)
    with pytest.raises(ConfigurationError, match="do not apply to memory"):
        Limiter.from_url("memory://", cluster=True)
    with pytest.raises(ConfigurationError, match="unsupported backend URL scheme"):
        Limiter.from_url("memcached://localhost:11211")


def test_identity_secret_changes_keys() -> None:
    plain = Limiter(MemoryBackend())
    keyed = Limiter(MemoryBackend(), identity_secret="s3cret")
    rule = Rule("r", "1/s")
    assert plain.key_for(rule, "x") != keyed.key_for(rule, "x")
    assert keyed.key_for(rule, "x").startswith("unilimit:v1:rl:r:sw:{")


async def test_close(make: Factory) -> None:
    await make().close()


async def test_sync_and_async_paths_agree(clock: ManualClock) -> None:
    """Identical scenarios through both paths yield identical decisions."""
    rules = [Rule("a", "3/10s", key="user"), Rule("b", "5/minute", algorithm="token_bucket")]
    clocks = [ManualClock(), ManualClock()]
    sync = Limiter(MemoryBackend(clock=clocks[0]))
    asyn = Limiter(MemoryBackend(clock=clocks[1]))
    for step in range(40):
        ctx = req(user_id=f"u{step % 3}", peer=f"198.51.100.{step % 2}")
        d1 = sync.evaluate(ctx, rules)
        d2 = await asyn.aevaluate(ctx, rules)
        assert d1 == d2
        for c in clocks:
            c.advance(0.7)


def test_needs_user() -> None:
    by_ip = Rule("ip", "1/s")
    by_user = Rule("user", "1/s", key="user")
    chained = Rule("chain", "1/s", key="user_or_ip")
    custom = Rule("custom", "1/s", key=lambda ctx: "x")
    limiter = Limiter(MemoryBackend(), global_rules=[by_user])
    assert not limiter.needs_user([by_ip])
    assert limiter.needs_user([chained])
    assert limiter.needs_user([custom])  # a callable may read ctx.user_id
    assert limiter.needs_user([Rule("w", "1/s", when=lambda ctx: ctx.user_id is None)])
    assert limiter.needs_user([Rule("c", "5/s", cost=lambda ctx: 1)])
    assert limiter.needs_user(include_global=True)
    with_lists = Limiter(MemoryBackend(), deny=AccessList(users=["evil"]))
    assert with_lists.needs_user([by_ip])
    assert not Limiter(MemoryBackend(), deny=AccessList(networks=["10.0.0.0/8"])).needs_user()
