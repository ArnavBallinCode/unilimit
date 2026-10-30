import re

import pytest

from unilimit import Algorithm, ConfigurationError, Identity, Limiter, MemoryBackend, Rule
from unilimit.core.keys import (
    crc16,
    hash_tag,
    identity_digest,
    key_hash_slot,
    normalise_secret,
    rate_limit_key,
    rule_segment,
    validate_namespace,
)

# Verified with `CLUSTER KEYSLOT` against Redis 8.8 during the M1 review.
SERVER_VERIFIED_SLOTS = [
    ("unilimit:v1:rl:api:sw:{Zm9vYmFyYmF6cXV4MTIzNA}", 15182),
    ("unilimit:v1:rl:login:tb:{Zm9vYmFyYmF6cXV4MTIzNA}", 15182),
    ("Zm9vYmFyYmF6cXV4MTIzNA", 15182),
    ("{ns}:v1:rl:{rule}:{algo}:{Zm9vYmFyYmF6cXV4MTIzNA}", 9937),
    ("{ns}:v1:rl:{other}:{algo}:{AAAAAAAAAAAAAAAAAAAAAA}", 9937),
    ("foo{}{bar}", 8363),
    ("foo{{bar}}zap", 4015),
    ("foo{bar}{zap}", 5061),
    ("123456789", 12739),
]


def test_crc16_xmodem_check_value() -> None:
    assert crc16(b"123456789") == 0x31C3


@pytest.mark.parametrize(("key", "slot"), SERVER_VERIFIED_SLOTS)
def test_hash_slots_match_redis(key: str, slot: int) -> None:
    assert key_hash_slot(key) == slot


def test_hash_tag_rules() -> None:
    assert hash_tag("{user1000}.following") == b"user1000"
    assert hash_tag("foo{}{bar}") == b"foo{}{bar}"  # empty tag: whole key
    assert hash_tag("foo{{bar}}zap") == b"{bar"
    assert hash_tag("foo{bar}{zap}") == b"bar"
    assert hash_tag("no-tag") == b"no-tag"
    assert hash_tag(b"open{only") == b"open{only"


def test_key_format() -> None:
    digest = identity_digest("user", "42")
    key = rate_limit_key("myapp", "api", Algorithm.SLIDING_WINDOW, digest)
    assert key == f"myapp:v1:rl:api:sw:{{{digest}}}"
    assert hash_tag(key) == digest.encode()


@pytest.mark.parametrize(
    "rule_name", ["api", "GET /items/{id}", "a}b{c", "{", "x" * 100, "ünïcode", "~deadbeef"]
)
def test_every_key_for_one_identity_shares_a_slot(rule_name: str) -> None:
    digest = identity_digest("ip", "203.0.113.9")
    slots = {
        key_hash_slot(rate_limit_key(ns, rule_name, algo, digest))
        for ns in ("unilimit", "a.b-c_d")
        for algo in Algorithm
    }
    assert slots == {key_hash_slot(digest)}


def test_limiter_keys_colocate_per_identity() -> None:
    limiter = Limiter(MemoryBackend())
    rules = [Rule("a", "1/s"), Rule("GET /x/{y}", "5/m", algorithm="token_bucket")]
    ident = Identity("user", "7")
    assert len({key_hash_slot(limiter.key_for(r, ident)) for r in rules}) == 1


def test_rule_segments() -> None:
    assert rule_segment("free-tier.v2:x_y") == "free-tier.v2:x_y"
    hashed = rule_segment("GET /items/{id}")
    assert re.fullmatch(r"~[0-9a-f]{16}", hashed)
    assert rule_segment("x" * 65).startswith("~")
    # A literal name can never look like a hashed segment.
    assert rule_segment("~deadbeefdeadbeef") != "~deadbeefdeadbeef"
    assert "{" not in hashed
    assert "}" not in hashed


def test_identity_digest_properties() -> None:
    d = identity_digest("ip", "1.2.3.4")
    assert len(d) == 22
    assert re.fullmatch(r"[A-Za-z0-9_-]{22}", d)
    assert d == identity_digest("ip", "1.2.3.4")
    assert d != identity_digest("user", "1.2.3.4")  # kind separates namespaces
    # Kinds cannot contain NUL, so `kind ‖ 0x00 ‖ value` can never be ambiguous.
    assert identity_digest("a", "b\x00c") != identity_digest("ab", "c")
    with pytest.raises(ValueError, match="identity kind"):
        identity_digest("a\x00b", "c")
    secret = normalise_secret("s3cret")
    assert identity_digest("ip", "1.2.3.4", secret) != d
    assert "1.2.3.4" not in d


def test_secrets() -> None:
    assert normalise_secret(None) is None
    assert normalise_secret("abc") == b"abc"
    long = normalise_secret(b"x" * 500)
    assert long is not None
    assert len(long) == 64
    with pytest.raises(ConfigurationError):
        normalise_secret("")


@pytest.mark.parametrize("ns", ["", "a" * 33, "has space", "a:b", "{tag}", "ü"])
def test_invalid_namespaces(ns: str) -> None:
    with pytest.raises(ConfigurationError):
        validate_namespace(ns)
