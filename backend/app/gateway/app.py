import asyncio
import contextlib
import logging
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI

from app.gateway.config import get_gateway_config
from app.gateway.deps import langgraph_runtime
from app.gateway.routers import (
    agents,
    artifacts,
    assistants_compat,
    channels,
    mcp,
    memory,
    models,
    runs,
    skills,
    suggestions,
    thread_runs,
    threads,
    uploads,
)
from deerflow.config.app_config import get_app_config

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Stuck-run monitor constants (overridable via environment variables)
# ---------------------------------------------------------------------------
_DEFAULT_LANGGRAPH_URL = "http://localhost:2024"
_STUCK_RUN_CHECK_INTERVAL = int(os.environ.get("STUCK_RUN_CHECK_INTERVAL_SECONDS", "60"))
# Threshold must be well above the longest subagent timeout (currently 900 s).
_STUCK_RUN_THRESHOLD = int(os.environ.get("STUCK_RUN_THRESHOLD_SECONDS", "3600"))


async def _stuck_run_monitor() -> None:
    """Background task that periodically cancels LangGraph runs stuck after a restart.

    After a server restart LangGraph can restore a run to a "running" state but
    never re-dispatch its pending parallel tool calls, causing the run to block
    indefinitely. This monitor detects such runs by age and cancels them with
    ``action=rollback`` so that queued successor runs can proceed.
    """
    langgraph_url = os.environ.get("LANGGRAPH_API_URL", _DEFAULT_LANGGRAPH_URL)
    logger.info(
        "Stuck-run monitor started (url=%s, interval=%ds, threshold=%ds)",
        langgraph_url,
        _STUCK_RUN_CHECK_INTERVAL,
        _STUCK_RUN_THRESHOLD,
    )

    async with httpx.AsyncClient(base_url=langgraph_url, timeout=10.0) as client:
        while True:
            try:
                await asyncio.sleep(_STUCK_RUN_CHECK_INTERVAL)
                await _check_and_cancel_stuck_runs(client)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Stuck-run monitor encountered an unexpected error; will retry next cycle")

    logger.info("Stuck-run monitor stopped")


async def _check_and_cancel_stuck_runs(client: httpx.AsyncClient) -> None:
    """Single scan: find busy threads, identify stuck runs, cancel them."""
    try:
        resp = await client.post("/threads/search", json={"status": "busy"})
        resp.raise_for_status()
        busy_threads: list[dict] = resp.json()
    except (httpx.ConnectError, httpx.ConnectTimeout):
        # LangGraph server not reachable (e.g. gateway mode or server is down) — skip silently.
        return
    except Exception:
        logger.exception("Failed to query busy threads from LangGraph")
        return

    now = datetime.now(tz=timezone.utc)

    for thread in busy_threads:
        thread_id = thread.get("thread_id")
        if not thread_id:
            continue
        try:
            runs_resp = await client.get(f"/threads/{thread_id}/runs")
            runs_resp.raise_for_status()
            thread_runs_list: list[dict] = runs_resp.json()
        except Exception:
            logger.warning("Failed to list runs for thread %s", thread_id)
            continue

        for run in thread_runs_list:
            if run.get("status") != "running":
                continue

            run_id = run.get("run_id")
            created_at_raw = run.get("created_at")
            if not run_id or not created_at_raw:
                continue

            try:
                created_at = datetime.fromisoformat(created_at_raw.replace("Z", "+00:00"))
                age_seconds = (now - created_at).total_seconds()
            except Exception:
                continue

            if age_seconds <= _STUCK_RUN_THRESHOLD:
                continue

            logger.warning(
                "Cancelling stuck run %s on thread %s (age=%.0fs > threshold=%ds)",
                run_id,
                thread_id,
                age_seconds,
                _STUCK_RUN_THRESHOLD,
            )
            try:
                cancel_resp = await client.post(
                    f"/threads/{thread_id}/runs/{run_id}/cancel",
                    json={"wait": False, "action": "rollback"},
                )
                cancel_resp.raise_for_status()
                logger.info("Cancelled stuck run %s on thread %s", run_id, thread_id)
            except Exception:
                logger.exception("Failed to cancel stuck run %s on thread %s", run_id, thread_id)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan handler."""

    # Load config and check necessary environment variables at startup
    try:
        get_app_config()
        logger.info("Configuration loaded successfully")
    except Exception as e:
        error_msg = f"Failed to load configuration during gateway startup: {e}"
        logger.exception(error_msg)
        raise RuntimeError(error_msg) from e
    config = get_gateway_config()
    logger.info(f"Starting API Gateway on {config.host}:{config.port}")

    # Initialize LangGraph runtime components (StreamBridge, RunManager, checkpointer, store)
    async with langgraph_runtime(app):
        logger.info("LangGraph runtime initialised")

        # Start IM channel service if any channels are configured
        try:
            from app.channels.service import start_channel_service

            channel_service = await start_channel_service()
            logger.info("Channel service started: %s", channel_service.get_status())
        except Exception:
            logger.exception("No IM channels configured or channel service failed to start")

        # Start stuck-run monitor to auto-cancel runs that get permanently blocked
        # after a server restart (LangGraph checkpoint restore bug).
        monitor_task = asyncio.create_task(_stuck_run_monitor())

        try:
            yield
        finally:
            monitor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await monitor_task

        # Stop channel service on shutdown
        try:
            from app.channels.service import stop_channel_service

            await stop_channel_service()
        except Exception:
            logger.exception("Failed to stop channel service")

    logger.info("Shutting down API Gateway")


def create_app() -> FastAPI:
    """Create and configure the FastAPI application.

    Returns:
        Configured FastAPI application instance.
    """

    app = FastAPI(
        title="DeerFlow API Gateway",
        description="""
## DeerFlow API Gateway

API Gateway for DeerFlow - A LangGraph-based AI agent backend with sandbox execution capabilities.

### Features

- **Models Management**: Query and retrieve available AI models
- **MCP Configuration**: Manage Model Context Protocol (MCP) server configurations
- **Memory Management**: Access and manage global memory data for personalized conversations
- **Skills Management**: Query and manage skills and their enabled status
- **Artifacts**: Access thread artifacts and generated files
- **Health Monitoring**: System health check endpoints

### Architecture

LangGraph requests are handled by nginx reverse proxy.
This gateway provides custom endpoints for models, MCP configuration, skills, and artifacts.
        """,
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        openapi_tags=[
            {
                "name": "models",
                "description": "Operations for querying available AI models and their configurations",
            },
            {
                "name": "mcp",
                "description": "Manage Model Context Protocol (MCP) server configurations",
            },
            {
                "name": "memory",
                "description": "Access and manage global memory data for personalized conversations",
            },
            {
                "name": "skills",
                "description": "Manage skills and their configurations",
            },
            {
                "name": "artifacts",
                "description": "Access and download thread artifacts and generated files",
            },
            {
                "name": "uploads",
                "description": "Upload and manage user files for threads",
            },
            {
                "name": "threads",
                "description": "Manage DeerFlow thread-local filesystem data",
            },
            {
                "name": "agents",
                "description": "Create and manage custom agents with per-agent config and prompts",
            },
            {
                "name": "suggestions",
                "description": "Generate follow-up question suggestions for conversations",
            },
            {
                "name": "channels",
                "description": "Manage IM channel integrations (Feishu, Slack, Telegram)",
            },
            {
                "name": "assistants-compat",
                "description": "LangGraph Platform-compatible assistants API (stub)",
            },
            {
                "name": "runs",
                "description": "LangGraph Platform-compatible runs lifecycle (create, stream, cancel)",
            },
            {
                "name": "health",
                "description": "Health check and system status endpoints",
            },
        ],
    )

    # CORS is handled by nginx - no need for FastAPI middleware

    # Include routers
    # Models API is mounted at /api/models
    app.include_router(models.router)

    # MCP API is mounted at /api/mcp
    app.include_router(mcp.router)

    # Memory API is mounted at /api/memory
    app.include_router(memory.router)

    # Skills API is mounted at /api/skills
    app.include_router(skills.router)

    # Artifacts API is mounted at /api/threads/{thread_id}/artifacts
    app.include_router(artifacts.router)

    # Uploads API is mounted at /api/threads/{thread_id}/uploads
    app.include_router(uploads.router)

    # Thread cleanup API is mounted at /api/threads/{thread_id}
    app.include_router(threads.router)

    # Agents API is mounted at /api/agents
    app.include_router(agents.router)

    # Suggestions API is mounted at /api/threads/{thread_id}/suggestions
    app.include_router(suggestions.router)

    # Channels API is mounted at /api/channels
    app.include_router(channels.router)

    # Assistants compatibility API (LangGraph Platform stub)
    app.include_router(assistants_compat.router)

    # Thread Runs API (LangGraph Platform-compatible runs lifecycle)
    app.include_router(thread_runs.router)

    # Stateless Runs API (stream/wait without a pre-existing thread)
    app.include_router(runs.router)

    @app.get("/health", tags=["health"])
    async def health_check() -> dict:
        """Health check endpoint.

        Returns:
            Service health status information.
        """
        return {"status": "healthy", "service": "deer-flow-gateway"}

    return app


# Create app instance for uvicorn
app = create_app()
