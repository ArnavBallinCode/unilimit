"""Redis backend: sync via ``redis.Redis``, async via ``redis.asyncio.Redis``.

Each script call evaluates its keys atomically. On standalone Redis (and Sentinel) a
whole batch is one script call, so it is fully atomic. On Redis Cluster a batch is
split by hash slot; see :meth:`RedisBackend.consume` for the multi-slot protocol.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from contextlib import asynccontextmanager, contextmanager, suppress
from dataclasses import dataclass
from typing import Any

import redis
import redis.asyncio
import redis.asyncio.cluster
import redis.asyncio.retry
import redis.cluster
from redis import exceptions as rex
from redis.backoff import NoBackoff
from redis.retry import Retry

from unilimit._logging import throttled
from unilimit.backends.base import (
    BatchResult,
    ConsumeOp,
    LeaseResult,
    OpResult,
    check_unique_keys,
)
from unilimit.backends.redis import _errors
from unilimit.backends.redis._protocol import (
    LEASE_SCRIPT,
    MODE_CONSUME,
    MODE_PEEK,
    SCRIPT,
    Group,
    ScriptReply,
    commit_order,
    enforcing_allowed,
    finished,
    parse_reply,
    place,
    plan_groups,
    script_args,
)
from unilimit.core.clock import Clock
from unilimit.core.errors import BackendError, BackendUnavailable, ConfigurationError
from unilimit.core.keys import key_hash_slot
from unilimit.observability import BreakerEvent, Emitter, now


@dataclass
class _Progress:
    """Tracks whether any group of the current batch has been committed."""

    committed_any: bool = False


@dataclass
class _AsyncEntry:
    client: Any
    script: Any
    lease_script: Any
    gate: asyncio.Semaphore
    owned: bool
    init_lock: asyncio.Lock
    ready: bool = False


class RedisBackend:
    """Distributed rate-limit storage on Redis (standalone, Sentinel or Cluster).

    Build it from a URL (clients are created lazily, one async client per event loop)
    or pass redis-py clients you manage yourself. Clients built from a URL use short
    timeouts and retry **once, immediately, and only on connection errors**. That
    recovers stale pooled connections (after a Redis restart or an idle connection drop)
    without failing open. Timeouts are never retried, so outages do not multiply
    latency. If the server ran a script but the reply was lost, the retry consumes
    quota a second time: conservative, never over-admission.

    At most ``max_connections`` script calls are in flight per process (per event loop
    for async). Further calls wait up to ``pool_timeout`` seconds for a slot instead of
    failing, so traffic bursts cannot exhaust the connection pool and fail open. Pool
    exhaustion is reported as :class:`BackendUnavailable` but never starts a cooldown,
    because Redis itself is healthy.

    Circuit breaker: after a Redis failure (connection error, timeout, cluster down)
    the backend fails fast for ``cooldown`` seconds without touching the network, so
    fail-open does not add a timeout to every request. When the cooldown expires,
    exactly one request probes Redis while concurrent requests keep failing fast. A
    successful probe closes the breaker at once; a failed probe starts a new cooldown.

    ``clock`` overrides Redis server time. It exists for deterministic tests; production
    deployments should leave it unset so every worker shares the server clock.
    """

    def __init__(
        self,
        url: str | None = None,
        *,
        client: Any = None,
        async_client: Any = None,
        cluster: bool | None = None,
        socket_timeout: float | None = 0.5,
        socket_connect_timeout: float | None = 1.0,
        max_connections: int = 32,
        pool_timeout: float = 1.0,
        cooldown: float = 0.5,
        clock: Clock | None = None,
        client_options: Mapping[str, Any] | None = None,
    ) -> None:
        if url is None and client is None and async_client is None:
            raise ConfigurationError("RedisBackend needs a url, a client or an async_client")
        if url is not None and (client is not None or async_client is not None):
            raise ConfigurationError("pass either a url or explicit clients, not both")
        for name, value in (("cooldown", cooldown), ("pool_timeout", pool_timeout)):
            if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
                raise ConfigurationError(f"{name} must be a non-negative number of seconds")
        if (
            isinstance(max_connections, bool)
            or not isinstance(max_connections, int)
            or (max_connections < 1)
        ):
            raise ConfigurationError("max_connections must be a positive integer")

        supplied_cluster = isinstance(client, redis.cluster.RedisCluster) or isinstance(
            async_client, redis.asyncio.cluster.RedisCluster
        )
        if cluster is None:
            cluster = supplied_cluster
        elif (client is not None or async_client is not None) and cluster != supplied_cluster:
            raise ConfigurationError("cluster= does not match the supplied client type")

        self._url = url
        self._cluster = bool(cluster)
        self._options: dict[str, Any] = {
            "socket_timeout": socket_timeout,
            "socket_connect_timeout": socket_connect_timeout,
            "max_connections": max_connections,
            **(client_options or {}),
        }
        self._max_connections = max_connections
        self._pool_timeout = float(pool_timeout)
        self._cooldown = float(cooldown)
        self._clock = clock
        self._lock = threading.Lock()
        self._gate = threading.BoundedSemaphore(max_connections)

        self._client = client
        self._script: Any = client.register_script(SCRIPT) if client is not None else None
        self._lease_script: Any = (
            client.register_script(LEASE_SCRIPT) if client is not None else None
        )
        self._owns_client = False
        self._fixed_async = async_client
        self._fixed_async_script: Any = (
            async_client.register_script(SCRIPT) if async_client is not None else None
        )
        self._fixed_async_lease_script: Any = (
            async_client.register_script(LEASE_SCRIPT) if async_client is not None else None
        )
        # Per event loop: redis.asyncio connections and asyncio primitives are bound to
        # the loop that created them.
        self._async: dict[asyncio.AbstractEventLoop, _AsyncEntry] = {}

        self._down_until = 0.0
        self._down_reason = ""
        self._probing = False
        self._breaker_lock = threading.Lock()
        self._emitters: tuple[Emitter, ...] = ()

    @classmethod
    def from_url(cls, url: str, **kwargs: Any) -> RedisBackend:
        return cls(url, **kwargs)

    @property
    def cluster(self) -> bool:
        return self._cluster

    @property
    def name(self) -> str:
        return "redis-cluster" if self._cluster else "redis"

    def attach_emitter(self, emitter: Emitter) -> None:
        """Called by :class:`~unilimit.Limiter`: circuit breaker transitions are emitted
        to its observers."""
        self._emitters = (*self._emitters, emitter)

    def _emit_breaker(self, state: str, error: str | None) -> None:
        for emitter in self._emitters:
            emitter.emit(
                lambda: BreakerEvent(
                    timestamp=now(),
                    backend=self.name,
                    state=state,  # type: ignore[arg-type]
                    error=error,
                    cooldown=self._cooldown,
                )
            )

    # -- clients -----------------------------------------------------------------------

    def _sync(self) -> tuple[Any, Any]:
        if self._client is None:
            if self._url is None:
                raise ConfigurationError("no sync client: construct RedisBackend with client=")
            with self._lock:
                if self._client is None:
                    retry = Retry(NoBackoff(), 1, supported_errors=(rex.ConnectionError,))
                    if self._cluster:
                        client: Any = redis.cluster.RedisCluster.from_url(
                            self._url, retry=retry, **self._options
                        )
                    else:
                        client = redis.Redis.from_url(self._url, retry=retry, **self._options)
                    self._script = client.register_script(SCRIPT)
                    self._lease_script = client.register_script(LEASE_SCRIPT)
                    self._client = client
                    self._owns_client = True
        return self._client, self._script

    def _async_entry(self) -> _AsyncEntry:
        loop = asyncio.get_running_loop()
        entry = self._async.get(loop)
        if entry is not None:
            return entry
        # Forget entries whose event loop is gone; they can no longer be used.
        for stale in [lp for lp in self._async if lp.is_closed()]:
            del self._async[stale]
        gate = asyncio.Semaphore(self._max_connections)
        if self._fixed_async is not None:
            entry = _AsyncEntry(
                self._fixed_async,
                self._fixed_async_script,
                self._fixed_async_lease_script,
                gate,
                False,
                asyncio.Lock(),
            )
        elif self._url is None:
            raise ConfigurationError("no async client: construct RedisBackend with async_client=")
        else:
            retry = redis.asyncio.retry.Retry(
                NoBackoff(), 1, supported_errors=(rex.ConnectionError,)
            )
            if self._cluster:
                client: Any = redis.asyncio.cluster.RedisCluster.from_url(
                    self._url, retry=retry, **self._options
                )
            else:
                client = redis.asyncio.Redis.from_url(self._url, retry=retry, **self._options)
            entry = _AsyncEntry(
                client,
                client.register_script(SCRIPT),
                client.register_script(LEASE_SCRIPT),
                gate,
                True,
                asyncio.Lock(),
            )
        self._async[loop] = entry
        return entry

    async def _ready_entry(self) -> _AsyncEntry:
        """The loop's entry, with cluster topology discovered before concurrent use.

        A cold redis.asyncio RedisCluster used by many tasks at once races its own
        topology initialisation and breaks in-flight commands, so it is initialised
        exactly once, under a lock, first.
        """
        entry = self._async_entry()
        if not entry.ready:
            async with entry.init_lock:
                if not entry.ready:
                    if self._cluster:
                        await entry.client.initialize()
                    entry.ready = True
        return entry

    # -- concurrency gate --------------------------------------------------------------

    def _overloaded(self) -> BackendUnavailable:
        return BackendUnavailable(
            f"no Redis connection became available within {self._pool_timeout}s "
            f"(max_connections={self._max_connections})"
        )

    @contextmanager
    def _slot(self) -> Iterator[None]:
        if not self._gate.acquire(blocking=False) and not self._gate.acquire(
            timeout=self._pool_timeout
        ):
            raise self._overloaded()
        try:
            yield
        finally:
            self._gate.release()

    @asynccontextmanager
    async def _aslot(self, gate: asyncio.Semaphore) -> AsyncIterator[None]:
        if not gate.locked():
            await gate.acquire()
        else:
            try:
                async with asyncio.timeout(self._pool_timeout):
                    await gate.acquire()
            except TimeoutError:
                raise self._overloaded() from None
        try:
            yield
        finally:
            gate.release()

    # -- failure handling --------------------------------------------------------------

    def _enter_breaker(self) -> bool:
        """Raise while the breaker is open; return True if this call is the probe."""
        if not self._down_until:
            return False
        with self._breaker_lock:
            if not self._down_until:
                return False
            if time.monotonic() < self._down_until or self._probing:
                raise BackendUnavailable(f"Redis unavailable (cooling down): {self._down_reason}")
            self._probing = True
        self._emit_breaker("half_open", None)  # outside the lock: observers are user code
        return True

    def _trip(self, error: BaseException, cause: BaseException | None = None) -> None:
        with self._breaker_lock:
            first = not self._down_until
            self._down_reason = str(error)
            self._down_until = time.monotonic() + self._cooldown
        if first:
            throttled.log(
                logging.WARNING,
                "redis_unavailable",
                "Redis unavailable (%s); failing fast for %.2fs before probing",
                error,
                self._cooldown,
            )
        self._emit_breaker("open", type(cause or error).__name__)

    def _close_breaker(self) -> None:
        with self._breaker_lock:
            was_open = bool(self._down_until)
            self._down_until = 0.0
        if was_open:
            throttled.log(logging.INFO, "redis_recovered", "Redis is reachable again")
            throttled.flush()
            self._emit_breaker("closed", None)

    @contextmanager
    def _errors(self) -> Iterator[_Progress]:
        probe = self._enter_breaker()
        progress = _Progress()
        try:
            yield progress
        except BackendUnavailable as exc:  # raised locally (pool exhaustion): no cooldown
            raise BackendUnavailable(str(exc), partial_commit=progress.committed_any) from exc
        except BackendError:
            # Raised after Redis replied (e.g. unreadable state): Redis is reachable.
            if probe or self._down_until:
                self._close_breaker()
            raise
        except _errors.HANDLED as exc:
            error = _errors.translate(exc, partial_commit=progress.committed_any)
            if self._cooldown and _errors.is_outage(exc):
                self._trip(error, exc)
            elif probe or self._down_until:
                self._close_breaker()  # Redis answered, even if with an error
            raise error from exc
        else:
            if self._down_until:
                self._close_breaker()
        finally:
            if probe:
                self._probing = False

    def _now(self) -> int | None:
        return self._clock.now_us() if self._clock is not None else None

    # -- sync protocol -----------------------------------------------------------------

    def _call(
        self,
        client: Any,
        script: Any,
        ops: Sequence[ConsumeOp],
        group: Group,
        mode: str,
        now: int | None,
    ) -> ScriptReply:
        group_ops = group.ops(ops)
        with self._slot():
            reply = script(
                keys=group.keys(ops), args=script_args(group_ops, mode, now), client=client
            )
        return parse_reply(reply, group_ops)

    def consume(self, ops: Sequence[ConsumeOp]) -> BatchResult:
        """Atomically evaluate ``ops`` and commit only if every enforcing op allows.

        Standalone Redis: one script call, fully atomic. Redis Cluster with ops in
        several hash slots:

        1. Read-only evaluation of every slot group. Any rejection returns without
           writing anything.
        2. Each group is then committed by a script that **re-evaluates its rules
           against current state** and writes only if they all still allow. Groups run
           in a deterministic order. If one rejects because state changed after step 1,
           the request is rejected and earlier groups stay consumed: the result has
           ``committed=False, partial_commit=True``. A request is never admitted
           unless every group committed.
        """
        if not ops:
            return BatchResult(True, ())
        if len(ops) == 1:
            op = ops[0]
            with self._errors():
                client, script = self._sync()
                with self._slot():
                    reply = script(
                        keys=[op.key],
                        args=script_args(ops, MODE_CONSUME, self._now()),
                        client=client,
                    )
                parsed = parse_reply(reply, ops)
                return BatchResult(parsed.committed, parsed.results)
        check_unique_keys(ops)
        groups = plan_groups(ops, self._cluster)
        with self._errors() as progress:
            client, script = self._sync()
            now = self._now()
            if len(groups) == 1:
                reply = self._call(client, script, ops, groups[0], MODE_CONSUME, now)
                return BatchResult(reply.committed, reply.results)
            results: list[OpResult | None] = [None] * len(ops)
            for group in groups:
                peek = self._call(client, script, ops, group, MODE_PEEK, now)
                place(results, group, peek.results)
            if not enforcing_allowed(ops, results):
                return BatchResult(False, finished(results))
            for group in commit_order(groups):
                reply = self._call(client, script, ops, group, MODE_CONSUME, now)
                place(results, group, reply.results)
                if not reply.committed:
                    return BatchResult(False, finished(results), progress.committed_any)
                progress.committed_any = True
            return BatchResult(True, finished(results))

    def peek(self, ops: Sequence[ConsumeOp]) -> tuple[OpResult, ...]:
        if not ops:
            return ()
        if len(ops) == 1:
            op = ops[0]
            with self._errors():
                client, script = self._sync()
                with self._slot():
                    reply = script(
                        keys=[op.key],
                        args=script_args(ops, MODE_PEEK, self._now()),
                        client=client,
                    )
                return parse_reply(reply, ops).results
        check_unique_keys(ops)
        with self._errors():
            client, script = self._sync()
            now = self._now()
            results: list[OpResult | None] = [None] * len(ops)
            for group in plan_groups(ops, self._cluster):
                peek = self._call(client, script, ops, group, MODE_PEEK, now)
                place(results, group, peek.results)
            return finished(results)

    def reset(self, keys: Sequence[str]) -> None:
        if not keys:
            return
        with self._errors():
            client, _ = self._sync()
            for group in _slot_groups(keys, self._cluster):
                with self._slot():
                    client.delete(*group)

    def close(self) -> None:
        """Close the sync client if this backend created it."""
        with self._lock:
            client, owned = self._client, self._owns_client
            if owned:
                self._client = None
                self._script = None
                self._lease_script = None
                self._owns_client = False
        if owned and client is not None:
            if self._cluster:
                # redis-py's RedisCluster.close() closes each node's Redis client, but
                # those clients wrap explicitly supplied pools, which Redis.close() does
                # not disconnect. Without this, one socket per used node leaks.
                for node in client.get_nodes():
                    if node.redis_connection is not None:
                        node.redis_connection.connection_pool.disconnect()
            client.close()

    def acquire_lease(
        self, key: str, lease_id: str, max_concurrent: int, ttl_us: int, now_us: int = 0
    ) -> LeaseResult:
        if now_us <= 0 and self._clock is not None:
            now_us = self._clock.now_us()
        key_ttl_sec = max(1, (ttl_us * 2 + 999_999) // 1_000_000)
        with self._errors():
            client, _ = self._sync()
            with self._slot():
                reply = self._lease_script(
                    keys=[key],
                    args=[
                        "a",
                        lease_id,
                        str(now_us),
                        str(ttl_us),
                        str(max_concurrent),
                        str(key_ttl_sec),
                    ],
                    client=client,
                )
            allowed = bool(reply[0])
            current = int(reply[1])
            max_c = int(reply[2])
            retry_after_us = int(reply[3]) if int(reply[3]) > 0 else None
            return LeaseResult(
                allowed=allowed,
                current=current,
                max_concurrent=max_c,
                retry_after_us=retry_after_us,
            )

    def renew_lease(self, key: str, lease_id: str, ttl_us: int, now_us: int = 0) -> bool:
        if now_us <= 0 and self._clock is not None:
            now_us = self._clock.now_us()
        key_ttl_sec = max(1, (ttl_us * 2 + 999_999) // 1_000_000)
        with self._errors():
            client, _ = self._sync()
            with self._slot():
                reply = self._lease_script(
                    keys=[key],
                    args=["r", lease_id, str(now_us), str(ttl_us), "0", str(key_ttl_sec)],
                    client=client,
                )
            return bool(reply[0])

    def release_lease(self, key: str, lease_id: str) -> None:
        with suppress(Exception), self._errors():
            client, _ = self._sync()
            with self._slot():
                self._lease_script(
                    keys=[key],
                    args=["x", lease_id, "0", "0", "0", "0"],
                    client=client,
                )

    # -- async protocol ----------------------------------------------------------------

    async def _acall(
        self,
        entry: _AsyncEntry,
        ops: Sequence[ConsumeOp],
        group: Group,
        mode: str,
        now: int | None,
    ) -> ScriptReply:
        group_ops = group.ops(ops)
        async with self._aslot(entry.gate):
            reply = await entry.script(
                keys=group.keys(ops), args=script_args(group_ops, mode, now), client=entry.client
            )
        return parse_reply(reply, group_ops)

    async def aconsume(self, ops: Sequence[ConsumeOp]) -> BatchResult:
        """Async twin of :meth:`consume` with identical semantics."""
        if not ops:
            return BatchResult(True, ())
        if len(ops) == 1:
            op = ops[0]
            with self._errors():
                entry = await self._ready_entry()
                async with self._aslot(entry.gate):
                    reply = await entry.script(
                        keys=[op.key],
                        args=script_args(ops, MODE_CONSUME, self._now()),
                        client=entry.client,
                    )
                parsed = parse_reply(reply, ops)
                return BatchResult(parsed.committed, parsed.results)
        check_unique_keys(ops)
        groups = plan_groups(ops, self._cluster)
        with self._errors() as progress:
            entry = await self._ready_entry()
            now = self._now()
            if len(groups) == 1:
                reply = await self._acall(entry, ops, groups[0], MODE_CONSUME, now)
                return BatchResult(reply.committed, reply.results)
            results: list[OpResult | None] = [None] * len(ops)
            peeks = await asyncio.gather(
                *(self._acall(entry, ops, g, MODE_PEEK, now) for g in groups)
            )
            for group, peek in zip(groups, peeks, strict=True):
                place(results, group, peek.results)
            if not enforcing_allowed(ops, results):
                return BatchResult(False, finished(results))
            for group in commit_order(groups):
                reply = await self._acall(entry, ops, group, MODE_CONSUME, now)
                place(results, group, reply.results)
                if not reply.committed:
                    return BatchResult(False, finished(results), progress.committed_any)
                progress.committed_any = True
            return BatchResult(True, finished(results))

    async def apeek(self, ops: Sequence[ConsumeOp]) -> tuple[OpResult, ...]:
        if not ops:
            return ()
        if len(ops) == 1:
            op = ops[0]
            with self._errors():
                entry = await self._ready_entry()
                async with self._aslot(entry.gate):
                    reply = await entry.script(
                        keys=[op.key],
                        args=script_args(ops, MODE_PEEK, self._now()),
                        client=entry.client,
                    )
                return parse_reply(reply, ops).results
        check_unique_keys(ops)
        with self._errors():
            entry = await self._ready_entry()
            now = self._now()
            groups = plan_groups(ops, self._cluster)
            replies = await asyncio.gather(
                *(self._acall(entry, ops, g, MODE_PEEK, now) for g in groups)
            )
            results: list[OpResult | None] = [None] * len(ops)
            for group, reply in zip(groups, replies, strict=True):
                place(results, group, reply.results)
            return finished(results)

    async def areset(self, keys: Sequence[str]) -> None:
        if not keys:
            return
        with self._errors():
            entry = await self._ready_entry()
            for group in _slot_groups(keys, self._cluster):
                async with self._aslot(entry.gate):
                    await entry.client.delete(*group)

    async def aacquire_lease(
        self, key: str, lease_id: str, max_concurrent: int, ttl_us: int, now_us: int = 0
    ) -> LeaseResult:
        if now_us <= 0 and self._clock is not None:
            now_us = self._clock.now_us()
        key_ttl_sec = max(1, (ttl_us * 2 + 999_999) // 1_000_000)
        with self._errors():
            entry = await self._ready_entry()
            async with self._aslot(entry.gate):
                reply = await entry.lease_script(
                    keys=[key],
                    args=[
                        "a",
                        lease_id,
                        str(now_us),
                        str(ttl_us),
                        str(max_concurrent),
                        str(key_ttl_sec),
                    ],
                    client=entry.client,
                )
            allowed = bool(reply[0])
            current = int(reply[1])
            max_c = int(reply[2])
            retry_after_us = int(reply[3]) if int(reply[3]) > 0 else None
            return LeaseResult(
                allowed=allowed,
                current=current,
                max_concurrent=max_c,
                retry_after_us=retry_after_us,
            )

    async def arenew_lease(self, key: str, lease_id: str, ttl_us: int, now_us: int = 0) -> bool:
        if now_us <= 0 and self._clock is not None:
            now_us = self._clock.now_us()
        key_ttl_sec = max(1, (ttl_us * 2 + 999_999) // 1_000_000)
        with self._errors():
            entry = await self._ready_entry()
            async with self._aslot(entry.gate):
                reply = await entry.lease_script(
                    keys=[key],
                    args=["r", lease_id, str(now_us), str(ttl_us), "0", str(key_ttl_sec)],
                    client=entry.client,
                )
            return bool(reply[0])

    async def arelease_lease(self, key: str, lease_id: str) -> None:
        with suppress(Exception), self._errors():
            entry = await self._ready_entry()
            async with self._aslot(entry.gate):
                await entry.lease_script(
                    keys=[key],
                    args=["x", lease_id, "0", "0", "0", "0"],
                    client=entry.client,
                )

    async def aclose(self) -> None:
        """Close the async client created for the running event loop, if any.

        Clients bound to other, still-running loops must be closed from those loops.
        Clients passed in by the caller are never closed.
        """
        entry = self._async.pop(asyncio.get_running_loop(), None)
        if entry is not None and entry.owned:
            await entry.client.aclose()


def _slot_groups(keys: Sequence[str], cluster: bool) -> list[list[str]]:
    if not cluster:
        return [list(keys)]
    groups: dict[int, list[str]] = {}
    for key in keys:
        groups.setdefault(key_hash_slot(key), []).append(key)
    return list(groups.values())
