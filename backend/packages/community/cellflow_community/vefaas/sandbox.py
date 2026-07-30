"""VeFaasAioSandbox — 继承 DeerFlow AioSandbox，注入 veFaaS 路由 + 数据面鉴权 header。

veFaaS 的所有 Sandbox 实例共用同一个 APIG endpoint，两件事都靠 header 控制：
1. **路由** 到具体实例：`x-faas-instance-name: {vefaas_cloud_id}`
2. **数据面鉴权**（APIG Key Auth 插件）：`Authorization: {api_key}` —— 裸 key，无前缀

DeerFlow 原生的 AioSandbox 创建 agent_sandbox.Sandbox 客户端时没有透传 headers，
所以子类化并替换内部的 client。

ID 双标识：
- id：DeerFlow 业务 ID（BaseSandbox 的 _id，用于日志/追踪等上层语义）
- vefaas_cloud_id：veFaaS 云端真实 ID（用于 HTTP header 路由）
"""

from __future__ import annotations

import threading

from agent_sandbox import Sandbox as AioSandboxClient

from cellflow_community.aio_sandbox.aio_sandbox import AioSandbox
from deerflow.sandbox.sandbox import Sandbox as BaseSandbox


class VeFaasAioSandbox(AioSandbox):
    """AioSandbox 变体：所有 HTTP 请求都带 veFaaS 路由 + Key Auth header。"""

    def __init__(
        self,
        id: str,  # DeerFlow 业务 ID
        base_url: str,  # APIG endpoint（所有实例共用）
        vefaas_cloud_id: str,  # veFaaS 云端真实 ID（用于 header 路由）
        api_key: str,  # APIG Key Auth
        home_dir: str | None = None,
    ) -> None:
        # 绕过父类 AioSandbox.__init__（父类会创建无 header 的 client），
        # 直接调 BaseSandbox.__init__ 初始化 _id，然后手工完成其余字段。
        BaseSandbox.__init__(self, id)
        self._base_url = base_url
        self._vefaas_cloud_id = vefaas_cloud_id
        self._client = AioSandboxClient(
            base_url=base_url,
            headers={
                "x-faas-instance-name": vefaas_cloud_id,  # 路由到实例
                "Authorization": api_key,  # APIG Key Auth，裸 key 无前缀
            },
            timeout=600,
        )
        self._home_dir = home_dir
        self._lock = threading.Lock()

    @property
    def vefaas_cloud_id(self) -> str:
        return self._vefaas_cloud_id

    # 其余 7 个方法（execute_command / read_file / write_file / list_dir /
    # glob / grep / update_file）全部继承父类 AioSandbox 的实现。
