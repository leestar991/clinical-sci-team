"""VeFaas sandbox provider — 火山引擎 veFaaS serverless container integration.

Replaces DeerFlow's AioSandboxProvider with veFaaS-backed sandbox execution.

Usage:
    config.yaml → sandbox.use: "cellflow_community.vefaas:VeFaasSandboxProvider"
"""


def __getattr__(name: str):
    """Lazy imports so the package loads even without volcengine SDK."""
    if name == "VeFaasSandboxBackend":
        from .backend import VeFaasSandboxBackend

        return VeFaasSandboxBackend
    if name == "VeFaasSandboxProvider":
        from .provider import VeFaasSandboxProvider

        return VeFaasSandboxProvider
    if name == "VeFaasAioSandbox":
        from .sandbox import VeFaasAioSandbox

        return VeFaasAioSandbox
    if name == "VeFaasConfig":
        from .config import VeFaasConfig

        return VeFaasConfig
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "VeFaasSandboxProvider",
    "VeFaasSandboxBackend",
    "VeFaasAioSandbox",
    "VeFaasConfig",
]
