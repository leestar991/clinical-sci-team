"""Configuration for stream bridge."""

from typing import Literal, Self

from pydantic import BaseModel, Field, model_validator

StreamBridgeType = Literal["memory", "redis"]


class StreamBridgeConfig(BaseModel):
    """Configuration for the stream bridge that connects agent workers to SSE endpoints."""

    type: StreamBridgeType = Field(
        default="memory",
        description=("Stream bridge backend type. 'memory' uses in-process asyncio.Queue (single-process only). 'redis' uses Redis Streams for cross-pod shared event delivery; requires namespace to be set."),
    )
    redis_url: str | None = Field(
        default=None,
        description="Redis URL when type='redis'. Example: 'redis://localhost:6379/0'.",
    )
    namespace: str | None = Field(
        default=None,
        description=(
            "REQUIRED when type='redis'; ignored otherwise. Used as the key "
            "namespace for stream isolation across tenants/environments. "
            "Stream key derives to '{namespace}:deerflow:stream:{run_id}'. "
            "Examples: 'prod-cellflow', 'staging-team-a'. Empty/whitespace "
            "values are rejected at config-load time."
        ),
    )
    queue_maxsize: int = Field(
        default=256,
        description="Approximate per-run buffer size (XADD MAXLEN ~ N).",
    )
    publish_max_connections: int = Field(
        default=16,
        description=("Redis connection pool size for XADD/EVALSHA (short calls). Sizing: >= max(2, peak_concurrent_workers_per_pod / 2)."),
    )
    subscribe_max_connections: int = Field(
        default=256,
        description=(
            "Redis connection pool size for XREAD BLOCK (each subscriber holds one connection for the SSE duration). Strict admission: requests beyond this cap return HTTP 503 + Retry-After: 5. Sizing: >= peak_concurrent_sse_per_pod * 1.2."
        ),
    )

    @model_validator(mode="after")
    def _validate_redis_namespace(self) -> Self:
        if self.type == "redis":
            if self.namespace is None or not self.namespace.strip():
                raise ValueError("stream_bridge.namespace is required when type='redis' and must be a non-empty string. Set a value like 'prod-cellflow' to isolate stream keys.")
        return self


# Global configuration instance — None means no stream bridge is configured
# (falls back to memory with defaults).
_stream_bridge_config: StreamBridgeConfig | None = None


def get_stream_bridge_config() -> StreamBridgeConfig | None:
    """Get the current stream bridge configuration, or None if not configured."""
    return _stream_bridge_config


def set_stream_bridge_config(config: StreamBridgeConfig | None) -> None:
    """Set the stream bridge configuration."""
    global _stream_bridge_config
    _stream_bridge_config = config


def load_stream_bridge_config_from_dict(config_dict: dict | None) -> None:
    """Load stream bridge configuration from a dictionary."""
    global _stream_bridge_config
    if config_dict is None:
        _stream_bridge_config = None
        return
    _stream_bridge_config = StreamBridgeConfig(**config_dict)
