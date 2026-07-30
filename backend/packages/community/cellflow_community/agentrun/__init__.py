"""AgentRun sandbox provider package (Alibaba Cloud AgentRun)."""

import os

# Suppress the agentrun-sdk 0.0.x "breaking changes" import-time warning.
# We pin to a specific version in pyproject.toml, so the SDK author's advice
# is already followed. setdefault keeps the user's explicit override (if any).
os.environ.setdefault("DISABLE_BREAKING_CHANGES_WARNING", "1")

from cellflow_community.agentrun.provider import AgentRunSandboxProvider  # noqa: E402

__all__ = ["AgentRunSandboxProvider"]
