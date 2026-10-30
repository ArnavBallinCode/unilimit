"""Framework-independent observability: structured events and observers.

An *observer* is a synchronous callable that receives one event per notable occurrence::

    def observe(event: Event) -> None:
        print(event.to_dict())

    limiter = Limiter.from_url("redis://localhost", observer=observe)

Guarantees
----------
- **An observer can never change an enforcement decision.** Events are emitted after
  the decision is made. An exception raised by an observer (any ``Exception``) is
  caught and logged (throttled, category ``observer_error``); the request continues
  exactly as if the observer had not been called.
- **Observers run inline** on the request path, in the calling thread (or event loop).
  They must be fast and must not block; hand slow work to a queue.
- **Nothing is built without an observer.** With none configured the cost per decision
  is a single truthiness check.
- **No sensitive data.** Events carry rule names and algorithms (which you configured),
  backend kind, outcomes and timings only: never identities, IP addresses, storage keys,
  paths, headers or error messages (exception *class names* only).

Schema
------
Every event has ``name`` (stable, see below), ``timestamp`` (Unix seconds) and the
fields documented on its class. :meth:`to_dict` returns a flat JSON-serialisable
mapping with the keys ``event``, ``schema`` (:data:`SCHEMA_VERSION`) and the fields.
Adding a field bumps nothing; removing or renaming one bumps ``SCHEMA_VERSION``.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar, Literal

from unilimit._logging import throttled

SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class RuleOutcome:
    """One rule's part in a decision."""

    name: str
    algorithm: str
    allowed: bool
    enforced: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "algorithm": self.algorithm,
            "allowed": self.allowed,
            "enforced": self.enforced,
        }


@dataclass(frozen=True, slots=True)
class DecisionEvent:
    """A request (or a single-rule ``consume``) was decided. ``name == "decision"``.

    ``reason`` is a :class:`~unilimit.Reason` value. ``source`` says who produced the
    figures: ``"backend"`` (Redis or memory, the normal case), ``"fallback"`` (the
    process-local fallback while the backend was unavailable) or ``"policy"`` (decided
    before any backend call: exempt, denied, missing identity) or ``"degraded"`` (the
    backend was unavailable and ``fail_mode`` decided). ``fail_mode`` is set only when
    ``source == "degraded"``. ``duration`` is the backend call in seconds, ``None`` when
    there was none.
    """

    name: ClassVar[str] = "decision"
    timestamp: float
    allowed: bool
    reason: str
    source: Literal["backend", "fallback", "policy", "degraded"]
    backend: str
    rules: tuple[RuleOutcome, ...]
    binding_rule: str | None
    fail_mode: Literal["open", "closed"] | None
    partial_commit: bool
    duration: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "event": self.name,
            "schema": SCHEMA_VERSION,
            "timestamp": self.timestamp,
            "allowed": self.allowed,
            "reason": self.reason,
            "source": self.source,
            "backend": self.backend,
            "rules": [r.to_dict() for r in self.rules],
            "binding_rule": self.binding_rule,
            "fail_mode": self.fail_mode,
            "partial_commit": self.partial_commit,
            "duration": self.duration,
        }


@dataclass(frozen=True, slots=True)
class BackendUnavailableEvent:
    """A backend call failed with ``BackendUnavailable``. ``name == "backend_unavailable"``.

    ``error`` is the exception class name of the root cause, ``duration`` the time the
    failed call took in seconds (near zero while the circuit breaker is open).
    """

    name: ClassVar[str] = "backend_unavailable"
    timestamp: float
    backend: str
    operation: Literal["consume", "peek"]
    error: str
    partial_commit: bool
    duration: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "event": self.name,
            "schema": SCHEMA_VERSION,
            "timestamp": self.timestamp,
            "backend": self.backend,
            "operation": self.operation,
            "error": self.error,
            "partial_commit": self.partial_commit,
            "duration": self.duration,
        }


@dataclass(frozen=True, slots=True)
class BreakerEvent:
    """The Redis circuit breaker changed state. ``name == "breaker"``.

    ``state`` is ``"open"`` (Redis failed; calls fail fast for ``cooldown`` seconds),
    ``"half_open"`` (one probe call is going to Redis) or ``"closed"`` (the probe
    succeeded: Redis recovered). ``error`` is the failure's class name for ``"open"``.
    """

    name: ClassVar[str] = "breaker"
    timestamp: float
    backend: str
    state: Literal["open", "half_open", "closed"]
    error: str | None
    cooldown: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "event": self.name,
            "schema": SCHEMA_VERSION,
            "timestamp": self.timestamp,
            "backend": self.backend,
            "state": self.state,
            "error": self.error,
            "cooldown": self.cooldown,
        }


@dataclass(frozen=True, slots=True)
class FallbackEvent:
    """The process-local fallback started or stopped serving. ``name == "fallback"``.

    ``decisions`` is the number of decisions the fallback served in the period that is
    ending (``0`` for ``"activated"``).
    """

    name: ClassVar[str] = "fallback"
    timestamp: float
    backend: str
    state: Literal["activated", "deactivated"]
    decisions: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "event": self.name,
            "schema": SCHEMA_VERSION,
            "timestamp": self.timestamp,
            "backend": self.backend,
            "state": self.state,
            "decisions": self.decisions,
        }


Event = DecisionEvent | BackendUnavailableEvent | BreakerEvent | FallbackEvent
EVENT_NAMES = (
    DecisionEvent.name,
    BackendUnavailableEvent.name,
    BreakerEvent.name,
    FallbackEvent.name,
)
Observer = Callable[[Event], None]


def now() -> float:
    return time.time()


class Emitter:
    """Delivers events to observers. Cheap when there are none; never raises.

    Callers check :attr:`enabled` before doing any work for an event, and pass a
    factory so that nothing is built without an observer.
    """

    __slots__ = ("_observers",)

    def __init__(self, observers: Sequence[Observer] = ()) -> None:
        self._observers: tuple[Observer, ...] = tuple(observers)

    @property
    def enabled(self) -> bool:
        return bool(self._observers)

    def add(self, observer: Observer) -> None:
        self._observers = (*self._observers, observer)  # copy on write: lock-free readers

    def remove(self, observer: Observer) -> None:
        self._observers = tuple(o for o in self._observers if o is not observer)

    def emit(self, make: Callable[[], Event]) -> None:
        observers = self._observers
        if not observers:
            return
        try:
            event = make()
        except Exception as exc:  # a bug here must not touch enforcement
            throttled.log(
                logging.ERROR, "observer_error", "building an event failed: %s", type(exc).__name__
            )
            return
        for observer in observers:
            try:
                observer(event)
            except Exception as exc:
                throttled.log(
                    logging.WARNING,
                    "observer_error",
                    "an observer raised %s while handling a %r event; it was ignored",
                    type(exc).__name__,
                    event.name,
                )


def normalise_observers(observer: Observer | Sequence[Observer] | None) -> tuple[Observer, ...]:
    if observer is None:
        return ()
    if callable(observer):
        return (observer,)
    return tuple(observer)
