"""Redis backend. Requires the ``redis`` extra: ``pip install "unilimit[redis]"``."""

try:
    import redis as _redis  # noqa: F401
except ImportError as exc:  # pragma: no cover - exercised only without the extra
    raise ImportError(
        'The Redis backend requires redis-py. Install it with: pip install "unilimit[redis]"'
    ) from exc

from unilimit.backends.redis._protocol import decode_state, encode_state
from unilimit.backends.redis.backend import RedisBackend

__all__ = ["RedisBackend", "decode_state", "encode_state"]
