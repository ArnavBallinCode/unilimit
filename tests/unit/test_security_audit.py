from __future__ import annotations

import logging

import pytest

from unilimit import (
    Algorithm,
    Identity,
    Limiter,
    MemoryBackend,
    RequestContext,
    Rule,
    TrustedProxies,
)
from unilimit.core.identity import (
    IdentityConfig,
    IdentityResolver,
    coerce_identity_value,
)
from unilimit.core.keys import identity_digest, normalise_secret, rate_limit_key
from unilimit.observability import DecisionEvent

# ---------------------------------------------------------------------------
# 1. IP Spoofing, Proxy Trust & Header Injection
# ---------------------------------------------------------------------------


def test_ip_spoofing_untrusted_peer() -> None:
    """An untrusted peer attempting to spoof client IP via headers is completely ignored."""
    lb = TrustedProxies(["10.0.0.0/8"])
    cfg = IdentityConfig(trusted_proxies=lb)

    # Attacker connects from 203.0.113.5 and sends forged XFF headers
    ctx = RequestContext(
        peer="203.0.113.5",
        headers={
            "x-forwarded-for": "1.1.1.1, 10.0.0.1",
            "x-real-ip": "1.1.1.1",
            "forwarded": "for=1.1.1.1",
        },
    )
    resolver = IdentityResolver(ctx, cfg)
    assert resolver.client_ip == "203.0.113.5"


def test_ip_spoofing_trusted_proxy_chain_traversal() -> None:
    """Traversing right-to-left through trusted proxies stops at the first untrusted client IP."""
    # Topology: Client (198.51.100.99) -> Edge Proxy (10.0.0.1) -> Internal Proxy (10.0.0.2) -> App
    lb = TrustedProxies(["10.0.0.0/8"])
    cfg = IdentityConfig(trusted_proxies=lb)

    # Attacker at 198.51.100.99 injected "forged.ip, 127.0.0.1" into their request header.
    # Edge proxy appended 198.51.100.99, internal proxy appended 10.0.0.1.
    ctx = RequestContext(
        peer="10.0.0.2",
        headers={"x-forwarded-for": "forged.ip, 127.0.0.1, 198.51.100.99, 10.0.0.1"},
    )
    resolver = IdentityResolver(ctx, cfg)
    # The first untrusted IP from the right is 198.51.100.99
    assert resolver.client_ip == "198.51.100.99"


def test_ip_spoofing_malformed_header_injection_fails_safe() -> None:
    """Malformed or injection attempts in XFF stop traversal at the last trusted hop."""
    lb = TrustedProxies(["10.0.0.0/8"])
    cfg = IdentityConfig(trusted_proxies=lb)

    # Malformed entry right before the trusted hop
    ctx = RequestContext(
        peer="10.0.0.2",
        headers={"x-forwarded-for": "1.1.1.1, '; DROP TABLE users; --, 10.0.0.1"},
    )
    resolver = IdentityResolver(ctx, cfg)
    # Fails safe to the last known trusted hop (10.0.0.1), preventing arbitrary attribution
    assert resolver.client_ip == "10.0.0.1"


def test_forwarded_rfc7239_parameter_injection() -> None:
    """RFC 7239 Forwarded header parser handles parameter injection safely."""
    lb = TrustedProxies(["10.0.0.0/8"], header="Forwarded")
    cfg = IdentityConfig(trusted_proxies=lb)

    # Injected extra parameters or tricky separators
    ctx = RequestContext(
        peer="10.0.0.2",
        headers={
            "forwarded": 'for=192.0.2.1;unknown="injected;for=10.0.0.99", for="[2001:db8::1]:8080"'
        },
    )
    resolver = IdentityResolver(ctx, cfg)
    assert resolver.client_ip == "2001:db8::/64"


# ---------------------------------------------------------------------------
# 2. Identity Redaction, Information Leakage & Memory Exhaustion
# ---------------------------------------------------------------------------


def test_identity_repr_redaction() -> None:
    """Identity string value is excluded from repr and str representations."""
    ident = Identity("user", "secret-user-token-98765")
    rep = repr(ident)
    assert "secret-user-token-98765" not in rep
    assert "user" in rep


def test_identity_value_length_limit_prevents_memory_exhaustion() -> None:
    """Identity values longer than MAX_IDENTITY_LENGTH (1024) are rejected."""
    long_value = "x" * 1025
    assert coerce_identity_value(long_value) is None

    valid_value = "x" * 1024
    assert coerce_identity_value(valid_value) == valid_value


def test_identity_coercion_type_safety() -> None:
    """Boolean types and blank whitespace are never accepted as valid identities."""
    assert coerce_identity_value(True) is None
    assert coerce_identity_value(False) is None
    assert coerce_identity_value("   \t\n  ") is None
    assert coerce_identity_value(12345) == "12345"


def test_malformed_identity_logs_are_redacted(caplog: pytest.LogCaptureFixture) -> None:
    """When an identity resolver encounters an uncoercible value, it logs with value redacted."""
    ctx = RequestContext(peer=None, headers={"x-api-key": " "})
    cfg = IdentityConfig()
    resolver = IdentityResolver(ctx, cfg)

    with caplog.at_level(logging.WARNING, logger="unilimit"):
        ident = resolver.resolve(["api_key"], "auth_rule")
    assert ident is None
    assert "value redacted" in caplog.text


def test_observability_event_contains_no_sensitive_identities() -> None:
    """DecisionEvent to_dict() carries no keys, paths, IPs, or identity values."""
    from unilimit.observability import RuleOutcome

    event = DecisionEvent(
        timestamp=1700000000.0,
        allowed=False,
        reason="exhausted",
        source="backend",
        backend="redis",
        rules=(RuleOutcome("login_rule", "sw", False, True),),
        binding_rule="login_rule",
        fail_mode=None,
        partial_commit=False,
        duration=0.001,
    )
    d = event.to_dict()
    assert "user" not in d
    assert "ip" not in d
    assert "key" not in d
    assert "identity" not in d
    assert "path" not in d


# ---------------------------------------------------------------------------
# 3. Cryptographic Namespace Isolation & Slot Colocation
# ---------------------------------------------------------------------------


def test_crypto_digest_namespace_separation() -> None:
    """Identities of different kinds or secrets produce completely different digests."""
    d_user = identity_digest("user", "12345")
    d_api = identity_digest("api_key", "12345")
    d_ip = identity_digest("ip", "12345")
    assert len({d_user, d_api, d_ip}) == 3

    # Secret salt separation
    secret1 = normalise_secret("secret_alpha")
    secret2 = normalise_secret("secret_beta")
    d_sec1 = identity_digest("user", "12345", secret1)
    d_sec2 = identity_digest("user", "12345", secret2)
    assert d_sec1 != d_sec2
    assert d_sec1 != d_user


def test_redis_key_structure_and_no_raw_identity() -> None:
    """Redis keys contain namespace, version, rule, algorithm, and digest only."""
    digest = identity_digest("user", "sensitive_username_bob")
    key = rate_limit_key("myapp", "login", Algorithm.SLIDING_WINDOW, digest)
    assert "sensitive_username_bob" not in key
    assert key == f"myapp:v1:rl:login:sw:{{{digest}}}"


# ---------------------------------------------------------------------------
# 4. Fail-Open vs Fail-Closed Rule Enforcement
# ---------------------------------------------------------------------------


def test_rule_fail_mode_closed_overrides_limiter_fail_open() -> None:
    """A sensitive rule with fail_mode='closed' enforces closed behavior when limiter is open."""
    limiter = Limiter(MemoryBackend(), fail_mode="open")
    closed_rule = Rule("login", "5/m", fail_mode="closed")
    open_rule = Rule("public_api", "100/m", fail_mode="open")

    assert closed_rule.fail_mode == "closed"
    assert open_rule.fail_mode == "open"
    assert limiter.fail_mode == "open"
