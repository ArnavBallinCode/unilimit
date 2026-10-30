"""Pure helpers shared by the sync and async Redis drivers (no I/O).

Stored state is a string ``<algo>:<field>:<field>[:<field>]`` written by the Lua script.
:func:`encode_state` / :func:`decode_state` mirror that format exactly; they are used by
tests and operational tooling, never on the request path.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from importlib.resources import files
from typing import Any

from unilimit.algorithms import Algorithm, State
from unilimit.backends.base import ConsumeOp, OpResult
from unilimit.core.errors import BackendError
from unilimit.core.keys import key_hash_slot

SCRIPT = files(__package__).joinpath("batch.lua").read_text(encoding="utf-8")
LEASE_SCRIPT = files(__package__).joinpath("lease.lua").read_text(encoding="utf-8")

_FIELDS_COMPACT = 4  # allowed, remaining, reset_after, retry_after
_FIELDS_DETAILED = 6  # + ttl, written state

MODE_CONSUME = "c"
MODE_PEEK = "p"
MODE_DIFF_CONSUME = "cd"
MODE_DIFF_PEEK = "pd"

_BY_CODE = {a.code: a for a in Algorithm}


def encode_state(algorithm: Algorithm, state: State) -> str:
    """Exactly the string the Lua script stores for ``state``."""
    if algorithm is Algorithm.TOKEN_BUCKET:
        tokens, last = state
        return f"tb:{float(tokens):.17g}:{int(last)}"
    return ":".join([algorithm.code, *(str(int(v)) for v in state)])


def decode_state(raw: str | bytes) -> tuple[Algorithm, State]:
    text = raw.decode("ascii") if isinstance(raw, bytes) else raw
    code, _, rest = text.partition(":")
    algorithm = _BY_CODE[code]
    fields = rest.split(":")
    if algorithm is Algorithm.TOKEN_BUCKET:
        return algorithm, (float(fields[0]), int(fields[1]))
    return algorithm, tuple(int(f) for f in fields)


def script_args(ops: Sequence[ConsumeOp], mode: str, now_us: int | None) -> list[str]:
    """``now_us=None`` makes the script read the Redis server clock."""
    if len(ops) == 1:
        op = ops[0]
        spec = op.spec
        return [
            mode,
            "" if now_us is None else str(now_us),
            spec.algorithm.code,
            str(spec.limit),
            str(spec.window_us),
            str(spec.capacity),
            str(op.cost),
            "1" if op.enforce else "0",
        ]
    args = [mode, "" if now_us is None else str(now_us)]
    for op in ops:
        spec = op.spec
        args.extend(
            (
                spec.algorithm.code,
                str(spec.limit),
                str(spec.window_us),
                str(spec.capacity),
                str(op.cost),
                "1" if op.enforce else "0",
            )
        )
    return args


@dataclass(frozen=True, slots=True)
class ScriptReply:
    committed: bool
    now_us: int
    results: tuple[OpResult, ...]
    ttls_us: tuple[int, ...]
    """TTL the script computed for each written key (0 when not written)."""
    written: tuple[str | None, ...]
    """Exact state string stored for each key (None when not written)."""


def _text(value: Any) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


def parse_reply(reply: Sequence[Any], ops: Sequence[ConsumeOp]) -> ScriptReply:
    if int(reply[0]) == -1:
        kind, key, stored = _text(reply[1]), _text(reply[2]), _text(reply[3])
        if kind == "MISMATCH":
            expected = next(op.spec.algorithm for op in ops if op.key == key)
            raise BackendError(
                f"state for key {key!r} was written by {_BY_CODE[stored].value}, "
                f"not {expected.value}"
            )
        raise BackendError(
            f"unreadable rate-limit state at key {key!r}; it was not written by UniLimit "
            "or is corrupted. Delete the key (Limiter.reset) to recover."
        )
    num_ops = len(ops)
    reply_len = len(reply)
    if reply_len == 2 + _FIELDS_COMPACT * num_ops:
        if num_ops == 1:
            allowed = int(reply[2])
            remaining = int(reply[3])
            reset = int(reply[4])
            retry = int(reply[5])
            return ScriptReply(
                int(reply[0]) == 1,
                int(reply[1]),
                (OpResult(allowed == 1, remaining, reset, None if retry < 0 else retry),),
                (0,),
                (None,),
            )
        results = []
        for i in range(num_ops):
            base = 2 + _FIELDS_COMPACT * i
            allowed = int(reply[base])
            remaining = int(reply[base + 1])
            reset = int(reply[base + 2])
            retry = int(reply[base + 3])
            results.append(OpResult(allowed == 1, remaining, reset, None if retry < 0 else retry))
        return ScriptReply(
            int(reply[0]) == 1,
            int(reply[1]),
            tuple(results),
            (0,) * num_ops,
            (None,) * num_ops,
        )
    if reply_len == 2 + _FIELDS_DETAILED * num_ops:
        if num_ops == 1:
            allowed = int(reply[2])
            remaining = int(reply[3])
            reset = int(reply[4])
            retry = int(reply[5])
            ttl = int(reply[6])
            raw_written = reply[7]
            written = (_text(raw_written) or None) if raw_written else None
            return ScriptReply(
                int(reply[0]) == 1,
                int(reply[1]),
                (OpResult(allowed == 1, remaining, reset, None if retry < 0 else retry),),
                (ttl,),
                (written,),
            )
        results = []
        ttls = []
        written_list: list[str | None] = []
        for i in range(num_ops):
            base = 2 + _FIELDS_DETAILED * i
            allowed = int(reply[base])
            remaining = int(reply[base + 1])
            reset = int(reply[base + 2])
            retry = int(reply[base + 3])
            ttl = int(reply[base + 4])
            raw_w = reply[base + 5]
            results.append(OpResult(allowed == 1, remaining, reset, None if retry < 0 else retry))
            ttls.append(ttl)
            written_list.append((_text(raw_w) or None) if raw_w else None)
        return ScriptReply(
            int(reply[0]) == 1, int(reply[1]), tuple(results), tuple(ttls), tuple(written_list)
        )
    raise BackendError("unexpected reply shape from the UniLimit script")


@dataclass(frozen=True, slots=True)
class Group:
    """Ops that one script call evaluates atomically (one hash slot in Cluster mode)."""

    indices: tuple[int, ...]
    enforcing: bool

    def ops(self, ops: Sequence[ConsumeOp]) -> list[ConsumeOp]:
        return [ops[i] for i in self.indices]

    def keys(self, ops: Sequence[ConsumeOp]) -> list[str]:
        return [ops[i].key for i in self.indices]


def plan_groups(ops: Sequence[ConsumeOp], cluster: bool) -> list[Group]:
    """One group for standalone Redis; one group per hash slot for Cluster.

    Groups keep first-appearance order, so the plan is deterministic.
    """
    n = len(ops)
    if n == 1:
        return [Group((0,), ops[0].enforce)]
    if not cluster:
        return [Group(tuple(range(n)), any(op.enforce for op in ops))]
    by_slot: dict[int, list[int]] = {}
    for i, op in enumerate(ops):
        by_slot.setdefault(key_hash_slot(op.key), []).append(i)
    return [Group(tuple(idx), any(ops[i].enforce for i in idx)) for idx in by_slot.values()]


def commit_order(groups: Sequence[Group]) -> list[Group]:
    """Enforcing groups first (declaration order), shadow-only groups last.

    Shadow-only groups always commit, so committing them last means they are never
    consumed for a request that a later enforcing group rejects.
    """
    return [g for g in groups if g.enforcing] + [g for g in groups if not g.enforcing]


def place(target: list[OpResult | None], group: Group, results: Sequence[OpResult]) -> None:
    for i, result in zip(group.indices, results, strict=True):
        target[i] = result


def finished(target: list[OpResult | None]) -> tuple[OpResult, ...]:
    assert all(r is not None for r in target)
    return tuple(r for r in target if r is not None)


def enforcing_allowed(ops: Sequence[ConsumeOp], results: Sequence[OpResult | None]) -> bool:
    return all(
        r is not None and r.allowed for op, r in zip(ops, results, strict=True) if op.enforce
    )
