"""Tests for the stuck-run monitor in app.gateway.app."""

import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_client(threads_response=None, runs_response=None, cancel_response=None):
    """Build a mock httpx.AsyncClient with configurable responses."""
    client = AsyncMock(spec=httpx.AsyncClient)

    thread_resp = MagicMock()
    thread_resp.raise_for_status = MagicMock()
    thread_resp.json = MagicMock(return_value=threads_response or [])
    client.post = AsyncMock(return_value=thread_resp)

    run_resp = MagicMock()
    run_resp.raise_for_status = MagicMock()
    run_resp.json = MagicMock(return_value=runs_response or [])
    client.get = AsyncMock(return_value=run_resp)

    return client


def _ts(seconds_ago: float) -> str:
    """Return an ISO-8601 UTC timestamp ``seconds_ago`` seconds in the past."""
    dt = datetime.now(tz=timezone.utc) - timedelta(seconds=seconds_ago)
    return dt.isoformat()


# ---------------------------------------------------------------------------
# Tests for _check_and_cancel_stuck_runs
# ---------------------------------------------------------------------------


class TestCheckAndCancelStuckRuns:
    @pytest.mark.anyio
    async def test_cancels_run_exceeding_threshold(self):
        """A running run older than the threshold must be cancelled with rollback."""
        from app.gateway.app import _check_and_cancel_stuck_runs

        thread_id = "thread-abc"
        run_id = "run-123"

        client = _make_client(
            threads_response=[{"thread_id": thread_id}],
            runs_response=[{"run_id": run_id, "status": "running", "created_at": _ts(3700)}],
        )
        cancel_resp = MagicMock()
        cancel_resp.raise_for_status = MagicMock()

        # The first call to post is /threads/search, subsequent ones are /cancel
        client.post = AsyncMock(side_effect=[
            _make_client(threads_response=[{"thread_id": thread_id}]).post.return_value,
            cancel_resp,
        ])
        client.post.return_value.json.return_value = [{"thread_id": thread_id}]

        with patch("app.gateway.app._STUCK_RUN_THRESHOLD", 3600):
            await _check_and_cancel_stuck_runs(client)

        # Verify cancel was called with rollback action
        cancel_call_args = client.post.call_args_list[-1]
        assert f"/threads/{thread_id}/runs/{run_id}/cancel" in str(cancel_call_args)
        call_json = cancel_call_args.kwargs.get("json") or cancel_call_args.args[1] if len(cancel_call_args.args) > 1 else cancel_call_args.kwargs.get("json")
        assert call_json == {"wait": False, "action": "rollback"}

    @pytest.mark.anyio
    async def test_skips_run_within_threshold(self):
        """A running run younger than the threshold must NOT be cancelled."""
        from app.gateway.app import _check_and_cancel_stuck_runs

        thread_id = "thread-abc"
        run_id = "run-123"

        client = AsyncMock(spec=httpx.AsyncClient)
        search_resp = MagicMock()
        search_resp.raise_for_status = MagicMock()
        search_resp.json = MagicMock(return_value=[{"thread_id": thread_id}])
        client.post = AsyncMock(return_value=search_resp)

        runs_resp = MagicMock()
        runs_resp.raise_for_status = MagicMock()
        runs_resp.json = MagicMock(return_value=[{"run_id": run_id, "status": "running", "created_at": _ts(100)}])
        client.get = AsyncMock(return_value=runs_resp)

        with patch("app.gateway.app._STUCK_RUN_THRESHOLD", 3600):
            await _check_and_cancel_stuck_runs(client)

        # post was called once (threads/search), never for cancel
        assert client.post.call_count == 1

    @pytest.mark.anyio
    async def test_skips_non_running_status(self):
        """Runs with status != 'running' (e.g. 'pending') must be ignored."""
        from app.gateway.app import _check_and_cancel_stuck_runs

        thread_id = "thread-abc"
        run_id = "run-pending"

        client = AsyncMock(spec=httpx.AsyncClient)
        search_resp = MagicMock()
        search_resp.raise_for_status = MagicMock()
        search_resp.json = MagicMock(return_value=[{"thread_id": thread_id}])
        client.post = AsyncMock(return_value=search_resp)

        runs_resp = MagicMock()
        runs_resp.raise_for_status = MagicMock()
        runs_resp.json = MagicMock(return_value=[{"run_id": run_id, "status": "pending", "created_at": _ts(9999)}])
        client.get = AsyncMock(return_value=runs_resp)

        with patch("app.gateway.app._STUCK_RUN_THRESHOLD", 3600):
            await _check_and_cancel_stuck_runs(client)

        assert client.post.call_count == 1  # only the threads/search call

    @pytest.mark.anyio
    async def test_handles_langgraph_connection_error(self):
        """ConnectError from LangGraph must be silently swallowed (no exception raised)."""
        from app.gateway.app import _check_and_cancel_stuck_runs

        client = AsyncMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(side_effect=httpx.ConnectError("refused"))

        # Should not raise
        await _check_and_cancel_stuck_runs(client)

    @pytest.mark.anyio
    async def test_handles_connect_timeout(self):
        """ConnectTimeout from LangGraph must also be silently swallowed."""
        from app.gateway.app import _check_and_cancel_stuck_runs

        client = AsyncMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(side_effect=httpx.ConnectTimeout("timeout"))

        await _check_and_cancel_stuck_runs(client)

    @pytest.mark.anyio
    async def test_no_busy_threads_no_runs_checked(self):
        """When there are no busy threads, the runs endpoint is never called."""
        from app.gateway.app import _check_and_cancel_stuck_runs

        client = AsyncMock(spec=httpx.AsyncClient)
        search_resp = MagicMock()
        search_resp.raise_for_status = MagicMock()
        search_resp.json = MagicMock(return_value=[])
        client.post = AsyncMock(return_value=search_resp)

        await _check_and_cancel_stuck_runs(client)

        client.get.assert_not_called()

    @pytest.mark.anyio
    async def test_missing_thread_id_skipped(self):
        """Threads without a thread_id field are skipped without error."""
        from app.gateway.app import _check_and_cancel_stuck_runs

        client = AsyncMock(spec=httpx.AsyncClient)
        search_resp = MagicMock()
        search_resp.raise_for_status = MagicMock()
        search_resp.json = MagicMock(return_value=[{"not_a_thread_id": "x"}])
        client.post = AsyncMock(return_value=search_resp)

        await _check_and_cancel_stuck_runs(client)

        client.get.assert_not_called()
