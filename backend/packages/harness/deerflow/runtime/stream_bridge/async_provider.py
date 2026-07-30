"""Async stream bridge factory.

Provides an **async context manager** aligned with
:func:`deerflow.runtime.checkpointer.async_provider.make_checkpointer`.

Usage (e.g. FastAPI lifespan)::

    from deerflow.agents.stream_bridge import make_stream_bridge

    async with make_stream_bridge() as bridge:
        app.state.stream_bridge = bridge
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator

from deerflow.config.app_config import AppConfig
from deerflow.config.stream_bridge_config import get_stream_bridge_config

from .base import StreamBridge

logger = logging.getLogger(__name__)


@contextlib.asynccontextmanager
async def make_stream_bridge(app_config: AppConfig | None = None) -> AsyncIterator[StreamBridge]:
    """Async context manager that yields a :class:`StreamBridge`.

    Falls back to :class:`MemoryStreamBridge` when no configuration is
    provided and nothing is set globally.

    When ``stream_bridge.type == "redis"``, constructs a
    :class:`RedisStreamBridge` with strict admission. Defense-in-depth
    validation here covers callers that bypass the Pydantic validator (e.g.
    test fixtures using ``model_construct``).
    """
    if app_config is None:
        config = get_stream_bridge_config()
    else:
        config = app_config.stream_bridge

    if config is None or config.type == "memory":
        from deerflow.runtime.stream_bridge.memory import MemoryStreamBridge

        maxsize = config.queue_maxsize if config is not None else 256
        bridge = MemoryStreamBridge(queue_maxsize=maxsize)
        logger.info("Stream bridge initialised: memory (queue_maxsize=%d)", maxsize)
        try:
            yield bridge
        finally:
            await bridge.close()
        return

    if config.type == "redis":
        # Defense-in-depth: Pydantic validator already enforces this, but
        # raise here too in case a caller bypassed config loading.
        if not config.namespace or not config.namespace.strip():
            raise ValueError(f"stream_bridge.type='redis' requires a non-empty namespace; got namespace={config.namespace!r}")
        if not config.redis_url:
            raise ValueError("stream_bridge.type='redis' requires redis_url to be set")
        # Lazy import so memory-only deployments don't need redis-py at import time.
        from deerflow.runtime.stream_bridge.redis import RedisStreamBridge

        bridge = RedisStreamBridge(
            config.redis_url,
            namespace=config.namespace,
            queue_maxsize=config.queue_maxsize,
            publish_max_connections=config.publish_max_connections,
            subscribe_max_connections=config.subscribe_max_connections,
        )
        logger.info(
            "Stream bridge initialised: redis (namespace=%s, queue_maxsize=%d, publish_pool=%d, subscribe_pool=%d)",
            config.namespace,
            config.queue_maxsize,
            config.publish_max_connections,
            config.subscribe_max_connections,
        )
        try:
            yield bridge
        finally:
            await bridge.close()
        return

    raise ValueError(f"Unknown stream bridge type: {config.type!r}")
