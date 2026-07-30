"""AgentRunSandboxProvider — SandboxProvider backed by Alibaba Cloud AgentRun."""

from __future__ import annotations

import logging
import threading
import time
import uuid

from deerflow.sandbox.progress import emit_sandbox_progress
from deerflow.sandbox.sandbox import Sandbox
from deerflow.sandbox.sandbox_provider import SandboxProvider

from .config import AgentRunConfig
from .registry import MemorySandboxRegistry, RedisSandboxRegistry, SandboxRegistry
from .sandbox import AgentRunSandbox

logger = logging.getLogger(__name__)

_ACQUIRE_LOCK_TIMEOUT = 60
_THREAD_LOCK_CLEANUP_THRESHOLD = 500
_TTL_REFRESH_INTERVAL = 60


class AgentRunSandboxProvider(SandboxProvider):
    """Alibaba Cloud AgentRun sandbox provider.

    Creates AgentRun sandbox instances with optional OSS mount configuration.
    Each (user, thread) pair gets its own sandbox via registry-based mapping.
    Per-thread-id locks serialize concurrent acquire calls for the same thread.

    Config loading: ``config.yaml`` sandbox.use points here via reflection.
    Credentials come from environment variables (AGENTRUN_ACCESS_KEY_ID etc.).
    """

    def __init__(self) -> None:
        self._config = AgentRunConfig.from_env()
        self._sandboxes: dict[str, AgentRunSandbox] = {}
        self._sdk_instances: dict[str, object] = {}
        self._lock = threading.Lock()
        self._thread_locks: dict[str, threading.Lock] = {}
        self._last_ttl_refresh: dict[str, float] = {}
        self._shutdown_called = False
        self._registry: SandboxRegistry = self._build_registry()

    def _build_registry(self) -> SandboxRegistry:
        if self._config.registry_redis_url:
            try:
                return RedisSandboxRegistry(
                    self._config.registry_redis_url,
                    self._config.registry_key_prefix,
                )
            except Exception:
                logger.warning("Failed to connect to Redis, falling back to memory registry", exc_info=True)
        return MemorySandboxRegistry()

    # -- SandboxProvider interface -------------------------------------------

    def acquire(self, thread_id: str | None = None) -> str:
        if self._shutdown_called:
            raise RuntimeError("AgentRunSandboxProvider is shutting down")

        thread_id = thread_id or None

        with self._lock:
            if thread_id and thread_id not in self._thread_locks:
                self._thread_locks[thread_id] = threading.Lock()
            thread_lock = self._thread_locks.get(thread_id) if thread_id else None
            self._maybe_cleanup_thread_locks()

        if thread_lock:
            acquired = thread_lock.acquire(timeout=_ACQUIRE_LOCK_TIMEOUT)
            if not acquired:
                raise RuntimeError(f"Timed out waiting to acquire sandbox for thread {thread_id} (>{_ACQUIRE_LOCK_TIMEOUT}s)")
        try:
            return self._acquire_inner(thread_id)
        finally:
            if thread_lock:
                thread_lock.release()

    def get(self, sandbox_id: str) -> Sandbox | None:
        with self._lock:
            return self._sandboxes.get(sandbox_id)

    def release(self, sandbox_id: str) -> None:
        """No-op: keep the sandbox alive for reuse within the same thread."""
        logger.debug("Sandbox %s release requested (no-op, kept alive)", sandbox_id)

    # -- Internal acquire logic ----------------------------------------------

    def _acquire_inner(self, thread_id: str | None) -> str:
        if self._shutdown_called:
            raise RuntimeError("AgentRunSandboxProvider is shutting down")

        user_id = self._resolve_user_id()

        # Step 1: Check registry
        if thread_id:
            existing_sid = None
            try:
                existing_sid = self._registry.get(user_id, thread_id)
            except Exception:
                logger.warning("Registry read failed, proceeding without cache", exc_info=True)

            if existing_sid:
                # Step 2: Local SDK instance exists → health check
                sdk_sandbox = self._sdk_instances.get(existing_sid)
                if sdk_sandbox and self._check_alive(sdk_sandbox):
                    self._refresh_registry_ttl(user_id, thread_id, existing_sid)
                    return existing_sid

                # Step 3: No local instance (created by another instance) → re-attach
                if not sdk_sandbox:
                    sdk_sandbox = self._try_reattach(existing_sid)
                    if sdk_sandbox:
                        sandbox = AgentRunSandbox(id=existing_sid, sdk_sandbox=sdk_sandbox, command_timeout=self._config.command_timeout)
                        with self._lock:
                            self._sandboxes[existing_sid] = sandbox
                            self._sdk_instances[existing_sid] = sdk_sandbox
                        self._refresh_registry_ttl(user_id, thread_id, existing_sid)
                        return existing_sid

                # Sandbox is dead, clean up
                logger.info("Sandbox %s for thread %s is no longer alive, recreating", existing_sid, thread_id)
                self._cleanup_dead_sandbox(user_id, thread_id, existing_sid)

        # Step 4: Create new sandbox
        sandbox_id = f"ar-{uuid.uuid4().hex[:12]}"
        oss_mount = self._build_oss_mount(thread_id, user_id)

        emit_sandbox_progress({"type": "sandbox_starting", "sandbox_id": sandbox_id, "thread_id": thread_id})
        started = time.monotonic()

        sdk_sandbox = self._create_sdk_sandbox(sandbox_id, oss_mount)

        health = self._wait_for_health(sdk_sandbox, sandbox_id)
        if not health:
            try:
                sdk_sandbox.stop()
            except Exception:
                pass
            raise RuntimeError(f"AgentRun sandbox {sandbox_id} failed health check")

        emit_sandbox_progress({"type": "sandbox_ready", "sandbox_id": sandbox_id, "thread_id": thread_id, "elapsed_ms": int((time.monotonic() - started) * 1000)})

        sandbox = AgentRunSandbox(id=sandbox_id, sdk_sandbox=sdk_sandbox, command_timeout=self._config.command_timeout)

        if self._shutdown_called:
            try:
                sdk_sandbox.stop()
            except Exception:
                pass
            raise RuntimeError("AgentRunSandboxProvider is shutting down")

        with self._lock:
            self._sandboxes[sandbox_id] = sandbox
            self._sdk_instances[sandbox_id] = sdk_sandbox

        if thread_id:
            try:
                self._registry.set(user_id, thread_id, sandbox_id, ttl=self._config.idle_timeout)
            except Exception:
                logger.warning("Registry write failed for thread %s", thread_id, exc_info=True)

        logger.info("Created AgentRun sandbox %s for thread %s", sandbox_id, thread_id)
        return sandbox_id

    # -- Re-attach -----------------------------------------------------------

    def _try_reattach(self, sandbox_id: str) -> object | None:
        """Connect to an existing sandbox created by another instance."""
        try:
            from agentrun import AioSandbox, Config
            from agentrun.sandbox import TemplateType

            sdk_config = Config(
                access_key_id=self._config.access_key,
                access_key_secret=self._config.secret_key,
                account_id=self._config.account_id,
                region_id=self._config.region,
            )
            template_type = getattr(TemplateType, self._config.template_type, TemplateType.AIO)
            sdk_sandbox = AioSandbox.connect(
                sandbox_id=sandbox_id,
                template_type=template_type,
                config=sdk_config,
            )
            if self._check_alive(sdk_sandbox):
                return sdk_sandbox
        except Exception:
            logger.info("Re-attach to sandbox %s failed", sandbox_id, exc_info=True)
        return None

    # -- Helpers -------------------------------------------------------------

    def _cleanup_dead_sandbox(self, user_id: str, thread_id: str, sandbox_id: str) -> None:
        try:
            self._registry.delete(user_id, thread_id)
        except Exception:
            logger.warning("Registry delete failed for thread %s", thread_id, exc_info=True)
        with self._lock:
            self._sandboxes.pop(sandbox_id, None)
            self._sdk_instances.pop(sandbox_id, None)
            self._last_ttl_refresh.pop(sandbox_id, None)

    def _refresh_registry_ttl(self, user_id: str, thread_id: str, sandbox_id: str) -> None:
        now = time.time()
        last = self._last_ttl_refresh.get(sandbox_id, 0)
        if now - last < _TTL_REFRESH_INTERVAL:
            return
        try:
            self._registry.set(user_id, thread_id, sandbox_id, ttl=self._config.idle_timeout)
            self._last_ttl_refresh[sandbox_id] = now
        except Exception:
            pass

    def _maybe_cleanup_thread_locks(self) -> None:
        if len(self._thread_locks) <= _THREAD_LOCK_CLEANUP_THRESHOLD:
            return
        stale_keys = [tid for tid, lock in self._thread_locks.items() if not lock.locked()]
        for tid in stale_keys:
            self._thread_locks.pop(tid, None)

    # -- SDK sandbox creation ------------------------------------------------

    def _create_sdk_sandbox(self, sandbox_id: str, oss_mount: object | None):
        """Create an agentrun-sdk AioSandbox instance."""
        from agentrun import AioSandbox, Config

        sdk_config = Config(
            access_key_id=self._config.access_key,
            access_key_secret=self._config.secret_key,
            account_id=self._config.account_id,
            region_id=self._config.region,
        )

        from agentrun.sandbox import TemplateType

        template_type = getattr(TemplateType, self._config.template_type, TemplateType.AIO)

        kwargs = {
            "template_type": template_type,
            "template_name": self._config.template_name,
            "sandbox_id": sandbox_id,
            "sandbox_idle_timeout_seconds": self._config.idle_timeout,
            "config": sdk_config,
        }
        if oss_mount:
            kwargs["oss_mount_config"] = oss_mount

        return AioSandbox.create(**kwargs)

    # -- OSS mount construction ----------------------------------------------

    def _build_oss_mount(self, thread_id: str | None, user_id: str) -> object | None:
        """Build the AgentRun OSSMountConfig.

        Creates 3 mount points:
          /mnt/user-data    → {prefix}/users/{uid}/threads/{tid}/   (rw)
          /mnt/skills       → {prefix}/skills/                      (ro)
          /mnt/user-skills  → {prefix}/users/{uid}/skills/          (ro)

        Returns None if OSS is not enabled.
        """
        if not self._config.oss_enabled:
            return None

        cfg = self._config
        uid = user_id or "default"
        tid = thread_id or "default"
        prefix = cfg.oss_prefix or "cellflow"

        from agentrun.sandbox import OSSMountConfig, OSSMountPoint

        return OSSMountConfig(
            mount_points=[
                OSSMountPoint(
                    bucket_name=cfg.oss_bucket,
                    bucket_path=f"/{prefix}/users/{uid}/threads/{tid}",
                    endpoint=f"http://{cfg.oss_endpoint}",
                    mount_dir="/mnt/user-data",
                    read_only=False,
                ),
                OSSMountPoint(
                    bucket_name=cfg.oss_bucket,
                    bucket_path=f"/{prefix}/skills",
                    endpoint=f"http://{cfg.oss_endpoint}",
                    mount_dir="/mnt/skills",
                    read_only=True,
                ),
                OSSMountPoint(
                    bucket_name=cfg.oss_bucket,
                    bucket_path=f"/{prefix}/users/{uid}/skills",
                    endpoint=f"http://{cfg.oss_endpoint}",
                    mount_dir="/mnt/user-skills",
                    read_only=True,
                ),
            ]
        )

    @staticmethod
    def _resolve_user_id() -> str:
        try:
            from deerflow.runtime.user_context import get_effective_user_id

            return get_effective_user_id()
        except Exception:
            return "default"

    @staticmethod
    def _wait_for_health(sdk_sandbox, sandbox_id, max_wait: float = 30, interval: float = 3) -> bool:
        deadline = time.time() + max_wait
        while time.time() < deadline:
            try:
                result = sdk_sandbox.check_health()
                if isinstance(result, dict):
                    if result.get("status") == "ok":
                        return True
                elif result:
                    return True
            except Exception:
                pass
            time.sleep(interval)
        return False

    @staticmethod
    def _check_alive(sdk_sandbox) -> bool:
        try:
            result = sdk_sandbox.check_health()
            if isinstance(result, dict):
                return result.get("status") == "ok"
            return bool(result)
        except Exception:
            return False

    def shutdown(self) -> None:
        """Stop all managed AgentRun sandboxes."""
        with self._lock:
            if self._shutdown_called:
                return
            self._shutdown_called = True
            instances = list(self._sdk_instances.items())
            self._sdk_instances.clear()
            self._sandboxes.clear()
            self._thread_locks.clear()
            self._last_ttl_refresh.clear()

        if isinstance(self._registry, MemorySandboxRegistry):
            self._registry.clear()

        for sandbox_id, sdk_sandbox in instances:
            try:
                sdk_sandbox.stop()
                logger.info("Stopped AgentRun sandbox %s during shutdown", sandbox_id)
            except Exception as e:
                logger.warning(
                    "Failed to stop AgentRun sandbox %s during shutdown: %s",
                    sandbox_id,
                    e,
                )
