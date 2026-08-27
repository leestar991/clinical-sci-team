"""``analyze_run_timing.py`` — the wall-clock/failure half of run analysis.

Pins the judgements that were made by hand during the ``a7c19ea1`` review and got the
diagnosis right, so the next review starts from them instead of rediscovering them:

* Busy time is a **union** of call windows, never a sum — concurrent calls would otherwise
  total more than the wall clock.
* Lead/subagent **overlap** is the parallelism signal. Near-zero means the lead is idling in
  its poll loop, i.e. the session is serial and dispatch is the lever.
* Latency bucketed by input size separates "context too big" from "reasoning too long".
* Tool failures must be found in **both** channels, and by more shapes than an ``Error:``
  prefix (a non-zero exit lands after the command's own stdout).
* Timestamps that are really batch-flush times must be **reported as such**, not turned into
  per-step numbers. That trap cost the earlier analysis a wrong-by-construction timeline.
"""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "analyze_run_timing.py"
TZ = timezone(timedelta(hours=8))
THREAD = "t-1"


@pytest.fixture(scope="module")
def mod():
    spec = importlib.util.spec_from_file_location("analyze_run_timing", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _utc(hour: int, minute: int, second: int = 0) -> str:
    """An ISO stamp in UTC — the database's own timezone."""
    return datetime(2026, 8, 13, hour, minute, second, tzinfo=UTC).isoformat()


def _run(run_id: str = "r-1", *, start=(8, 0), end=(9, 0), status: str = "success", tokens: int = 1000) -> dict:
    return {
        "run_id": run_id,
        "status": status,
        "created_at": _utc(*start),
        "updated_at": _utc(*end),
        "total_tokens": tokens,
        "llm_call_count": 1,
    }


def _lead_call(hour: int, minute: int, second: int = 0, *, latency_ms: int = 10_000, caller: str = "lead_agent", inp: int = 60_000, out: int = 2_000, reasoning: int = 1_500, cache: int = 50_000, tools=None) -> dict:
    return {
        "event_type": "llm.ai.response",
        "created_at": _utc(hour, minute, second),
        "metadata": {
            "caller": caller,
            "latency_ms": latency_ms,
            "usage": {
                "input_tokens": inp,
                "output_tokens": out,
                "input_token_details": {"cache_read": cache},
                "output_token_details": {"reasoning": reasoning},
            },
        },
        "content": {"tool_calls": [n if isinstance(n, dict) else {"name": n} for n in (tools or [])]},
    }


def _lead_tool_result(hour: int, minute: int, *, name: str, text: str, status: str = "success", tool_call_id: str | None = None) -> dict:
    content: dict = {"name": name, "status": status, "content": text}
    if tool_call_id is not None:
        content["tool_call_id"] = tool_call_id
    return {
        "event_type": "llm.tool.result",
        "created_at": _utc(hour, minute),
        "metadata": {},
        "content": content,
    }


def _step(task_id: str, index: int, hour: int, minute: int, second: int = 0, *, kind: str = "ai", tool_name: str | None = None, text: str = "", tool_calls: list[dict] | None = None) -> dict:
    content: dict = {"task_id": task_id, "kind": kind, "message_index": index, "text": text}
    if kind == "tool":
        content["tool_name"] = tool_name
    elif tool_calls is not None:
        content["tool_calls"] = tool_calls
    return {
        "event_type": "subagent.step",
        "created_at": _utc(hour, minute, second),
        "metadata": {"task_id": task_id, "message_index": index},
        "content": content,
    }


def _summarize(hour: int, minute: int, *, task_id: str | None = None, summary_chars: int = 1200) -> dict:
    return {
        "event_type": "middleware:summarize",
        "created_at": _utc(hour, minute),
        "metadata": {},
        "content": {"changes": {"task_id": task_id, "tokens_before": 70_000, "tokens_after": 30_000, "summary_chars": summary_chars}},
    }


# ── failure detection ────────────────────────────────────────────────────────────


class TestFailureDetection:
    @pytest.mark.parametrize(
        "text",
        [
            pytest.param("Error: old_str is not unique in /mnt/x.json", id="error-prefix"),
            pytest.param("  Error: Permission denied accessing file: /mnt/skills/a.py", id="indented"),
            pytest.param('Traceback (most recent call last):\n  File "<string>", line 1\nJSONDecodeError', id="traceback"),
            pytest.param("⛔ 未能定位章节标题\n\nExit Code: 2", id="exit-code-after-stdout"),
            pytest.param("[EX] total=46\n⛔ 未过闸的轨：['EX']\nEXIT=2", id="agent-echoed-exit"),
            pytest.param("EXIT CODE: 2\n\nStd Error:\ngate.py: error: argument --qc: expected one argument", id="argparse-usage"),
        ],
    )
    def test_real_failure_shapes(self, mod, text):
        assert mod.tool_result_failed(text) is True

    @pytest.mark.parametrize(
        "text",
        [
            pytest.param("正常内容", id="plain"),
            pytest.param("- 若退避重试后仍 `Error:`，在 result 中标 route_a_failed", id="skill-doc-quoting-error"),
            pytest.param("except json.JSONDecodeError as exc:\n    raise", id="source-read-back"),
            pytest.param("OCR batch done — 13 written, 0 failed", id="summary-mentioning-failed"),
            pytest.param("IN passed= True blocking= 0\nEXIT=0", id="exit-zero"),
            pytest.param("run with EXIT=2 to reproduce", id="exit-mid-line"),
        ],
    )
    def test_non_failures(self, mod, text):
        """False positives turn the metric into noise, which is how it stops being read."""
        assert mod.tool_result_failed(text) is False

    def test_both_channels_are_counted(self, mod):
        events = {
            "r-1": [
                _lead_tool_result(8, 10, name="bash", text="Error: Unsafe absolute paths in command: /Users"),
                _step("task-0", 1, 8, 20, kind="tool", tool_name="str_replace", text="Error: old_str is not unique"),
            ]
        }
        report = mod.analyze_timing(THREAD, [_run()], events)
        totals = report["totals"]
        assert (totals["lead_tool_failures"], totals["subagent_tool_failures"], totals["tool_failures_total"]) == (1, 1, 2)
        assert report["failures"]["by_tool"] == {"bash": 1, "str_replace": 1}

    def test_non_success_status_counts_without_an_error_payload(self, mod):
        events = {"r-1": [_lead_tool_result(8, 10, name="bash", text="", status="error")]}
        assert mod.analyze_timing(THREAD, [_run()], events)["totals"]["lead_tool_failures"] == 1


# ── busy time is a union ─────────────────────────────────────────────────────────


class TestBusyTimeAccounting:
    def test_overlapping_lead_calls_are_not_double_counted(self, mod):
        """Two 60s calls ending 30s apart overlap; the union is 90s, the sum would be 120s."""
        events = {"r-1": [_lead_call(8, 1, 0, latency_ms=60_000), _lead_call(8, 1, 30, latency_ms=60_000)]}
        totals = mod.analyze_timing(THREAD, [_run()], events)["totals"]
        assert totals["lead_llm_busy_seconds"] == 90.0

    def test_disjoint_calls_add_up(self, mod):
        events = {"r-1": [_lead_call(8, 1, 0, latency_ms=10_000), _lead_call(8, 30, 0, latency_ms=20_000)]}
        assert mod.analyze_timing(THREAD, [_run()], events)["totals"]["lead_llm_busy_seconds"] == 30.0

    def test_serial_session_reports_near_zero_overlap(self, mod):
        """The a7c19ea1 shape: the lead waits in its poll loop while a subagent works."""
        events = {
            "r-1": [
                _lead_call(8, 1, 0, latency_ms=30_000),
                _step("task-0", 1, 8, 10),
                _step("task-0", 2, 8, 20),
            ]
        }
        totals = mod.analyze_timing(THREAD, [_run()], events)["totals"]
        assert totals["lead_subagent_overlap_seconds"] == 0.0
        assert totals["subagent_busy_seconds"] == 600.0

    def test_parallel_work_reports_overlap(self, mod):
        events = {
            "r-1": [
                _lead_call(8, 15, 0, latency_ms=120_000),
                _step("task-0", 1, 8, 10),
                _step("task-0", 2, 8, 20),
            ]
        }
        assert mod.analyze_timing(THREAD, [_run()], events)["totals"]["lead_subagent_overlap_seconds"] > 0

    def test_unaccounted_is_session_minus_combined(self, mod):
        events = {"r-1": [_lead_call(8, 1, 0, latency_ms=60_000)]}
        totals = mod.analyze_timing(THREAD, [_run(start=(8, 0), end=(8, 10))], events)["totals"]
        assert totals["session_seconds"] == 600.0
        assert totals["combined_busy_seconds"] == 60.0
        assert totals["unaccounted_seconds"] == 540.0

    def test_gap_between_runs_is_reported_as_idle(self, mod):
        """Think time between a run and its follow-up is not the system being slow."""
        runs = [_run("r-1", start=(8, 0), end=(8, 10)), _run("r-2", start=(8, 25), end=(8, 30))]
        report = mod.analyze_timing(THREAD, runs, {"r-1": [], "r-2": []})
        assert report["totals"]["idle_between_runs_seconds"] == 900.0
        assert report["idle_gaps"][0]["after_run"] == "r-1"


# ── dispatch pattern & concurrency watermark ────────────────────────────────────


class TestDispatchPattern:
    def test_histogram_counts_tasks_per_reply(self, mod):
        """The plan's assumption A probe: a serial habit concentrates on 1-task replies."""
        events = {
            "r-1": [
                _lead_call(8, 1, tools=["task"]),
                _lead_call(8, 2, tools=["task", "task"]),
                _lead_call(8, 3, tools=["task", "task", "task"]),
                _lead_call(8, 4, tools=["task", "task", "task", "task", "task"]),
            ]
        }
        dispatch = mod.analyze_timing(THREAD, [_run()], events)["dispatch"]
        assert dispatch["dispatch_replies"] == 4
        assert dispatch["replies_with_1_task"] == 1
        assert dispatch["replies_with_2_tasks"] == 1
        assert dispatch["replies_with_3_tasks"] == 1
        assert dispatch["replies_with_4plus_tasks"] == 1  # over-limit collapse, not a crash

    def test_non_task_tools_do_not_count_as_dispatch(self, mod):
        events = {"r-1": [_lead_call(8, 1, tools=["read_file", "bash", "task"])]}
        dispatch = mod.analyze_timing(THREAD, [_run()], events)["dispatch"]
        assert dispatch["replies_with_1_task"] == 1
        assert dispatch["dispatch_replies"] == 1

    def test_summarizer_replies_are_excluded(self, mod):
        """The dispatch histogram describes the lead agent, not middleware callers."""
        events = {"r-1": [_lead_call(8, 1, tools=["task"], caller="summarize")]}
        assert mod.analyze_timing(THREAD, [_run()], events)["dispatch"]["dispatch_replies"] == 0


class TestConcurrencyWatermark:
    def test_disjoint_tasks_peak_at_one(self, mod):
        events = {
            "r-1": [
                _step("t-1", 1, 8, 10),
                _step("t-1", 2, 8, 20),
                _step("t-2", 1, 8, 30),
                _step("t-2", 2, 8, 40),
            ]
        }
        concurrency = mod.analyze_timing(THREAD, [_run()], events)["concurrency"]
        assert concurrency["max_concurrent"] == 1
        assert concurrency["avg_while_busy"] == 1.0

    def test_three_overlapping_tasks_peak_at_three(self, mod):
        events = {
            "r-1": [
                _step("t-1", 1, 8, 10),
                _step("t-2", 1, 8, 12),
                _step("t-3", 1, 8, 14),
                _step("t-1", 2, 8, 30),
                _step("t-2", 2, 8, 31),
                _step("t-3", 2, 8, 32),
            ]
        }
        concurrency = mod.analyze_timing(THREAD, [_run()], events)["concurrency"]
        assert concurrency["max_concurrent"] == 3
        assert sorted(concurrency["tasks_at_peak"]) == ["t-1", "t-2", "t-3"]

    def test_end_and_start_at_same_instant_do_not_double_count(self, mod):
        """A task finishing at t and one starting at t never overlap."""
        events = {
            "r-1": [
                _step("t-1", 1, 8, 10),
                _step("t-1", 2, 8, 20),
                _step("t-2", 1, 8, 20),
                _step("t-2", 2, 8, 30),
            ]
        }
        assert mod.analyze_timing(THREAD, [_run()], events)["concurrency"]["max_concurrent"] == 1

    def test_serial_dispatch_warns_only_with_enough_tasks(self, mod):
        events = {"r-1": [_step(f"t-{i}", 1, 8, i * 6) for i in range(6)] + [_step(f"t-{i}", 2, 8, i * 6 + 3) for i in range(6)]}
        report = mod.analyze_timing(THREAD, [_run()], events)
        assert "serial task dispatch" in mod.render(report)
        small = {
            "r-1": [_step("t-1", 1, 8, 10), _step("t-1", 2, 8, 20)],
        }
        assert "serial task dispatch" not in mod.render(mod.analyze_timing(THREAD, [_run()], small))


class TestPhaseMarks:
    def test_first_phase_summary_write_marks_the_boundary(self, mod):
        events = {
            "r-1": [
                _lead_call(8, 1, tools=[{"name": "write_file", "args": {"path": "/mnt/user-data/workspace/phase2_summary.json"}}]),
                _lead_call(8, 5, tools=[{"name": "write_file", "args": {"path": "/mnt/user-data/workspace/phase3_summary.json"}}]),
                _lead_call(9, 0, tools=[{"name": "write_file", "args": {"path": "/mnt/user-data/workspace/phase2_summary.json"}}]),
                _step("t-1", 1, 7, 59),  # task spans only the phase2 boundary
                _step("t-1", 2, 8, 3),
            ]
        }
        marks = mod.analyze_timing(THREAD, [_run()], events)["phase_marks"]
        assert [m["phase"] for m in marks] == ["2", "3"]  # phases in time order, duplicates collapsed
        assert marks[0]["in_flight_at_boundary"] == 1
        assert marks[1]["in_flight_at_boundary"] == 0

    def test_other_writes_are_ignored(self, mod):
        events = {
            "r-1": [
                _lead_call(8, 1, tools=[{"name": "write_file", "args": {"path": "/mnt/user-data/workspace/todos.json"}}]),
            ]
        }
        assert mod.analyze_timing(THREAD, [_run()], events)["phase_marks"] == []


# ── declared-never-executed & gate findings (f9231297 fixes) ────────────────────


class TestDeclaredUnexecuted:
    def test_task_calls_without_any_result_are_flagged(self, mod):
        """The f9231297 shape: 5 declared (3 task), only the 2 ordinary tools executed."""
        events = {
            "r-1": [
                _lead_call(
                    8,
                    2,
                    46,
                    tools=[
                        {"name": "task", "id": "call_A", "args": {}},
                        {"name": "task", "id": "call_B", "args": {}},
                        {"name": "task", "id": "call_C", "args": {}},
                        {"name": "read_file", "id": "call_D", "args": {}},
                        {"name": "bash", "id": "call_E", "args": {}},
                    ],
                ),
                _lead_tool_result(8, 2, name="read_file", text="ok", tool_call_id="call_D"),
                _lead_tool_result(8, 2, name="bash", text="-rw-r--r--", tool_call_id="call_E"),
            ]
        }
        report = mod.analyze_timing(THREAD, [_run()], events)
        assert report["totals"]["declared_unexecuted"] == 3
        assert sorted(d["name"] for d in report["declared_unexecuted"]) == ["task", "task", "task"]
        assert "declared but never executed" in mod.render(report)

    def test_subagent_steps_mark_the_task_call_executed(self, mod):
        """`task_tool` stores its tool_call_id as the subagent's task id."""
        events = {
            "r-1": [
                _lead_call(8, 1, tools=[{"name": "task", "id": "call_T", "args": {}}]),
                _step("call_T", 1, 8, 5),
                _step("call_T", 2, 8, 15),
            ]
        }
        assert mod.analyze_timing(THREAD, [_run()], events)["totals"]["declared_unexecuted"] == 0

    def test_lead_tool_result_counts_as_executed(self, mod):
        events = {
            "r-1": [
                _lead_call(8, 1, tools=[{"name": "task", "id": "call_T", "args": {}}]),
                _lead_tool_result(8, 10, name="task", text="Task Succeeded", tool_call_id="call_T"),
            ]
        }
        assert mod.analyze_timing(THREAD, [_run()], events)["totals"]["declared_unexecuted"] == 0


class TestGateFindingClassification:
    GATE_CMD = "python3 /mnt/skills/custom/eligibility-judgment/scripts/check_judgment_structure.py --workspace w --patient P --track IN"

    def test_structure_gate_verdict_is_a_finding_not_a_failure(self, mod):
        """Gates reporting problems exit non-zero by design — not a tool failure."""
        events = {
            "r-1": [
                _step("t-1", 1, 8, 1, tool_calls=[{"name": "bash", "args": {"command": self.GATE_CMD}}]),
                _step("t-1", 2, 8, 2, kind="tool", tool_name="bash", text="[P/IN] stage=draft documents={}\n⛔ 闸6 疑似漏判未清空\nEXIT:2"),
            ]
        }
        report = mod.analyze_timing(THREAD, [_run()], events)
        assert report["totals"]["subagent_tool_failures"] == 0
        assert report["totals"]["gate_findings"] == 1

    def test_gate_usage_error_stays_a_failure(self, mod):
        """The f9231297 misuse case: gate run with wrong args is a real failure."""
        events = {
            "r-1": [
                _step("t-1", 1, 8, 1, tool_calls=[{"name": "bash", "args": {"command": "python3 .../check_reason_alignment.py --ocr a b"}}]),
                _step("t-1", 2, 8, 2, kind="tool", tool_name="bash", text="usage: check_reason_alignment.py [-h] --criteria CRITERIA\nEXIT:2"),
            ]
        }
        report = mod.analyze_timing(THREAD, [_run()], events)
        assert report["totals"]["subagent_tool_failures"] == 1
        assert report["totals"]["gate_findings"] == 0

    def test_non_gate_bash_failure_is_not_a_finding(self, mod):
        events = {
            "r-1": [
                _step("t-1", 1, 8, 1, tool_calls=[{"name": "bash", "args": {"command": "python3 /tmp/x.py"}}]),
                _step("t-1", 2, 8, 2, kind="tool", tool_name="bash", text="Traceback (most recent call last):\nError: boom"),
            ]
        }
        report = mod.analyze_timing(THREAD, [_run()], events)
        assert report["totals"]["subagent_tool_failures"] == 1
        assert report["totals"]["gate_findings"] == 0

    def test_lead_gate_verdict_classified_via_call_args(self, mod):
        events = {
            "r-1": [
                _lead_call(8, 1, tools=[{"name": "bash", "id": "call_G", "args": {"command": self.GATE_CMD}}]),
                _lead_tool_result(8, 2, name="bash", status="error", text="[P/IN] stage=draft\n闸2口径=整轨\nEXIT:2", tool_call_id="call_G"),
            ]
        }
        report = mod.analyze_timing(THREAD, [_run()], events)
        assert report["totals"]["lead_tool_failures"] == 0
        assert report["totals"]["gate_findings"] == 1


# ── context-bound vs reasoning-bound ────────────────────────────────────────────


class TestLatencyAttribution:
    def test_buckets_split_by_input_size(self, mod):
        events = {
            "r-1": [
                _lead_call(8, 1, 0, latency_ms=10_000, inp=20_000),
                _lead_call(8, 5, 0, latency_ms=40_000, inp=60_000),
                _lead_call(8, 9, 0, latency_ms=80_000, inp=120_000),
            ]
        }
        buckets = mod.analyze_timing(THREAD, [_run()], events)["latency_vs_context"]
        assert [b["input_tokens_from"] for b in buckets] == [0, 50_000, 100_000]
        assert [b["avg_latency_seconds"] for b in buckets] == [10.0, 40.0, 80.0]

    def test_reasoning_share_is_surfaced(self, mod):
        """78.4% reasoning is what said "not context-bound" — trimming context buys nothing."""
        events = {"r-1": [_lead_call(8, 1, 0, out=10_000, reasoning=7_840)]}
        assert mod.analyze_timing(THREAD, [_run()], events)["latency_by_caller"]["lead_agent"]["reasoning_share"] == 78.4

    def test_callers_are_separated(self, mod):
        events = {
            "r-1": [
                _lead_call(8, 1, 0, latency_ms=20_000, caller="lead_agent"),
                _lead_call(8, 5, 0, latency_ms=30_000, caller="middleware:summarize"),
            ]
        }
        by_caller = mod.analyze_timing(THREAD, [_run()], events)["latency_by_caller"]
        assert by_caller["lead_agent"]["total_seconds"] == 20.0
        assert by_caller["middleware:summarize"]["total_seconds"] == 30.0


# ── flush-time stamps must be declared, not used ────────────────────────────────


class TestFlushTimeDetection:
    def test_collapsed_timestamps_raise_a_caveat(self, mod):
        """Pre-2026-08-13 runs batch at FLUSH_THRESHOLD=25, so stamps are flush times.

        A 127-step task once collapsed onto 7 distinct second values; deriving per-step
        timing from that is wrong by construction, not merely coarse.
        """
        events = {"r-1": [_step("task-0", i, 8, 10) for i in range(1, 13)]}
        report = mod.analyze_timing(THREAD, [_run()], events)
        assert report["caveats"], "collapsed stamps must be declared"
        assert "flush" in report["caveats"][0]
        assert "⚠" in report["tasks"][0]["timing_source"]

    def test_distinct_timestamps_are_trusted(self, mod):
        events = {"r-1": [_step("task-0", i, 8, 10 + i) for i in range(1, 8)]}
        report = mod.analyze_timing(THREAD, [_run()], events)
        assert not report["caveats"]
        assert report["tasks"][0]["timing_source"] == "run_events"

    def test_log_timing_takes_precedence(self, mod, tmp_path):
        """The log is authoritative for step wall clock; retries have no "Started" line."""
        log = tmp_path / "gateway.log"
        log.write_text(
            "2026-08-13 16:34:27 - deerflow.subagents.executor - INFO - [trace=x] Subagent general-purpose starting async execution, task_id=call_00_a-retry1, timeout=1800s\n"
            "2026-08-13 16:35:00 - deerflow.tools.builtins.task_tool - INFO - [trace=x] Task call_00_a-retry1 sent message #1/1\n"
            "2026-08-13 16:38:42 - deerflow.tools.builtins.task_tool - INFO - [trace=x] Task call_00_a-retry1 completed after 40 polls\n",
            encoding="utf-8",
        )
        parsed = mod.parse_gateway_log(log)
        assert "call_00_a-retry1" in parsed["tasks"], "retry tasks need the executor line, not 'Started background task'"

        report = mod.analyze_timing(THREAD, [_run()], {"r-1": []}, parsed)
        row = report["tasks"][0]
        assert row["timing_source"] == "gateway.log"
        assert row["wall_seconds"] == 255.0  # 16:34:27 → 16:38:42
        assert row["status"] == "completed after 40 polls"

    def test_log_window_excludes_other_threads(self, mod, tmp_path):
        """These log lines carry no thread id, so the run window is the only scoping."""
        log = tmp_path / "gateway.log"
        log.write_text(
            "2026-08-12 10:00:00 - deerflow.subagents.executor - INFO - Subagent general-purpose starting async execution, task_id=other-thread-task, timeout=1800s\n"
            "2026-08-13 16:34:27 - deerflow.subagents.executor - INFO - Subagent general-purpose starting async execution, task_id=mine, timeout=1800s\n",
            encoding="utf-8",
        )
        window = (datetime(2026, 8, 13, 16, 0, tzinfo=TZ), datetime(2026, 8, 13, 18, 0, tzinfo=TZ))
        assert set(mod.parse_gateway_log(log, window)["tasks"]) == {"mine"}


# ── compaction waste ────────────────────────────────────────────────────────────


class TestCompactionAccounting:
    def test_empty_summaries_are_counted_as_attempts(self, mod, tmp_path):
        """An empty summary costs a full model call and changes nothing."""
        log = tmp_path / "gateway.log"
        log.write_text(
            "2026-08-13 16:42:34 - deerflow.agents.middlewares.summarization_middleware - WARNING - Summary model returned no text (finish_reason='length', output_tokens=8192, reasoning_chars=21052) — compaction skipped\n",
            encoding="utf-8",
        )
        parsed = mod.parse_gateway_log(log)
        report = mod.analyze_timing(THREAD, [_run()], {"r-1": [_summarize(8, 30)]}, parsed)
        totals = report["totals"]
        assert (totals["compactions_applied"], totals["compactions_empty_summary"], totals["compaction_attempts"]) == (1, 1, 2)
        assert totals["empty_summary_share"] == 50.0
        assert report["empty_summaries"][0]["reasoning_chars"] == 21052


# ── timezone ────────────────────────────────────────────────────────────────────


def test_times_render_in_utc_plus_8(mod):
    """The database is UTC and gateway.log is UTC+8; the report matches the log."""
    report = mod.analyze_timing(THREAD, [_run(start=(8, 23, 33), end=(8, 30, 11))], {"r-1": []})
    assert report["display_timezone"] == "UTC+08:00"
    assert report["runs"][0]["created_at"].startswith("2026-08-13T16:23:33")


def test_render_names_the_serial_diagnosis(mod):
    events = {"r-1": [_lead_call(8, 1, 0, latency_ms=30_000), _step("task-0", 1, 8, 10), _step("task-0", 2, 8, 20)]}
    text = mod.render(mod.analyze_timing(THREAD, [_run()], events))
    assert "poll loop" in text and "serial" in text


def test_failures_only_mode_skips_the_timing_sections(mod):
    events = {"r-1": [_lead_call(8, 1, 0), _lead_tool_result(8, 5, name="bash", text="Error: boom")]}
    text = mod.render(mod.analyze_timing(THREAD, [_run()], events), failures_only=True)
    assert "tool failures" in text
    assert "where the wall clock went" not in text
