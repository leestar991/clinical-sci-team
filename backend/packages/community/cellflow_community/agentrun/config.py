"""AgentRun configuration — loaded from environment variables and config.yaml."""

from __future__ import annotations

import os


class AgentRunConfig:
    """AgentRun sandbox provider configuration.

    Credentials and resource identifiers come from environment variables.
    OSS mount settings are read from config.yaml storage section.
    """

    def __init__(
        self,
        *,
        access_key: str = "",
        secret_key: str = "",
        account_id: str = "",
        region: str = "cn-hangzhou",
        template_name: str = "deerflow-aio",
        template_type: str = "AIO",
        idle_timeout: int = 600,
        command_timeout: int = 300,
        oss_enabled: bool = False,
        oss_bucket: str = "",
        oss_endpoint: str = "",
        oss_prefix: str = "cellflow",
        registry_redis_url: str | None = None,
        registry_key_prefix: str | None = None,
    ) -> None:
        self.access_key = access_key
        self.secret_key = secret_key
        self.account_id = account_id
        self.region = region
        self.template_name = template_name
        self.template_type = template_type
        self.idle_timeout = idle_timeout
        self.command_timeout = command_timeout
        self.oss_enabled = oss_enabled
        self.oss_bucket = oss_bucket
        self.oss_endpoint = oss_endpoint
        self.oss_prefix = oss_prefix
        self.registry_redis_url = registry_redis_url
        self.registry_key_prefix = registry_key_prefix

    @classmethod
    def from_env(cls) -> AgentRunConfig:
        """Load from environment variables and config.yaml."""
        from deerflow.config.app_config import get_app_config

        config = get_app_config()
        storage = config.storage

        return cls(
            access_key=os.getenv("AGENTRUN_ACCESS_KEY_ID", ""),
            secret_key=os.getenv("AGENTRUN_ACCESS_KEY_SECRET", ""),
            account_id=os.getenv("AGENTRUN_ACCOUNT_ID", ""),
            region=os.getenv("AGENTRUN_REGION", "cn-hangzhou"),
            template_name=os.getenv("AGENTRUN_TEMPLATE_NAME", "deerflow-aio"),
            template_type=os.getenv("AGENTRUN_TEMPLATE_TYPE", "AIO"),
            idle_timeout=int(os.getenv("AGENTRUN_IDLE_TIMEOUT", "600")),
            command_timeout=int(os.getenv("AGENTRUN_COMMAND_TIMEOUT", "300")),
            oss_enabled=storage.backend == "oss",
            oss_bucket=storage.oss_bucket,
            oss_endpoint=os.getenv("AGENTRUN_OSS_INTERNAL_ENDPOINT", storage.oss_endpoint),
            oss_prefix=storage.oss_prefix,
            registry_redis_url=os.getenv("AGENTRUN_REGISTRY_REDIS_URL"),
            registry_key_prefix=os.getenv("AGENTRUN_REGISTRY_KEY_PREFIX"),
        )
