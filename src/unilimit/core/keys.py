"""Storage key schema (v1) and identity digests.

Rate-limit keys have the form::

    <ns>:v1:rl:<rule>:<algo>:{<digest>}

The braces around the digest are the only literal braces in the key. Redis Cluster
hashes the substring between the first ``{`` and the next ``}``, so every key for one
identity maps to one hash slot, whatever the rule or algorithm. The namespace and rule
segments are restricted to charsets without braces to guarantee this.
"""

from __future__ import annotations

import base64
import hashlib
import re

from unilimit.algorithms import Algorithm
from unilimit.core.errors import ConfigurationError

KEY_SCHEMA_VERSION = "v1"

_NAMESPACE_RE = re.compile(r"[A-Za-z0-9_.-]{1,32}")
_RULE_SEGMENT_RE = re.compile(r"[A-Za-z0-9_.:-]{1,64}")
_KIND_RE = re.compile(r"[a-z][a-z0-9_]{0,31}")

_BLAKE2B_MAX_KEY = 64


def validate_namespace(namespace: str) -> str:
    if not isinstance(namespace, str) or not _NAMESPACE_RE.fullmatch(namespace):
        raise ConfigurationError(
            f"invalid namespace {namespace!r}: use 1-32 characters from [A-Za-z0-9_.-]"
        )
    return namespace


def validate_kind(kind: str) -> str:
    if not isinstance(kind, str) or not _KIND_RE.fullmatch(kind):
        raise ValueError(f"invalid identity kind {kind!r}: use [a-z][a-z0-9_]{{0,31}}")
    return kind


def rule_segment(name: str) -> str:
    """The rule name if it is key-safe, otherwise ``~`` + 16 hex chars of its SHA-256.

    ``~`` is outside the safe charset, so hashed segments never collide with literal names.
    """
    if _RULE_SEGMENT_RE.fullmatch(name):
        return name
    return "~" + hashlib.sha256(name.encode("utf-8")).hexdigest()[:16]


def normalise_secret(secret: str | bytes | None) -> bytes | None:
    """Convert ``identity_secret`` to a BLAKE2b key (at most 64 bytes)."""
    if secret is None:
        return None
    raw = secret.encode("utf-8") if isinstance(secret, str) else secret
    if not isinstance(raw, bytes) or not raw:
        raise ConfigurationError("identity_secret must be a non-empty str or bytes")
    if len(raw) > _BLAKE2B_MAX_KEY:
        raw = hashlib.blake2b(raw, digest_size=_BLAKE2B_MAX_KEY).digest()
    return raw


def identity_digest(kind: str, value: str, secret: bytes | None = None) -> str:
    """22-character unpadded base64url BLAKE2b-128 of ``kind ‖ 0x00 ‖ value``.

    ``kind`` is validated to exclude NUL, so the encoding is unambiguous.
    """
    validate_kind(kind)
    h = hashlib.blake2b(digest_size=16, key=secret or b"")
    h.update(kind.encode("utf-8"))
    h.update(b"\x00")
    h.update(value.encode("utf-8"))
    return base64.urlsafe_b64encode(h.digest()).rstrip(b"=").decode("ascii")


def rate_limit_key(namespace: str, rule_name: str, algorithm: Algorithm, digest: str) -> str:
    return (
        f"{namespace}:{KEY_SCHEMA_VERSION}:rl:{rule_segment(rule_name)}:"
        f"{algorithm.code}:{{{digest}}}"
    )


# -- Redis Cluster hash slots (CRC16/XMODEM, as specified by Redis Cluster) ------------


def _crc16_table() -> tuple[int, ...]:
    table = []
    for byte in range(256):
        crc = byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else (crc << 1)
        table.append(crc & 0xFFFF)
    return tuple(table)


_CRC16_TABLE = _crc16_table()


def crc16(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc = ((crc << 8) & 0xFFFF) ^ _CRC16_TABLE[((crc >> 8) ^ byte) & 0xFF]
    return crc


def hash_tag(key: str | bytes) -> bytes:
    """The part of ``key`` that Redis Cluster hashes (the hash tag, if present)."""
    raw = key.encode("utf-8") if isinstance(key, str) else key
    start = raw.find(b"{")
    if start != -1:
        end = raw.find(b"}", start + 1)
        if end != -1 and end != start + 1:
            return raw[start + 1 : end]
    return raw


def key_hash_slot(key: str | bytes) -> int:
    """The Redis Cluster hash slot (0-16383) of ``key``."""
    return crc16(hash_tag(key)) % 16384
