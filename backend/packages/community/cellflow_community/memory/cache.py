"""Write-through memory cache backed by StorageBackend.

ETag-based change detection, LRU eviction, TTL expiry, and clock injection
for deterministic testing.
"""

import asyncio
import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

from deerflow.storage.base import StorageBackend

logger = logging.getLogger(__name__)


class MemoryCache:
    """Write-through cache for memory JSON stored in a StorageBackend.

    - Write-through: write to backend (get ETag) → update local cache
    - Read: cache hit → head backend for ETag → match = use cache; mismatch = re-read
    - LRU eviction with configurable max_entries
    - TTL expiry with configurable ttl_seconds
    - Clock injection for deterministic tests
    """

    def __init__(
        self,
        backend: StorageBackend,
        *,
        max_entries: int = 1000,
        ttl_seconds: float = 60.0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._backend = backend
        self._max_entries = max_entries
        self._ttl_seconds = ttl_seconds
        self._clock = clock or time.monotonic

        # OrderedDict for LRU: most recently used at the end
        self._entries: OrderedDict[str, tuple[dict[str, Any], str, float]] = OrderedDict()
        # cache_key → (data, etag, cached_at)
        self._lock = threading.Lock()

    # -- public async API -----------------------------------------------------

    async def read(self, key: str) -> dict[str, Any]:
        """Read memory data for *key*, using cache when possible."""
        with self._lock:
            entry = self._entries.get(key)

        if entry is not None:
            data, cached_etag, cached_at = entry
            age = self._clock() - cached_at
            if age < self._ttl_seconds:
                # Check if backend has newer version
                try:
                    head = await self._backend.head(key)
                    if head.etag == cached_etag:
                        self._touch(key)
                        return data
                except Exception:
                    pass  # head failed → fall through to full read

        return await self._read_through(key)

    async def write(self, key: str, data: dict[str, Any]) -> None:
        """Write *data* to backend, then update local cache."""
        import json

        raw = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        etag = await self._backend.write(key, raw)
        self._cache_put(key, data, etag)

    async def reload(self, key: str) -> dict[str, Any]:
        """Force re-read from backend, updating cache."""
        with self._lock:
            self._entries.pop(key, None)
        return await self._read_through(key)

    # -- concurrency ----------------------------------------------------------

    async def concurrent_read(self, key: str, concurrency: int) -> list[dict[str, Any]]:
        """Execute *concurrency* concurrent reads for the same key.

        The design calls for N concurrent reads → N backend.read calls
        (no singleflight). Gate-based tests control entry timing via
        FakeStorageBackend gates or Semaphore patterns.
        """
        tasks = [self.read(key) for _ in range(concurrency)]
        return await asyncio.gather(*tasks)

    # -- internals ------------------------------------------------------------

    async def _read_through(self, key: str) -> dict[str, Any]:
        import json

        raw = await self._backend.read(key)
        data = json.loads(raw)
        head = await self._backend.head(key)
        self._cache_put(key, data, head.etag)
        return data

    def _cache_put(self, key: str, data: dict[str, Any], etag: str) -> None:
        with self._lock:
            # LRU: remove then re-insert to put at end
            self._entries.pop(key, None)
            self._entries[key] = (data, etag, self._clock())
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)

    def _touch(self, key: str) -> None:
        """Move *key* to the end (most recently used)."""
        with self._lock:
            entry = self._entries.pop(key, None)
            if entry is not None:
                self._entries[key] = entry

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._entries)
