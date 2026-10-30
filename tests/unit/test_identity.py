import ipaddress
import logging

import pytest

from unilimit import ConfigurationError, Identity, RequestContext, TrustedProxies
from unilimit.core.identity import (
    IdentityConfig,
    IdentityResolver,
    coerce_identity_value,
    normalise_client_ip,
    normalise_key_spec,
    parse_ip,
)


def resolve_ip(
    peer: str | None,
    headers: dict[str, str] | list[tuple[str, str]] | None = None,
    proxies: TrustedProxies | None = None,
    ipv6_prefix: int = 64,
    client_ip: str | None = None,
) -> str | None:
    ctx = RequestContext(peer=peer, headers=headers, client_ip=client_ip)
    return IdentityResolver(ctx, IdentityConfig(proxies, ipv6_prefix)).client_ip


LB = TrustedProxies(["10.0.0.0/8", "fd00::/8"])


# -- parse_ip ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1.2.3.4", "1.2.3.4"),
        (" 1.2.3.4 ", "1.2.3.4"),
        ("1.2.3.4:8080", "1.2.3.4"),
        ('"1.2.3.4"', "1.2.3.4"),
        ("2001:db8::1", "2001:db8::1"),
        ("[2001:db8::1]:443", "2001:db8::1"),
        ('"[2001:db8:cafe::17]:4711"', "2001:db8:cafe::17"),
        ("fe80::1%eth0", "fe80::1"),
        ("::ffff:192.0.2.1", "192.0.2.1"),
        ("_hidden", None),
        ("unknown", None),
        ("", None),
        ("1.2.3", None),
        ("[2001:db8::1", None),
        ("999.1.1.1", None),
    ],
)
def test_parse_ip(text: str, expected: str | None) -> None:
    addr = parse_ip(text)
    assert (None if addr is None else str(addr)) == expected


def test_ipv6_aggregation() -> None:
    v6 = ipaddress.IPv6Address("2001:db8:1:2:3:4:5:6")
    assert normalise_client_ip(v6, 64) == "2001:db8:1:2::/64"
    assert normalise_client_ip(v6, 128) == "2001:db8:1:2:3:4:5:6"
    assert normalise_client_ip(ipaddress.IPv4Address("1.2.3.4"), 64) == "1.2.3.4"
    assert resolve_ip("2001:db8:1:2::a") == resolve_ip("2001:db8:1:2::b")
    assert resolve_ip("2001:db8:1:2::a", ipv6_prefix=128) != resolve_ip(
        "2001:db8:1:2::b", ipv6_prefix=128
    )


# -- trusted proxies ---------------------------------------------------------------------


def test_default_trusts_no_headers() -> None:
    spoof = {"x-forwarded-for": "6.6.6.6", "x-real-ip": "6.6.6.6"}
    assert resolve_ip("203.0.113.9", spoof) == "203.0.113.9"
    assert resolve_ip("10.0.0.1", spoof) == "10.0.0.1"


def test_header_ignored_when_peer_is_not_trusted() -> None:
    assert resolve_ip("203.0.113.9", {"x-forwarded-for": "6.6.6.6"}, LB) == "203.0.113.9"


def test_rightmost_untrusted_entry_is_the_client() -> None:
    # The client prepended a forged entry; our proxy appended the real address.
    headers = {"x-forwarded-for": "6.6.6.6, 198.51.100.7, 10.1.1.1"}
    assert resolve_ip("10.0.0.2", headers, LB) == "198.51.100.7"


def test_multiple_header_lines_are_joined_in_order() -> None:
    headers = [("X-Forwarded-For", "6.6.6.6"), ("x-forwarded-for", "198.51.100.7")]
    assert resolve_ip("10.0.0.2", headers, LB) == "198.51.100.7"


def test_all_hops_trusted_uses_leftmost() -> None:
    assert resolve_ip("10.0.0.2", {"x-forwarded-for": "10.9.9.9, 10.1.1.1"}, LB) == "10.9.9.9"


def test_malformed_entry_stops_at_last_trusted_hop() -> None:
    headers = {"x-forwarded-for": "198.51.100.7, garbage, 10.1.1.1"}
    assert resolve_ip("10.0.0.2", headers, LB) == "10.1.1.1"
    assert resolve_ip("10.0.0.2", {"x-forwarded-for": "nonsense"}, LB) == "10.0.0.2"
    assert resolve_ip("10.0.0.2", {"x-forwarded-for": ""}, LB) == "10.0.0.2"


def test_missing_header_uses_peer() -> None:
    assert resolve_ip("10.0.0.2", {}, LB) == "10.0.0.2"


def test_ipv6_and_ports_in_xff() -> None:
    headers = {"x-forwarded-for": "[2001:db8::5]:1234, fd00::1"}
    assert resolve_ip("fd00::2", headers, LB) == "2001:db8::/64"


def test_ipv4_mapped_peer_matches_ipv4_network() -> None:
    assert resolve_ip("::ffff:10.0.0.2", {"x-forwarded-for": "198.51.100.7"}, LB) == "198.51.100.7"


def test_x_real_ip() -> None:
    proxies = TrustedProxies(["10.0.0.0/8"], header="X-Real-IP")
    assert resolve_ip("10.0.0.2", {"x-real-ip": "198.51.100.7"}, proxies) == "198.51.100.7"
    assert resolve_ip("10.0.0.2", {"x-real-ip": "1.1.1.1, 2.2.2.2"}, proxies) == "10.0.0.2"
    assert resolve_ip("10.0.0.2", {"x-forwarded-for": "198.51.100.7"}, proxies) == "10.0.0.2"


def test_forwarded_header() -> None:
    proxies = TrustedProxies(["10.0.0.0/8"], header="Forwarded")
    header = 'for=192.0.2.60;proto=http;by=203.0.113.43, for="[2001:db8:cafe::17]:4711"'
    assert resolve_ip("10.0.0.2", {"forwarded": header}, proxies) == "2001:db8:cafe::/64"
    header = "for=192.0.2.60, For=10.3.3.3"
    assert resolve_ip("10.0.0.2", {"forwarded": header}, proxies) == "192.0.2.60"
    # Obfuscated identifiers and elements without for= stop the walk.
    assert resolve_ip("10.0.0.2", {"forwarded": "for=_hidden"}, proxies) == "10.0.0.2"
    assert resolve_ip("10.0.0.2", {"forwarded": "proto=https"}, proxies) == "10.0.0.2"


def test_trusted_proxies_validation(caplog: pytest.LogCaptureFixture) -> None:
    with pytest.raises(ConfigurationError):
        TrustedProxies([])
    with pytest.raises(ConfigurationError):
        TrustedProxies(["not-a-network"])
    with pytest.raises(ConfigurationError):
        TrustedProxies(["10.0.0.0/8"], header="CF-Connecting-IP")
    with caplog.at_level(logging.WARNING, logger="unilimit"):
        TrustedProxies(["0.0.0.0/0"])
    assert "trusts every address" in caplog.text
    single = TrustedProxies("10.0.0.0/8")
    assert len(single.networks) == 1
    assert "10.0.0.0/8" in repr(single)


def test_non_ip_peer_is_an_opaque_identity() -> None:
    assert resolve_ip("testclient") == "testclient"
    assert resolve_ip("/run/app.sock", {"x-forwarded-for": "1.1.1.1"}, LB) == "/run/app.sock"
    assert resolve_ip(None) is None
    assert resolve_ip("   ") is None


def test_explicit_client_ip_is_used_as_given() -> None:
    assert resolve_ip("10.0.0.2", client_ip="198.51.100.7") == "198.51.100.7"
    assert resolve_ip(None, client_ip="2001:db8::1:2") == "2001:db8::/64"
    assert resolve_ip(None, client_ip="worker-7") == "worker-7"


# -- identity values and key specs -------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("abc", "abc"),
        (42, "42"),
        (0, "0"),
        (True, None),
        ("", None),
        ("   ", None),
        ("x" * 1025, None),
        ("x" * 1024, "x" * 1024),
        (None, None),
        (4.2, None),
        (b"bytes", None),
    ],
)
def test_coerce_identity_value(value: object, expected: str | None) -> None:
    assert coerce_identity_value(value) == expected


def test_identity_validation_and_redaction() -> None:
    ident = Identity("api_key", "sk_live_secret")
    assert "sk_live_secret" not in repr(ident)
    assert Identity.of("x") == Identity("custom", "x")
    assert Identity.of(ident) is ident
    with pytest.raises(ValueError, match="identity kind"):
        Identity("Bad Kind", "x")
    with pytest.raises(ValueError, match="identity value"):
        Identity("custom", "")


def test_normalise_key_spec() -> None:
    def fn(ctx: RequestContext) -> str:
        return "x"

    assert normalise_key_spec("ip") == ("ip",)
    assert normalise_key_spec("user_or_ip") == ("user", "ip")
    assert normalise_key_spec(["api_key", "user_or_ip", "ip"]) == ("api_key", "user", "ip")
    assert normalise_key_spec(fn) == (fn,)
    assert normalise_key_spec((fn, "global")) == (fn, "global")


def test_resolver_fallback_chain_and_malformed_values(caplog: pytest.LogCaptureFixture) -> None:
    cfg = IdentityConfig(api_key_header="X-Key", tenant_header="X-Tenant")
    anon = IdentityResolver(RequestContext(peer="1.2.3.4"), cfg)
    assert anon.resolve(("user", "ip"), "r") == Identity("ip", "1.2.3.4")

    authed = IdentityResolver(RequestContext(peer="1.2.3.4", user_id=7), cfg)
    assert authed.resolve(("user", "ip"), "r") == Identity("user", "7")

    headers = {"x-key": "k1", "x-tenant": "acme"}
    r = IdentityResolver(RequestContext(headers=headers), cfg)
    assert r.resolve(("api_key",), "r") == Identity("api_key", "k1")
    assert r.resolve(("tenant",), "r") == Identity("tenant", "acme")
    assert r.resolve(("global",), "r") == Identity("global", "*")
    assert r.resolve((lambda ctx: 99,), "r") == Identity("custom", "99")
    assert r.resolve(("user",), "r") is None

    with caplog.at_level(logging.WARNING, logger="unilimit"):
        bad = IdentityResolver(RequestContext(user_id="x" * 5000, peer="1.2.3.4"), cfg)
        assert bad.resolve(("user", "ip"), "r") == Identity("ip", "1.2.3.4")
    assert "malformed user identity" in caplog.text
    assert "x" * 50 not in caplog.text


def test_tenant_requires_configuration() -> None:
    r = IdentityResolver(RequestContext(), IdentityConfig())
    with pytest.raises(ConfigurationError, match="tenant_header"):
        r.resolve(("tenant",), "r")


def test_identity_config_validation() -> None:
    for bad in (0, 129, True):
        with pytest.raises(ConfigurationError):
            IdentityConfig(ipv6_prefix=bad)


def test_request_context_normalisation() -> None:
    ctx = RequestContext(method="post", headers={"X-A": "1"})
    assert ctx.method == "POST"
    assert ctx.headers["x-a"] == "1"
    with pytest.raises(TypeError):
        ctx.headers["x-b"] = "2"  # type: ignore[index]


# -- header: keys and IPv4 grouping (after django-ratelimit) -----------------------------


def test_header_key_sources() -> None:
    assert normalise_key_spec("header:X-Client-Id") == ("header:x-client-id",)
    assert normalise_key_spec(["header:X-A", "ip"]) == ("header:x-a", "ip")
    for bad in ("header:", "header:bad name", "header:a/b"):
        with pytest.raises(ConfigurationError, match="header"):
            normalise_key_spec(bad)
    r = IdentityResolver(RequestContext(peer="1.2.3.4", headers={"X-B": "same"}), IdentityConfig())
    assert r.resolve(("header:x-a", "header:x-b"), "r") == Identity("header", "x-b:same")
    other = IdentityResolver(RequestContext(headers={"X-A": "same"}), IdentityConfig())
    # Same value under different header names: different identities, different counters.
    assert other.resolve(("header:x-a", "header:x-b"), "r") != r.resolve(
        ("header:x-a", "header:x-b"), "r"
    )
    assert r.resolve(("header:x-missing",), "r") is None


def test_ipv4_grouping() -> None:
    v4 = ipaddress.IPv4Address("198.51.100.77")
    assert normalise_client_ip(v4, 64) == "198.51.100.77"  # default: no grouping
    assert normalise_client_ip(v4, 64, 24) == "198.51.100.0/24"
    assert resolve_ip("198.51.100.1", ipv6_prefix=64) != resolve_ip("198.51.100.2")
    grouped = IdentityConfig(ipv4_prefix=24)
    a = IdentityResolver(RequestContext(peer="198.51.100.1"), grouped).client_ip
    b = IdentityResolver(RequestContext(peer="198.51.100.200"), grouped).client_ip
    assert a == b == "198.51.100.0/24"
    for bad in (0, 33, True):
        with pytest.raises(ConfigurationError, match="ipv4_prefix"):
            IdentityConfig(ipv4_prefix=bad)
