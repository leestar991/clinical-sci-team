"""SandboxRegistry — shared (user_id, thread_id) → sandbox_id mapping."""

from __future__ import annotations

import logging
import threading
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)


class SandboxRegistry(ABC):
    """Abstract registry for sandbox mapping storage."""

    @staticmethod
    def _make_key(user_id: str, thread_id: str) -> str:
        return f"{user_id}::{thread_id}"

    @abstractmethod
    def get(self, user_id: str, thread_id: str) -> str | None:
        """Return sandbox_id for the given user+thread, or None."""

    @abstractmethod
    def set(self, user_id: str, thread_id: str, sandbox_id: str, ttl: int | None = None) -> None:
        """Store mapping with optional TTL (seconds)."""

    @abstractmethod
    def delete(self, user_id: str, thread_id: str) -> None:
        """Remove mapping."""

    @abstractmethod
    def clear(self) -> None:
        """Remove all mappings."""


class MemorySandboxRegistry(SandboxRegistry):
    """In-process memory registry for single-instance deployments."""

    def __init__(self) -> None:
        self._store: dict[str, str] = {}
        self._lock = threading.Lock()

    def get(self, user_id: str, thread_id: str) -> str | None:
        with self._lock:
            return self._store.get(self._make_key(user_id, thread_id))

    def set(self, user_id: str, thread_id: str, sandbox_id: str, ttl: int | None = None) -> None:
        with self._lock:
            self._store[self._make_key(user_id, thread_id)] = sandbox_id

    def delete(self, user_id: str, thread_id: str) -> None:
        with self._lock:
            self._store.pop(self._make_key(user_id, thread_id), None)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()


class RedisSandboxRegistry(SandboxRegistry):
    """Redis-backed registry for multi-instance deployments."""

    KEY_PREFIX = "agentrun:sandbox:"

    def __init__(self, redis_url: str, key_prefix: str | None = None) -> None:
        import redis

        self._client = redis.Redis.from_url(
            redis_url,
            decode_responses=True,
            socket_timeout=3,
            max_connections=10,
        )
        self._prefix = key_prefix or self.KEY_PREFIX

    def _full_key(self, user_id: str, thread_id: str) -> str:
        return f"{self._prefix}{self._make_key(user_id, thread_id)}"

    def get(self, user_id: str, thread_id: str) -> str | None:
        return self._client.get(self._full_key(user_id, thread_id))

    def set(self, user_id: str, thread_id: str, sandbox_id: str, ttl: int | None = None) -> None:
        key = self._full_key(user_id, thread_id)
        if ttl:
            self._client.setex(key, ttl, sandbox_id)
        else:
            self._client.set(key, sandbox_id)

    def delete(self, user_id: str, thread_id: str) -> None:
        self._client.delete(self._full_key(user_id, thread_id))

    def clear(self) -> None:
        cursor = 0
        while True:
            cursor, keys = self._client.scan(cursor, match=f"{self._prefix}*", count=100)
            if keys:
                self._client.delete(*keys)
            if cursor == 0:
                break
