"""Storage backends. ``MemoryBackend`` is always available; Redis arrives in Milestone 3."""

from unilimit.backends.base import (
    AsyncBackend,
    Backend,
    BatchResult,
    ConsumeOp,
    OpResult,
    SyncBackend,
)
from unilimit.backends.memory import MemoryBackend

__all__ = [
    "AsyncBackend",
    "Backend",
    "BatchResult",
    "ConsumeOp",
    "MemoryBackend",
    "OpResult",
    "SyncBackend",
]
