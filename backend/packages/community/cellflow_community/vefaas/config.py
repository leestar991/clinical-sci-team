"""veFaaS POC 配置 —— 从 .env 读取凭证与资源定位符。

MVP 阶段会改为通过 config.yaml sandbox.vefaas.* 子段注入。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path


def _find_env() -> Path:
    """Locate .env by walking up from this module's directory."""
    here = Path(__file__).resolve().parent
    for parent in here.parents:
        candidate = parent / ".env"
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(".env not found in any parent directory")


def _load_env(path: Path) -> dict[str, str]:
    if not path.exists():
        print(f"❌ 未找到 {path}", file=sys.stderr)
        print("   请先在 backend/ 下 cp .env.example .env 并填入凭证", file=sys.stderr)
        sys.exit(1)
    env: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
    return env


def _required(env: dict[str, str], key: str) -> str:
    v = env.get(key, "").strip()
    if not v or v.startswith("AKLT...") or v == "..." or v.endswith("..."):
        print(f"❌ .env 中 {key} 未正确填写（实际值: {v!r}）", file=sys.stderr)
        sys.exit(1)
    return v


@dataclass(frozen=True)
class VeFaasConfig:
    """veFaaS 接入配置。

    字段与 .env 中的键名一一对应。
    """

    access_key: str
    secret_key: str
    region: str
    function_id: str  # Sandbox Application 的 Function ID
    endpoint: str  # APIG 域名（所有实例共用，靠 header 路由）
    api_key: str  # 数据面 Key Auth（Authorization: <api_key> 原文，无前缀）

    # TOS 挂载 — 仅需配置内网 endpoint，其余从 config.yaml storage 自动读取
    tos_internal_endpoint: str = ""  # 内网（veFaaS 沙箱挂载用），.env VEFAAS_TOS_INTERNAL_ENDPOINT

    # 资源规格（MVP 阶段固定，后续可从 config.yaml 读）
    cpu_milli: int = 1000
    memory_mb: int = 2048
    timeout_minutes: int = 60  # veFaaS create 时的 timeout，单位分钟

    @classmethod
    def from_env_file(cls, env_path: Path | None = None) -> VeFaasConfig:
        """从 .env 文件加载（默认路径：项目根目录 .env）。"""
        if env_path is None:
            env_path = _find_env()
        env = _load_env(env_path)
        return cls(
            access_key=_required(env, "VOLC_ACCESS_KEY"),
            secret_key=_required(env, "VOLC_SECRET_KEY"),
            region=_required(env, "VEFAAS_REGION"),
            function_id=_required(env, "VEFAAS_FUNCTION_ID"),
            endpoint=_required(env, "VEFAAS_ENDPOINT").rstrip("/"),
            api_key=_required(env, "VEFAAS_API_KEY"),
            tos_internal_endpoint=env.get("VEFAAS_TOS_INTERNAL_ENDPOINT", "").strip().rstrip("/"),
        )

    @property
    def tos_enabled(self) -> bool:
        """TOS mount is enabled when storage.backend is 'tos' and internal endpoint is set."""
        if not self.tos_internal_endpoint:
            return False
        try:
            from deerflow.config.app_config import get_app_config

            return get_app_config().storage.backend == "tos"
        except Exception:
            return False

    @property
    def tos_bucket(self) -> str:
        """Read from config.yaml storage.tos_bucket."""
        from deerflow.config.app_config import get_app_config

        return get_app_config().storage.tos_bucket

    @property
    def tos_endpoint(self) -> str:
        """Read from config.yaml storage.tos_endpoint."""
        from deerflow.config.app_config import get_app_config

        return get_app_config().storage.tos_endpoint

    @property
    def tos_prefix(self) -> str:
        """Read from config.yaml storage.tos_prefix."""
        from deerflow.config.app_config import get_app_config

        return get_app_config().storage.tos_prefix

    def safe_repr(self) -> str:
        """打印用（不含 SK / api_key）。"""
        return (
            f"VeFaasConfig("
            f"access_key={self.access_key[:8]}...,"
            f"secret_key=***,"
            f"region={self.region},"
            f"function_id={self.function_id},"
            f"endpoint={self.endpoint},"
            f"api_key=***,"
            f"tos_internal_endpoint={self.tos_internal_endpoint},"
            f"tos_enabled={self.tos_enabled},"
            f"cpu_milli={self.cpu_milli},"
            f"memory_mb={self.memory_mb},"
            f"timeout_minutes={self.timeout_minutes})"
        )


# veFaaS 管理面的"活跃"状态集合
ALIVE_STATUSES = frozenset({"Ready", "Running"})
