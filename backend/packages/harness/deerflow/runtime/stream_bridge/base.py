"""Abstract stream bridge protocol.

StreamBridge decouples agent workers (producers) from SSE endpoints
(consumers), aligning with LangGraph Platform's Queue + StreamManager
architecture.
"""

from __future__ import annotations

import abc
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class StreamEvent:
    """Single stream event.

    Attributes:
        id: Monotonically increasing event ID (used as SSE ``id:`` field,
            supports ``Last-Event-ID`` reconnection).
        event: SSE event name, e.g. ``"metadata"``, ``"updates"``,
            ``"events"``, ``"error"``, ``"end"``.
        data: JSON-serialisable payload.
    """

    id: str
    event: str
    data: Any


HEARTBEAT_SENTINEL = StreamEvent(id="", event="__heartbeat__", data=None)
END_SENTINEL = StreamEvent(id="", event="__end__", data=None)


class StreamBridge(abc.ABC):
    """Abstract base for stream bridges."""

    @abc.abstractmethod
    async def publish(self, run_id: str, event: str, data: Any) -> None:
        """Enqueue a single event for *run_id* (producer side)."""

    @abc.abstractmethod
    async def publish_end(self, run_id: str) -> None:
        """Signal that no more events will be produced for *run_id*."""

    @abc.abstractmethod
    def subscribe(
        self,
        run_id: str,
        *,
        last_event_id: str | None = None,
        heartbeat_interval: float = 15.0,
    ) -> AsyncIterator[StreamEvent]:
        """Async iterator that yields events for *run_id* (consumer side).

        Yields :data:`HEARTBEAT_SENTINEL` when no event arrives within
        *heartbeat_interval* seconds.  Yields :data:`END_SENTINEL` once
        the producer calls :meth:`publish_end`.
        """

    @abc.abstractmethod
    async def cleanup(
        self,
        run_id: str,
        *,
        delay: float = 0,
        escalate_on_failure: bool = False,
    ) -> None:
        """Release resources associated with *run_id*.

        If *delay* > 0 the implementation should wait before releasing,
        giving late subscribers a chance to drain remaining events.

        *escalate_on_failure* is meaningful only for backend implementations
        that can fail at the wire level (e.g. Redis). When True, the
        implementation should escalate (EXPIRE → UNLINK → terminal_leak
        metric) and never raise. MemoryStreamBridge ignores this flag.
        """

    async def close(self) -> None:
        """Release backend resources.  Default is a no-op."""


class SubscriberLimitExceeded(Exception):
    """Raised by RedisStreamBridge.subscribe() when the per-pod subscriber
    semaphore is at capacity. Gateway maps this to HTTP 503 + Retry-After: 5.
    MemoryStreamBridge never raises this.
    """


class TerminalExpireFailed(Exception):
    """Raised by RedisStreamBridge.publish_end() when the XADD succeeded but
    EXPIRE failed (e.g. Redis OOM-evict). Caller MUST trigger
    bridge.cleanup(delay=300, escalate_on_failure=True) to close the leak.
    MemoryStreamBridge never raises this.

    Attributes:
        entry_id: The Redis stream entry id of the __end__ entry that was
            still written despite EXPIRE failure.
    """

    def __init__(self, message: str, *, entry_id: str | None = None) -> None:
        super().__init__(message)
        self.entry_id = entry_id
