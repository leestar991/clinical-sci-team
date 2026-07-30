"""Unified Async Delay Rate Limiter for API calls."""

import asyncio
import threading
import time


class AsyncDelayRateLimiter:
    """A loop-neutral, delay-based rate limiter.

    Enforces a strict minimum interval between requests, spacing all callers
    evenly. The scheduling state is guarded by a ``threading.Lock`` (not an
    ``asyncio.Lock``) so a single process-wide instance stays correct when it is
    shared across multiple event loops — e.g. subagents run on their own event
    loops in separate threads, and an ``asyncio.Lock`` created on one loop raises
    "bound to a different event loop" when awaited from another.

    Reservation is done under the lock as pure arithmetic (no ``await``): each
    caller claims the next free slot on a shared ``time.monotonic()`` timeline,
    then sleeps until that slot *outside* the lock. This preserves the intended
    aggregate (process-wide) rate limit rather than multiplying it per loop.
    """

    def __init__(self, requests_per_second: float):
        """Initialize the rate limiter.

        Args:
            requests_per_second: The maximum number of requests allowed per second.
        """
        self.delay = 1.0 / float(requests_per_second)
        self._next_allowed = 0.0
        self._lock = threading.Lock()

    def _reserve(self) -> float:
        """Claim the next free slot on the shared timeline; return its monotonic time.

        Critical section is arithmetic-only (no await, no IO), so holding a
        threading lock briefly here does not stall the event loop.
        """
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next_allowed)
            self._next_allowed = slot + self.delay
            return slot

    async def acquire(self):
        """Acquire permission to make a request, sleeping if necessary to enforce the delay."""
        slot = self._reserve()
        sleep_time = slot - time.monotonic()
        if sleep_time > 0:
            await asyncio.sleep(sleep_time)
