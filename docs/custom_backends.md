# Custom Backends Contract & Conformance

UniLimit is designed with a strict sans-I/O policy: high-level rate limits, rules, and
identities are compiled by `Limiter` into a sequence of low-level, atomic operations
(`BatchOp`). A backend's sole responsibility is to evaluate and commit those operations
atomically.

This architecture makes it straightforward to implement custom storage backends (e.g. for
an internal cache cluster, database, or specialized distributed store) while guaranteeing
100% behavioral equivalence with UniLimit's built-in `MemoryBackend` and `RedisBackend`.

---

## 1. Backend Protocols

Backend contracts are defined as `@runtime_checkable` protocols in `unilimit.backends`:

### `SyncBackend`
```python
from collections.abc import Sequence
from typing import Protocol, runtime_checkable
from unilimit.backends.base import BatchOp, BatchResult


@runtime_checkable
class SyncBackend(Protocol):
    def consume(self, ops: Sequence[BatchOp]) -> BatchResult:
        """Evaluate a batch atomically.

        All operations must be committed if every operation is allowed.
        If any operation fails, no operations must be committed.
        """
        ...
```

### `AsyncBackend`
```python
from collections.abc import Sequence
from typing import Protocol, runtime_checkable
from unilimit.backends.base import BatchOp, BatchResult


@runtime_checkable
class AsyncBackend(Protocol):
    async def aconsume(self, ops: Sequence[BatchOp]) -> BatchResult:
        """Evaluate a batch atomically without blocking the event loop."""
        ...
```

### `LeaseBackend` and `AsyncLeaseBackend` (Optional)
If your backend supports WebSocket or distributed connection concurrency tracking, it
implements the lease protocols:

```python
from typing import Protocol, runtime_checkable
from unilimit.backends.base import Lease, LeaseResult


@runtime_checkable
class LeaseBackend(Protocol):
    def acquire_lease(
        self, key: str, max_leases: int, lease_id: str, ttl_seconds: float
    ) -> LeaseResult: ...

    def release_lease(self, key: str, lease_id: str) -> None: ...

    def renew_lease(self, key: str, lease_id: str, ttl_seconds: float) -> bool: ...


@runtime_checkable
class AsyncLeaseBackend(Protocol):
    async def aacquire_lease(
        self, key: str, max_leases: int, lease_id: str, ttl_seconds: float
    ) -> LeaseResult: ...

    async def arelease_lease(self, key: str, lease_id: str) -> None: ...

    async def arenew_lease(self, key: str, lease_id: str, ttl_seconds: float) -> bool: ...
```

---

## 2. Invariants Custom Backends Must Obey

Any custom backend must strictly adhere to the following invariants:

1. **Atomic Batches (All-or-Nothing):**
   A batch containing multiple operations (e.g. per-second burst limit + daily quota)
   must be evaluated together. If even one operation exceeds its limit, **none** of the
   operations in the batch may increment or consume quota. Never add compensating decrements.

2. **Equivalence with `evaluate_batch`:**
   UniLimit provides a reference implementation in `unilimit.backends.base.evaluate_batch`
   which matches the pure algorithm implementations (`unilimit.algorithms`). Custom storage
   layers must match this behavior and floating-point precision (`%.17g`).

3. **No Raw Identities:**
   The `Limiter` passes storage keys containing keyed HMAC digests. Backends must not
   attempt to unhash or extract raw identities from keys.

4. **Failure Semantics:**
   Transient connection or pool issues must raise `BackendUnavailable` (or inherit from it).
   Configuration or corruption errors must raise descriptive exceptions and fail closed.

---

## 3. Reference Implementation: In-Memory / Adapter

To implement a custom synchronous backend, you can store state dictionaries and delegate
to `evaluate_batch`:

```python
import time
from collections.abc import Sequence
from unilimit.backends.base import BatchOp, BatchResult, evaluate_batch


class CustomInMemoryBackend:
    def __init__(self) -> None:
        self._state: dict[str, tuple[Any, ...]] = {}

    def consume(self, ops: Sequence[BatchOp]) -> BatchResult:
        now_us = int(time.time() * 1_000_000)
        # Fetch current state for all keys in the batch
        batch_state = {op.key: self._state.get(op.key) for op in ops}

        # Evaluate atomically
        result = evaluate_batch(ops, now_us, batch_state)

        # Commit new state if allowed
        if result.committed:
            for op, op_res in zip(ops, result.results):
                self._state[op.key] = op_res.new_state
        return result
```

---

## 4. Conformance Testing Harness

UniLimit provides an automated test harness in `unilimit.testing` to verify any custom
backend against all edge cases, atomicity requirements, and algorithm properties:

```python
import pytest
from unilimit.testing import (
    verify_backend_conformance,
    verify_async_backend_conformance,
    verify_lease_backend_conformance,
    verify_async_lease_backend_conformance,
)
from mypackage.backend import MyCustomBackend


def test_custom_backend_conformance():
    backend = MyCustomBackend()
    # Runs comprehensive checks: single-op, multi-op atomicity, token bucket, sliding window
    verify_backend_conformance(backend)


@pytest.mark.asyncio
async def test_custom_async_backend_conformance():
    backend = MyCustomBackend()
    await verify_async_backend_conformance(backend)
```

The harness tests:
- Single operation increment and exhaustion.
- Multi-operation atomicity (rejection on any limit preserves untouched state for all).
- Different algorithms (fixed window, sliding window, token bucket replenishment).
- Leases (concurrency capping, idempotency, expiration renewal, releases).

If `verify_backend_conformance` passes, your backend is 100% compatible with all UniLimit
framework integrations (FastAPI, Django, DRF, etc.).
