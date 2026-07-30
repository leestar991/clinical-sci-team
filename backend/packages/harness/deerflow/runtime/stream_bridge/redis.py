"""Redis Streams-backed stream bridge for cross-pod event sharing.

See docs/proposal/REDIS_STREAM_BRIDGE_DESIGN.md v16 for design rationale.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import AsyncIterator
from typing import Any

import redis.exceptions

try:
    import redis.asyncio as aioredis
    from redis.exceptions import ConnectionError as RedisConnectionError
    from redis.exceptions import NoScriptError, ResponseError
    from redis.exceptions import TimeoutError as RedisTimeoutError
except ImportError as exc:  # pragma: no cover - hard import error path
    raise ImportError("RedisStreamBridge requires redis>=8.0. Install with: uv pip install redis") from exc

from .base import (
    END_SENTINEL,
    HEARTBEAT_SENTINEL,
    StreamBridge,
    StreamEvent,
    SubscriberLimitExceeded,
    TerminalExpireFailed,
)

logger = logging.getLogger(__name__)

# ─── Lua scripts ────────────────────────────────────────────────────────────
# publish: XADD + EXPIRE in a single round-trip.
# Returns [flag, id, err_msg] (plain array — NOT {err = ...} which would
# be transformed by redis-py into a ResponseError, losing the entry id).
#   flag=1 → both XADD and EXPIRE ok
#   flag=0 → XADD ok, EXPIRE failed (err_msg has details, prefix 'EXPIRE_FAILED:')
_PUBLISH_LUA = """
local id = redis.call('XADD', KEYS[1], 'MAXLEN', '~', ARGV[1], '*',
                      'event', ARGV[2], 'data', ARGV[3])
local ok, err = pcall(redis.call, 'EXPIRE', KEYS[1], ARGV[4])
if not ok then
  return {0, id, 'EXPIRE_FAILED:' .. tostring(err)}
end
return {1, id, ''}
"""

_END_EVENT = "__end__"
_REDIS_STREAM_ID_RE = re.compile(r"^\d+(?:-\d+)?$")

# Tuple of transport-level exceptions we treat as recoverable / loggable.
_TRANSPORT_EXC = (RedisConnectionError, RedisTimeoutError, ResponseError)


def _id_at_or_after(start_id: str, end_id: str) -> bool:
    """Compare two Redis Stream IDs of form ``<ms>-<seq>``.

    Returns True iff *start_id* is at or after *end_id* (i.e. the subscriber's
    cursor has already passed the END entry, so we should synthesize END
    without waiting for further XREAD events).
    """

    def _parse(s: str) -> tuple[int, int]:
        if s == "0":
            return (0, 0)
        ms, _, seq = s.partition("-")
        return (int(ms), int(seq) if seq else 0)

    return _parse(start_id) >= _parse(end_id)


def _normalize_xread_start_id(last_event_id: str | None) -> str:
    """Return a Redis XREAD-compatible start ID.

    LangGraph SDK uses ``Last-Event-ID: -1`` when joining a resumable stream
    without a concrete cursor. Redis Streams do not accept ``-1`` for XREAD,
    so treat it like an absent cursor and replay from the beginning.
    """
    if last_event_id is None:
        return "0-0"
    value = last_event_id.strip()
    if not value or value == "-1":
        return "0-0"
    if _REDIS_STREAM_ID_RE.fullmatch(value):
        return value
    logger.warning("Ignoring invalid Redis stream Last-Event-ID %r; replaying from start", last_event_id)
    return "0-0"


class RedisStreamBridge(StreamBridge):
    """Multi-pod-safe StreamBridge backed by Redis Streams.

    Subscriber admission is strict: when the per-pod semaphore is at
    capacity, ``subscribe()`` raises :class:`SubscriberLimitExceeded`
    immediately. Gateway maps this to HTTP 503 + Retry-After: 5.
    """

    def __init__(
        self,
        redis_url: str,
        *,
        namespace: str,
        queue_maxsize: int = 256,
        default_ttl: int = 3600,
        publish_max_connections: int = 16,
        subscribe_max_connections: int = 256,
    ) -> None:
        # Defense-in-depth: even if a caller bypasses the Pydantic validator
        # and constructs config directly, raise loud.
        if not namespace or not namespace.strip():
            raise ValueError("RedisStreamBridge requires a non-empty namespace to isolate stream keys; pass StreamBridgeConfig.namespace explicitly when type='redis'.")
        self._namespace = namespace.strip()
        self._queue_maxsize = queue_maxsize
        self._default_ttl = default_ttl
        self._subscribe_max_connections = subscribe_max_connections

        # Two pools: short calls vs long-polling BLOCK reads.
        self._publish_client = aioredis.from_url(
            redis_url,
            max_connections=publish_max_connections,
            decode_responses=True,
            socket_keepalive=True,
        )
        self._subscribe_client = aioredis.from_url(
            redis_url,
            max_connections=subscribe_max_connections,
            decode_responses=True,
            socket_keepalive=True,
        )

        # Application-level admission accounting.
        self._subscriber_sem = asyncio.Semaphore(subscribe_max_connections)

        # register_script auto-reloads on NOSCRIPT via redis-py 8.x.
        self._publish_script = self._publish_client.register_script(_PUBLISH_LUA)

    # ─── helpers ────────────────────────────────────────────────────────────

    def _key(self, run_id: str) -> str:
        return f"{self._namespace}:deerflow:stream:{run_id}"

    async def close(self) -> None:
        await self._publish_client.aclose()
        await self._subscribe_client.aclose()

    # ─── publish ────────────────────────────────────────────────────────────

    async def publish(self, run_id: str, event: str, data: Any) -> None:
        """Publish a non-terminal event.

        Raises:
            redis.exceptions.ConnectionError / TimeoutError: transport failure.
                Caller (worker) should swallow in finally; SSE consumer's
                heartbeat-RunStore probe will synthesize END.

        Notes:
            EXPIRE-failed-but-XADD-succeeded: log warning, do NOT raise.
            The next publish will reset TTL.
        """
        key = self._key(run_id)
        payload = json.dumps(data, ensure_ascii=False, default=str)
        flag, entry_id, err_msg = await self._publish_script(
            keys=[key],
            args=[self._queue_maxsize, event, payload, self._default_ttl],
        )
        if int(flag) == 0:
            logger.warning(
                "publish kept entry %s but EXPIRE failed (run=%s): %s",
                entry_id,
                run_id,
                err_msg,
            )

    async def publish_end(self, run_id: str) -> None:
        """Publish the END entry signalling no more events.

        Raises:
            TerminalExpireFailed: XADD ok but EXPIRE failed. Caller MUST trigger
                bridge.cleanup(delay=300, escalate_on_failure=True).
            redis.exceptions.ConnectionError / TimeoutError: transport failure.
        """
        key = self._key(run_id)
        flag, entry_id, err_msg = await self._publish_script(
            keys=[key],
            args=[self._queue_maxsize, _END_EVENT, "", self._default_ttl],
        )
        if int(flag) == 0:
            raise TerminalExpireFailed(
                f"publish_end XADD succeeded but EXPIRE failed: {err_msg}",
                entry_id=entry_id,
            )

    # ─── cleanup ────────────────────────────────────────────────────────────

    async def cleanup(
        self,
        run_id: str,
        *,
        delay: float = 0,
        escalate_on_failure: bool = False,
    ) -> None:
        """Release stream key for *run_id*.

        Args:
            delay: 0 → UNLINK immediately (non-blocking DEL);
                   N > 0 → EXPIRE key by N seconds.
            escalate_on_failure: when True, EXPIRE failure falls back to UNLINK;
                UNLINK failure emits the ``redis_stream_bridge.terminal_leak``
                metric + log CRITICAL but does NOT raise.

        Notes:
            delay=0 uses UNLINK (not DEL) to avoid blocking the Redis main
            thread on large streams.
        """
        key = self._key(run_id)

        if delay <= 0:
            try:
                await self._publish_client.unlink(key)
            except _TRANSPORT_EXC as exc:
                if escalate_on_failure:
                    self._emit_terminal_leak(run_id, str(exc))
                    return
                raise
            return

        # delay > 0 path
        try:
            await self._publish_client.expire(key, int(delay))
            return
        except _TRANSPORT_EXC as expire_exc:
            if not escalate_on_failure:
                raise
            logger.warning(
                "cleanup EXPIRE failed for run %s, falling back to UNLINK: %s",
                run_id,
                expire_exc,
            )

        # Escalation: EXPIRE failed, try UNLINK
        try:
            await self._publish_client.unlink(key)
        except _TRANSPORT_EXC as unlink_exc:
            self._emit_terminal_leak(run_id, str(unlink_exc))

    def _emit_terminal_leak(self, run_id: str, reason: str) -> None:
        """Emit metric + log CRITICAL; do NOT raise."""
        logger.critical(
            "redis_stream_bridge.terminal_leak: run=%s key=%s reason=%s",
            run_id,
            self._key(run_id),
            reason,
        )
        # TODO: wire to metrics provider when one exists (design §10.2 follow-up #2)

    # ─── subscribe ──────────────────────────────────────────────────────────

    async def subscribe(
        self,
        run_id: str,
        *,
        last_event_id: str | None = None,
        heartbeat_interval: float = 15.0,
    ) -> AsyncIterator[StreamEvent]:
        """Subscribe to *run_id*'s event stream.

        Strict admission: when the per-pod semaphore is at capacity,
        raises :class:`SubscriberLimitExceeded` immediately (NO bounded wait).
        Gateway maps this to HTTP 503 + Retry-After: 5.

        Three invariants (must hold under all paths):
            1. Reject path leaves the semaphore untouched.
            2. Success path's finally ALWAYS releases (covers cancel/close/raise).
            3. Never over-release: 0 <= sem._value <= subscribe_max_connections.

        T8 prime-heartbeat contract: immediately after admission succeeds, this
        generator yields a synthetic ``HEARTBEAT_SENTINEL`` as its first event
        (before any XREAD BLOCK call) so route-layer ``subscribe_with_prime``
        can return ``StreamingResponse`` headers without waiting up to
        ``heartbeat_interval`` for idle runs.

        Raises:
            SubscriberLimitExceeded: sem.locked() at entry.
            redis.ConnectionError / TimeoutError: transport failure (mapped to
                HTTP 502/504 by Gateway, distinct from 503 capacity).
        """
        # ─── strict admission (3 lines, provably race-free) ─────────────────
        if self._subscriber_sem.locked():
            raise SubscriberLimitExceeded(f"Subscriber capacity exhausted ({self._subscribe_max_connections})")
        await self._subscriber_sem.acquire()  # value > 0, does NOT actually await
        try:
            # ─── PRIME HEARTBEAT（T8 contract / codex round 17 finding 3）──
            # Immediately yield a synthetic heartbeat so callers (T11
            # subscribe_with_prime) can return HTTP response headers without
            # waiting for XREAD BLOCK to time out (~15s default).
            yield HEARTBEAT_SENTINEL
            # ─── 正常 xread loop ────────────────────────────────────────────
            async for entry in self._xread_loop(run_id, last_event_id, heartbeat_interval):
                yield entry
        finally:
            self._subscriber_sem.release()

    async def _xread_loop(
        self,
        run_id: str,
        last_event_id: str | None,
        heartbeat_interval: float,
    ) -> AsyncIterator[StreamEvent]:
        """The actual XREAD BLOCK loop. Extracted so :meth:`subscribe`'s
        ``try/finally`` wraps everything including initial peek and exception
        paths.
        """
        key = self._key(run_id)
        start_id = _normalize_xread_start_id(last_event_id)
        block_ms = max(1, int(heartbeat_interval * 1000))

        while True:
            try:
                result = await self._subscribe_client.xread(
                    {key: start_id},
                    count=128,
                    block=block_ms,
                )
            except redis.exceptions.TimeoutError:
                # Client-side socket_timeout can fire during a legitimate
                # xread block=Nms window (redis-py async client default,
                # or TCP keepalive misfire). Treat this identically to a
                # block-level empty poll so the SSE stream stays connected
                # instead of tearing down and forcing the frontend to
                # reconnect (or worse, silently stall as EventSource does
                # not retry after a 500-class error). Verified against a
                # real Redis instance that xread(block=Y) itself never
                # raises TimeoutError; the error is always socket-layer.
                logger.debug(
                    "xread socket-level TimeoutError during block=%dms; treating as empty poll",
                    block_ms,
                )
                result = None
            if not result:
                # Block timeout: check if END is already in the stream past last_id
                end_id = await self._peek_end_id(key)
                if end_id is not None and _id_at_or_after(start_id, end_id):
                    yield END_SENTINEL
                    return
                yield HEARTBEAT_SENTINEL
                continue

            # result format: [(key, [(entry_id, {field: value})])]
            _, entries = result[0]
            for entry_id, fields in entries:
                if fields.get("event") == _END_EVENT:
                    yield END_SENTINEL
                    return
                data_raw = fields.get("data", "")
                try:
                    data = json.loads(data_raw) if data_raw else None
                except json.JSONDecodeError:
                    data = data_raw  # tolerate non-JSON payloads
                yield StreamEvent(
                    id=entry_id,
                    event=fields.get("event", ""),
                    data=data,
                )
                start_id = entry_id

    async def _peek_end_id(self, key: str) -> str | None:
        """Return the entry id of the last ``__end__`` entry, or ``None`` if
        no END has been published yet. Uses XREVRANGE COUNT 1 (single
        round-trip).
        """
        entries = await self._publish_client.xrevrange(key, count=1)
        if not entries:
            return None
        entry_id, fields = entries[0]
        if fields.get("event") == _END_EVENT:
            return entry_id
        return None


__all__ = [
    "RedisStreamBridge",
    # Surfacing redis exception types lets call sites import the canonical
    # transport-exception tuple without taking a hard redis-py dependency
    # at their own import time.
    "NoScriptError",
]
