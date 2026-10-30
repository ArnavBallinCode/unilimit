"""Library logging: the ``unilimit`` logger and throttled failure logging.

An outage affects every request, so logging every affected request would flood the logs
and slow the application down. :class:`ThrottledLogger` logs the first occurrence of
each *category* immediately, suppresses repeats for ``interval`` seconds and reports how
many were suppressed. Suppression is **process-local**: every worker process logs its
own first occurrence and its own summaries; there is deliberately no shared state.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

logger = logging.getLogger("unilimit")
logger.addHandler(logging.NullHandler())

DEFAULT_INTERVAL = 30.0
_MAX_CATEGORIES = 256
_OVERFLOW = "other"


@dataclass
class _State:
    window_start: float
    suppressed: int = 0
    level: int = logging.WARNING
    last: str = ""


class ThrottledLogger:
    """Rate-limits log records per category. Thread-safe; never raises.

    Categories are chosen by the caller and must be bounded (failure kinds, configured
    rule names), never request data. Beyond 256 categories new ones share ``"other"``.
    ``interval=0`` disables throttling.
    """

    def __init__(
        self,
        target: logging.Logger,
        interval: float = DEFAULT_INTERVAL,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._target = target
        self._clock = clock
        self._lock = threading.Lock()
        self._states: dict[str, _State] = {}
        self._interval = 0.0
        self.configure(interval)

    @property
    def interval(self) -> float:
        return self._interval

    def configure(self, interval: float) -> None:
        if isinstance(interval, bool) or not isinstance(interval, int | float) or interval < 0:
            raise ValueError("the logging interval must be a non-negative number of seconds")
        self._interval = float(interval)

    def reset(self) -> None:
        """Forget all categories (tests)."""
        with self._lock:
            self._states.clear()

    def log(self, level: int, category: str, message: str, *args: object) -> None:
        """Log ``message % args`` unless ``category`` logged within the interval."""
        # Logging must never affect an enforcement decision.
        with contextlib.suppress(Exception):
            self._log(level, category, message, args)

    def _log(self, level: int, category: str, message: str, args: tuple[object, ...]) -> None:
        interval = self._interval
        summary = 0
        if interval > 0:
            now = self._clock()
            with self._lock:
                state = self._states.get(category)
                if state is None and len(self._states) >= _MAX_CATEGORIES:
                    category = _OVERFLOW
                    state = self._states.get(category)
                if state is None:
                    self._states[category] = _State(now, 0, level, message % args)
                elif now - state.window_start >= interval:
                    summary = state.suppressed
                    state.window_start, state.suppressed = now, 0
                    state.level, state.last = level, message % args
                else:
                    state.suppressed += 1
                    state.level, state.last = level, message % args
                    return
        if summary:
            self._target.log(
                level,
                message + " (%d similar messages suppressed in the previous %.0fs)",
                *args,
                summary,
                interval,
            )
        else:
            self._target.log(level, message, *args)

    def flush(self) -> None:
        """Report suppressed messages now (on recovery and shutdown), so that a burst
        that ends is never left unaccounted for."""
        with contextlib.suppress(Exception):
            with self._lock:
                pending = [(c, s.level, s.last, s.suppressed) for c, s in self._states.items()]
                for state in self._states.values():
                    state.suppressed = 0
            for category, level, last, count in pending:
                if count:
                    self._target.log(
                        level, "%d similar %r messages suppressed; last: %s", count, category, last
                    )


throttled = ThrottledLogger(logger)


def configure_logging(*, interval: float = DEFAULT_INTERVAL) -> None:
    """Set how long repeated failure messages of one category are suppressed (seconds,
    process-wide; ``0`` logs every occurrence)."""
    throttled.configure(interval)
