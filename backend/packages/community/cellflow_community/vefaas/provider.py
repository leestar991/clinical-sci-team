"""VeFaasSandboxProvider — 继承 DeerFlow AioSandboxProvider 替换 Backend 与 Sandbox 类。

只重写两个方法：
- _create_backend()：返回 VeFaasSandboxBackend 而不是 LocalContainerBackend
- _create_sandbox(thread_id, sandbox_id)：用 VeFaasAioSandbox 代替 AioSandbox，
  并用 backend 自己的 wait_ready（带 header）代替 wait_for_sandbox_ready（不带 header）

其他一切（warm pool / idle checker / replicas / 跨进程锁）全部继承父类。
"""

from __future__ import annotations

import logging
import time

from cellflow_community.aio_sandbox.aio_sandbox_provider import (
    DEFAULT_REPLICAS,
    AioSandboxProvider,
)
from cellflow_community.aio_sandbox.backend import SandboxBackend

from .backend import VeFaasSandboxBackend
from .config import VeFaasConfig
from .sandbox import VeFaasAioSandbox

logger = logging.getLogger(__name__)


class VeFaasSandboxProvider(AioSandboxProvider):
    """接入火山引擎 veFaaS 的 SandboxProvider。

    加载链路：config.yaml 写
        sandbox:
          use: "cellflow_community.vefaas:VeFaasSandboxProvider"
    DeerFlow 会通过 resolve_class() 加载此类。

    构造函数无参：从 backend/.env 读配置（POC 阶段方案）。
    """

    def __init__(self) -> None:
        self._vefaas_config = VeFaasConfig.from_env_file()
        super().__init__()

    # ──────────────────────────────────────────────
    # 重写 1：_create_backend 返回 VeFaasSandboxBackend
    # ──────────────────────────────────────────────

    def _create_backend(self) -> SandboxBackend:
        logger.info("VeFaasSandboxProvider._create_backend: using veFaaS backend")
        return VeFaasSandboxBackend(self._vefaas_config)

    # ──────────────────────────────────────────────
    # 重写 2：_create_sandbox 使用 VeFaasAioSandbox + 带 header 的 wait_ready
    # ──────────────────────────────────────────────

    def _create_sandbox(self, thread_id: str | None, sandbox_id: str) -> str:
        """与父类实现几乎相同，只有两处差异（标了 [VEFAAS DIFF]）。"""
        extra_mounts = self._get_extra_mounts(thread_id)

        # Enforce replicas
        replicas = self._config.get("replicas", DEFAULT_REPLICAS)
        with self._lock:
            total = len(self._sandboxes) + len(self._warm_pool)
        if total >= replicas:
            evicted = self._evict_oldest_warm()
            if evicted:
                logger.info(
                    "Evicted warm-pool sandbox %s to stay within replicas=%s",
                    evicted,
                    replicas,
                )
            else:
                logger.warning(
                    "All %s replica slots are in active use; creating sandbox %s beyond the soft limit",
                    replicas,
                    sandbox_id,
                )

        info = self._backend.create(thread_id, sandbox_id, extra_mounts=extra_mounts or None)

        # [VEFAAS DIFF 1] 使用 backend 自己的 wait_ready（带 header）
        # 而不是 cellflow_community.aio_sandbox.backend.wait_for_sandbox_ready
        assert isinstance(self._backend, VeFaasSandboxBackend)
        if not self._backend.wait_ready(info, timeout=60):
            self._backend.destroy(info)
            raise RuntimeError(f"Sandbox {sandbox_id} (vefaas={info.container_id}) failed to become ready within timeout at {info.sandbox_url}")

        # [VEFAAS DIFF 2] 使用 VeFaasAioSandbox 而不是 AioSandbox
        sandbox = VeFaasAioSandbox(
            id=info.sandbox_id,
            base_url=info.sandbox_url,
            vefaas_cloud_id=info.container_id or "",
            api_key=self._vefaas_config.api_key,
        )

        with self._lock:
            self._sandboxes[info.sandbox_id] = sandbox
            self._sandbox_infos[info.sandbox_id] = info
            self._last_activity[info.sandbox_id] = time.time()
            if thread_id:
                self._thread_sandboxes[thread_id] = info.sandbox_id

        logger.info(
            "Created veFaaS sandbox syntra=%s vefaas=%s for thread %s at %s",
            info.sandbox_id,
            info.container_id,
            thread_id,
            info.sandbox_url,
        )
        return info.sandbox_id
