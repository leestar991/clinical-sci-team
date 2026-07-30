"""VeFaasSandboxBackend — 实现 DeerFlow 的 SandboxBackend 抽象接口。

职责：包装火山引擎 veFaaS SDK（volcenginesdkvefaas），提供 create / destroy /
is_alive / discover / list_running 五个操作。

关键事实（2026-04-30 smoke test 实测）：
- metadata: dict[str, str] 能完整透传，list_sandboxes 可按 metadata filter
  精确反查 → discover 的数据基础
- status 字段值是 "Ready"（不是 "Running"），活跃状态集合 {Ready, Running}
- list_sandboxes 不能传 status 参数（InvalidParameter），改为客户端过滤
- describe 的 metadata 返回 metadata_list（list of {meta_key, meta_value}），
  list 的 metadata 返回 dict（两种格式并存）
- kill_sandbox 异步，返回后 status=Terminating

不支持（POC 阶段忽略）：
- extra_mounts（宿主机 bind mount） → veFaaS 不支持，延后到 Phase 2 走 TOS 挂载
- cross-process idempotency（多 pod 场景） → POC 单进程够用
"""

from __future__ import annotations

import logging
import time

import volcenginesdkcore
import volcenginesdkvefaas
from volcenginesdkvefaas.models import (
    CreateSandboxRequest,
    CredentialsForCreateSandboxInput,
    DescribeSandboxRequest,
    InstanceTosMountConfigForCreateSandboxInput,
    KillSandboxRequest,
    ListSandboxesRequest,
    TosMountPointForCreateSandboxInput,
)

from cellflow_community.aio_sandbox.backend import SandboxBackend
from cellflow_community.aio_sandbox.sandbox_info import SandboxInfo

from .config import ALIVE_STATUSES, VeFaasConfig

logger = logging.getLogger(__name__)


class VeFaasSandboxBackend(SandboxBackend):
    """veFaaS 实现的 DeerFlow SandboxBackend。

    - 每个 DeerFlow sandbox_id 对应 veFaaS 里一个 Sandbox 实例
    - metadata 字段作为业务 ID → 云 ID 的映射载体
    """

    def __init__(self, config: VeFaasConfig):
        self._config = config
        sdk_config = volcenginesdkcore.Configuration()
        sdk_config.ak = config.access_key
        sdk_config.sk = config.secret_key
        sdk_config.region = config.region
        self._api = volcenginesdkvefaas.VEFAASApi(volcenginesdkcore.ApiClient(sdk_config))
        logger.info("VeFaasSandboxBackend initialized: %s", config.safe_repr())

    # ──────────────────────────────────────────────
    # SandboxBackend 抽象接口实现
    # ──────────────────────────────────────────────

    def create(
        self,
        thread_id: str,
        sandbox_id: str,
        extra_mounts: list[tuple[str, str, bool]] | None = None,
    ) -> SandboxInfo:
        """创建 veFaaS 沙箱实例。

        ID 双标识：
        - DeerFlow 传入的 sandbox_id = 业务 ID，用于上层字典 key
        - veFaaS SDK 返回的 sandbox_id = 云 ID，用于 API 调用 + header 路由

        SandboxInfo.sandbox_id 保留业务 ID，云 ID 塞进 container_id 字段。
        extra_mounts 在 POC 阶段忽略（veFaaS 不支持宿主机 bind mount）。
        """
        if extra_mounts:
            logger.warning("VeFaasSandboxBackend.create: extra_mounts is ignored in POC (veFaaS does not support host bind mounts)")

        logger.info(
            "Creating veFaaS sandbox: thread_id=%s syntra_sandbox_id=%s",
            thread_id,
            sandbox_id,
        )

        req = CreateSandboxRequest(
            function_id=self._config.function_id,
            cpu_milli=self._config.cpu_milli,
            memory_mb=self._config.memory_mb,
            timeout=self._config.timeout_minutes,
            timeout_unit="minute",
            metadata={
                "syntra_sandbox_id": sandbox_id,
                "syntra_thread_id": thread_id or "",
                "syntra_tenant_id": "default",
            },
            instance_tos_mount_config=self._build_tos_mount(thread_id, user_id=self._resolve_user_id()),
        )

        last_err: Exception | None = None
        for attempt in range(3):
            try:
                resp = self._api.create_sandbox(req)
                break
            except volcenginesdkcore.rest.ApiException as e:
                if not self._is_cold_start_timeout(e):
                    logger.error(
                        "create_sandbox non-timeout ApiException (attempt %d/3): status=%s reason=%s body=%s",
                        attempt + 1,
                        getattr(e, "status", "?"),
                        e.reason,
                        getattr(e, "body", ""),
                    )
                    raise
                last_err = e
                wait_s = 3 * (2**attempt)
                logger.warning(
                    "create_sandbox cold start timeout (attempt %d/3), retrying in %ds: %s",
                    attempt + 1,
                    wait_s,
                    e.reason,
                )
                time.sleep(wait_s)
            except Exception as e:
                logger.error(
                    "create_sandbox unexpected %s (attempt %d/3): %s",
                    type(e).__name__,
                    attempt + 1,
                    e,
                )
                raise
        else:
            raise RuntimeError(f"create_sandbox failed after 3 attempts: {last_err}")

        vefaas_cloud_id = resp.sandbox_id
        logger.info(
            "veFaaS created instance %s for syntra_sandbox_id=%s",
            vefaas_cloud_id,
            sandbox_id,
        )

        return SandboxInfo(
            sandbox_id=sandbox_id,  # 业务 ID
            container_id=vefaas_cloud_id,  # 云 ID
            sandbox_url=self._config.endpoint,  # 所有实例同 URL
            created_at=time.time(),
        )

    def destroy(self, info: SandboxInfo) -> None:
        """销毁实例（异步：返回后 status 进入 Terminating）。"""
        vefaas_id = info.container_id
        if not vefaas_id:
            logger.warning(
                "destroy called with no container_id, skipping: %s",
                info.sandbox_id,
            )
            return

        logger.info(
            "Destroying veFaaS sandbox syntra=%s vefaas=%s",
            info.sandbox_id,
            vefaas_id,
        )
        try:
            self._api.kill_sandbox(
                KillSandboxRequest(
                    function_id=self._config.function_id,
                    sandbox_id=vefaas_id,
                )
            )
        except Exception as e:
            logger.warning("kill_sandbox failed for %s: %s", vefaas_id, e)

    def is_alive(self, info: SandboxInfo) -> bool:
        """查询实例是否活着：describe → status ∈ {Ready, Running}。"""
        vefaas_id = info.container_id
        if not vefaas_id:
            return False
        try:
            resp = self._api.describe_sandbox(
                DescribeSandboxRequest(
                    function_id=self._config.function_id,
                    sandbox_id=vefaas_id,
                )
            )
            return resp.status in ALIVE_STATUSES
        except Exception as e:
            logger.debug("describe_sandbox failed for %s: %s", vefaas_id, e)
            return False

    def discover(self, sandbox_id: str) -> SandboxInfo | None:
        """跨进程发现：用 DeerFlow 的 syntra_sandbox_id 反查 veFaaS。

        1. list_sandboxes(metadata={"syntra_sandbox_id": sandbox_id}) 查
        2. 客户端过滤 status ∈ {Ready, Running}
        3. 命中则返回 SandboxInfo

        注意：不能传 status 参数，SDK 会拒绝（InvalidParameter）。
        """
        try:
            resp = self._api.list_sandboxes(
                ListSandboxesRequest(
                    function_id=self._config.function_id,
                    metadata={"syntra_sandbox_id": sandbox_id},
                    page_size=10,
                )
            )
            alive = [sb for sb in (resp.sandboxes or []) if sb.status in ALIVE_STATUSES]
            if alive:
                sb = alive[0]
                logger.info(
                    "discover: found syntra_sandbox_id=%s → vefaas_id=%s",
                    sandbox_id,
                    sb.id,
                )
                return SandboxInfo(
                    sandbox_id=sandbox_id,
                    container_id=sb.id,
                    sandbox_url=self._config.endpoint,
                )
        except Exception as e:
            logger.debug("list_sandboxes discover failed for %s: %s", sandbox_id, e)
        return None

    def list_running(self) -> list[SandboxInfo]:
        """列出本 function_id 下所有活跃实例。

        返回的 SandboxInfo.sandbox_id 取 metadata 里的 syntra_sandbox_id（业务 ID），
        container_id 取 veFaaS 云 ID。没有 syntra metadata 的实例跳过。
        """
        try:
            resp = self._api.list_sandboxes(
                ListSandboxesRequest(
                    function_id=self._config.function_id,
                    page_size=100,
                )
            )
            result: list[SandboxInfo] = []
            skipped_no_metadata = 0
            for sb in resp.sandboxes or []:
                if sb.status not in ALIVE_STATUSES:
                    continue
                syntra_id = (sb.metadata or {}).get("syntra_sandbox_id")
                if not syntra_id:
                    skipped_no_metadata += 1
                    continue
                result.append(
                    SandboxInfo(
                        sandbox_id=syntra_id,
                        container_id=sb.id,
                        sandbox_url=self._config.endpoint,
                        created_at=time.time(),
                    )
                )
            logger.info(
                "list_running: %d alive managed sandboxes (from total %d, skipped %d without syntra metadata)",
                len(result),
                resp.total or 0,
                skipped_no_metadata,
            )
            return result
        except Exception as e:
            logger.warning("list_sandboxes failed: %s", e)
            return []

    def _build_tos_mount(
        self,
        thread_id: str | None,
        user_id: str | None = None,
    ) -> InstanceTosMountConfigForCreateSandboxInput | None:
        cfg = self._config
        if not cfg.tos_enabled:
            return None

        mount_prefix = cfg.tos_prefix or "cellflow"

        # Per-thread isolation: each sandbox mounts its own thread-scoped
        # TOS path so files from different threads never collide.
        # Key structure: {prefix}/users/{uid}/threads/{tid}/{uploads,outputs,workspace}/
        uid = user_id or "default"
        tid = thread_id or "default"
        thread_base = f"/{mount_prefix}/users/{uid}/threads/{tid}"
        user_base = f"/{mount_prefix}/users/{uid}"

        logger.info(
            "VeFaas TOS mount config: prefix=%s bucket=%s uploads_bucket_path=%r outputs_bucket_path=%r workspace_bucket_path=%r skills_bucket_path=%r",
            mount_prefix,
            cfg.tos_bucket,
            f"{thread_base}/uploads/",
            f"{thread_base}/outputs/",
            f"{thread_base}/workspace/",
            f"{user_base}/skills/",
        )

        return InstanceTosMountConfigForCreateSandboxInput(
            enable=True,
            credentials=CredentialsForCreateSandboxInput(
                access_key_id=cfg.access_key,
                secret_access_key=cfg.secret_key,
            ),
            tos_mount_points=[
                TosMountPointForCreateSandboxInput(
                    bucket_name=cfg.tos_bucket,
                    bucket_path=f"{thread_base}/",
                    local_mount_path="/mnt/user-data",
                    endpoint=cfg.tos_internal_endpoint,
                    read_only=False,
                ),
                TosMountPointForCreateSandboxInput(
                    bucket_name=cfg.tos_bucket,
                    bucket_path=f"/{mount_prefix}/skills/public/",
                    local_mount_path="/mnt/skills",
                    endpoint=cfg.tos_internal_endpoint,
                    read_only=True,
                ),
                TosMountPointForCreateSandboxInput(
                    bucket_name=cfg.tos_bucket,
                    bucket_path=f"{user_base}/skills/",
                    local_mount_path="/mnt/user-skills",
                    endpoint=cfg.tos_internal_endpoint,
                    read_only=True,
                ),
            ],
        )

    @staticmethod
    def _resolve_user_id() -> str:
        """Resolve the effective user ID for TOS key prefix."""
        try:
            from deerflow.runtime.user_context import get_effective_user_id

            return get_effective_user_id()
        except Exception:
            return "default"

    @staticmethod
    def _is_cold_start_timeout(exc: volcenginesdkcore.rest.ApiException) -> bool:
        body = getattr(exc, "body", "") or str(exc)
        return "UserTimeoutError" in body or "function_cold_start_timeout" in body

    # ──────────────────────────────────────────────
    # 辅助：带 header 的就绪探测（替代 DeerFlow 自带的 wait_for_sandbox_ready）
    # ──────────────────────────────────────────────

    def wait_ready(self, info: SandboxInfo, timeout: int = 60) -> bool:
        """等待实例就绪（轮询 describe_sandbox 的 status）。

        DeerFlow 的 wait_for_sandbox_ready 直接 GET endpoint/v1/sandbox 不带 header，
        veFaaS 下会路由失败。改为轮询 describe_sandbox。
        """
        start = time.time()
        while time.time() - start < timeout:
            if self.is_alive(info):
                return True
            time.sleep(1)
        return False
