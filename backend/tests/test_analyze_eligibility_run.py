"""``scripts/analyze_eligibility_run.py`` 的观测口径测试（Phase 0 / Task 1）。

**为什么需要这些测试**：会话 `2d628340` 的 follow-up run 有 11 个 subagent task 全部
`completed`、真实消耗 17.2M token，但快照时 `runs.total_tokens` 列还没回写（run 收尾才落账），
脚本照实求和得到 0，于是整份报告只报了主 run 的 628k —— **漏算 17.2M 却看不出异常**。
任何基于该脚本的"优化前后对比"都会因此失真。

脚本本身**没有**按 `status` 过滤（它对 `list_by_thread` 的所有 run 行求和），也**已经**算了
`subagent_tokens_from_tasks`（来自 `subagent.end.metadata.usage`）。缺的是：把这个交叉校验
暴露出来，并在两者不一致 / run 处于非终态时**出声**。

本文件锁定四类新口径：
1. run 行求和 vs 事件派生总量不一致 → 告警（`token_accounting_warnings`）；
2. run 处于非终态（`pending` / `running`）→ 告警；
3. 空 AI 步（区分"有 tool_calls"与"纯空转"两种，前者是正常的工具调用轮次）；
4. 门禁脚本调用次数 / 工具错误步数 / failed task 数。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

THREAD = "t-1"


@pytest.fixture(scope="module")
def analyze_module():
    """按文件路径加载 ``scripts/analyze_eligibility_run.py``（不在包树内）。"""
    script_path = Path(__file__).resolve().parents[1] / "scripts/analyze_eligibility_run.py"
    assert script_path.exists(), f"missing analyze script at {script_path}"
    spec = importlib.util.spec_from_file_location("_analyze_eligibility_run_under_test", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------
# 假 store：只实现脚本用到的两个方法
# --------------------------------------------------------------------------


class FakeRunStore:
    def __init__(self, rows: list[dict]):
        self._rows = rows

    async def list_by_thread(self, thread_id: str, *, user_id=None):
        assert thread_id == THREAD
        return list(self._rows)


class FakeEventStore:
    def __init__(self, events_by_run: dict[str, list[dict]]):
        self._events = events_by_run

    async def list_events(self, thread_id, run_id, *, event_types=None, limit=None, user_id=None):
        assert thread_id == THREAD
        return list(self._events.get(run_id, []))


def _run_row(run_id: str, *, status: str = "success", total: int = 0, subagent: int = 0, lead: int = 0) -> dict:
    return {
        "run_id": run_id,
        "status": status,
        "total_tokens": total,
        "total_input_tokens": total,
        "total_output_tokens": 0,
        "lead_agent_tokens": lead,
        "subagent_tokens": subagent,
        "middleware_tokens": 0,
        "llm_call_count": 1,
        "message_count": 1,
        "created_at": "2026-08-09T12:00:00+00:00",
        "updated_at": "2026-08-09T12:10:00+00:00",
    }


def _start(run_id: str, task_id: str, seq: int, description: str = "task") -> dict:
    return {
        "seq": seq,
        "event_type": "subagent.start",
        "created_at": "2026-08-09T12:00:00+00:00",
        "content": {"task_id": task_id, "description": description},
        "metadata": {"task_id": task_id},
    }


def _ai_step(task_id: str, seq: int, *, text: str = "thinking", tool_calls: list[dict] | None = None) -> dict:
    return {
        "seq": seq,
        "event_type": "subagent.step",
        "created_at": "2026-08-09T12:01:00+00:00",
        "content": {"task_id": task_id, "kind": "ai", "text": text, "tool_calls": tool_calls or []},
        "metadata": {"task_id": task_id},
    }


def _tool_step(task_id: str, seq: int, *, tool_name: str = "read_file", text: str = "ok") -> dict:
    return {
        "seq": seq,
        "event_type": "subagent.step",
        "created_at": "2026-08-09T12:02:00+00:00",
        "content": {"task_id": task_id, "kind": "tool", "tool_name": tool_name, "text": text},
        "metadata": {"task_id": task_id},
    }


def _end(task_id: str, seq: int, *, status: str = "completed", usage: dict | None = None, error: str | None = None) -> dict:
    metadata: dict = {"task_id": task_id}
    if usage is not None:
        metadata["usage"] = usage
    content: dict = {"task_id": task_id, "status": status}
    if error is not None:
        content["error"] = error
    return {
        "seq": seq,
        "event_type": "subagent.end",
        "created_at": "2026-08-09T12:05:00+00:00",
        "content": content,
        "metadata": metadata,
    }


def _summarize(seq: int, *, task_id: str | None, is_subagent: bool = True) -> dict:
    """A ``middleware:summarize`` event in the shape ``_record_summarize_event`` writes."""
    return {
        "seq": seq,
        "event_type": "middleware:summarize",
        "created_at": "2026-08-09T12:03:00+00:00",
        "content": {
            "name": "DeerFlowSummarizationMiddleware",
            "hook": "before_model",
            "action": "compact_history",
            "changes": {
                "tokens_before": 99755,
                "tokens_after": 45598,
                "messages_summarized": 6,
                "thread_id": THREAD,
                "task_id": task_id,
                "agent_name": None if is_subagent else "eligibility-screener",
                "is_subagent": is_subagent,
            },
        },
        "metadata": {},
    }


def _analyze(analyze_module, rows: list[dict], events: dict[str, list[dict]]) -> dict:
    import asyncio

    return asyncio.run(
        analyze_module.analyze_run(
            THREAD,
            run_store=FakeRunStore(rows),
            event_store=FakeEventStore(events),
        )
    )


# --------------------------------------------------------------------------
# 1. 漏算告警：run 行为 0，但事件里有 usage
# --------------------------------------------------------------------------


def test_run_row_zero_but_events_have_usage_warns(analyze_module) -> None:
    """`2d628340` 的真实形态：11 个 task 全 completed，run 行 total_tokens 还是 0。

    脚本必须（a）把事件派生总量算出来并暴露，（b）显式告警，而不是静默报 0。
    """
    events = {
        "r-ghost": [
            _start("r-ghost", "task-0", 1),
            _end("task-0", 2, usage={"input_tokens": 900, "output_tokens": 100, "total_tokens": 1000}),
        ]
    }
    report = _analyze(analyze_module, [_run_row("r-ghost", status="success", total=0)], events)

    totals = report["totals"]
    assert totals["total_tokens"] == 0, "run 行的值照实呈现，不被悄悄替换"
    assert totals["subagent_tokens_from_tasks"] == 1000, "事件派生总量必须算出来"

    warnings = report["token_accounting_warnings"]
    assert warnings, "run 行求和 < 事件派生总量时必须告警"
    assert any("1,000" in w or "1000" in w for w in warnings), f"告警需带上事件派生总量：{warnings}"


def test_consistent_totals_produce_no_warning(analyze_module) -> None:
    events = {
        "r-1": [
            _start("r-1", "task-0", 1),
            _end("task-0", 2, usage={"total_tokens": 1000}),
        ]
    }
    report = _analyze(analyze_module, [_run_row("r-1", status="success", total=1500, subagent=1000, lead=500)], events)
    assert report["token_accounting_warnings"] == []


# --------------------------------------------------------------------------
# 2. 非终态 run 告警
# --------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["running", "pending"])
def test_nonterminal_run_status_warns(analyze_module, status: str) -> None:
    """run 未收尾时它的 token 列还没落账 —— 报告必须声明这一点。"""
    report = _analyze(analyze_module, [_run_row("r-live", status=status, total=0)], {})
    warnings = report["token_accounting_warnings"]
    assert any("r-live" in w and status in w for w in warnings), f"{status} 必须触发告警：{warnings}"


@pytest.mark.parametrize("status", ["success", "error", "timeout", "interrupted"])
def test_terminal_run_status_does_not_warn(analyze_module, status: str) -> None:
    report = _analyze(analyze_module, [_run_row("r-done", status=status, total=100)], {})
    assert report["token_accounting_warnings"] == []


# --------------------------------------------------------------------------
# 3. 空 AI 步：两种口径必须分开
# --------------------------------------------------------------------------


def test_empty_ai_steps_split_by_tool_calls(analyze_module) -> None:
    """会话分析报的 35% 空步用的是"text 为空"口径，但带 tool_calls 的空 text 是**正常**的
    工具调用轮次。混在一起会把正常行为当浪费，所以两个口径都要出。
    """
    events = {
        "r-1": [
            _start("r-1", "task-0", 1),
            _ai_step("task-0", 2, text="有内容"),
            _ai_step("task-0", 3, text="", tool_calls=[{"name": "grep", "args": {"pattern": "x"}}]),
            _ai_step("task-0", 4, text="   "),
            _ai_step("task-0", 5, text=""),
            _end("task-0", 6),
        ]
    }
    report = _analyze(analyze_module, [_run_row("r-1")], events)
    totals = report["totals"]
    assert totals["ai_steps"] == 4
    assert totals["empty_ai_steps"] == 3, "空白 text 一律计入（与会话分析口径一致）"
    assert totals["empty_ai_steps_no_tool_calls"] == 2, "纯空转（无 tool_calls）才是真正的浪费"

    task = report["tasks"][0]
    assert task["empty_ai_steps"] == 3
    assert task["empty_ai_steps_no_tool_calls"] == 2


# --------------------------------------------------------------------------
# 4. 门禁脚本调用 / 工具错误 / failed task
# --------------------------------------------------------------------------


def test_gate_script_calls_counted_from_bash_commands(analyze_module) -> None:
    """判定阶段的核心指标：同一门禁脚本在单 task 内被跑了几次。"""
    events = {
        "r-1": [
            _start("r-1", "task-0", 1),
            _ai_step(
                "task-0",
                2,
                tool_calls=[{"name": "bash", "args": {"command": "python3 /mnt/skills/custom/eligibility-judgment/scripts/uncertain_recheck.py --criteria a.json"}}],
            ),
            _ai_step("task-0", 3, tool_calls=[{"name": "bash", "args": {"command": "python3 scripts/uncertain_recheck.py --out b.json"}}]),
            _ai_step("task-0", 4, tool_calls=[{"name": "bash", "args": {"command": "python3 scripts/check_reason_alignment.py"}}]),
            _ai_step("task-0", 5, tool_calls=[{"name": "bash", "args": {"command": "ls -la /mnt/user-data"}}]),
            _end("task-0", 6),
        ]
    }
    report = _analyze(analyze_module, [_run_row("r-1")], events)
    assert report["totals"]["gate_script_calls"] == {
        "check_reason_alignment.py": 1,
        "uncertain_recheck.py": 2,
    }, "只统计 .py 脚本，不含普通 shell 命令"
    assert report["totals"]["gate_script_call_total"] == 3
    assert report["tasks"][0]["gate_script_calls"] == {"check_reason_alignment.py": 1, "uncertain_recheck.py": 2}


def test_gate_script_calls_survive_truncated_args(analyze_module) -> None:
    """``_bounded_tool_call`` 会把超长 args 序列化成字符串（``args_truncated``），
    统计不能因此失效。"""
    events = {
        "r-1": [
            _start("r-1", "task-0", 1),
            _ai_step(
                "task-0",
                2,
                tool_calls=[{"name": "bash", "args": '{"command": "python3 scripts/uncertain_recheck.py --criteria', "args_truncated": True}],
            ),
            _end("task-0", 3),
        ]
    }
    report = _analyze(analyze_module, [_run_row("r-1")], events)
    assert report["totals"]["gate_script_calls"] == {"uncertain_recheck.py": 1}


def test_tool_error_steps_counted(analyze_module) -> None:
    """42 次工具误用的可度量化：工具结果以 ``Error:`` 开头即计为错误步。"""
    events = {
        "r-1": [
            _start("r-1", "task-0", 1),
            _tool_step("task-0", 2, tool_name="grep", text="Error: Path is not a directory: /mnt/x.md"),
            _tool_step("task-0", 3, tool_name="glob", text="  Error: Permission denied: /mnt/user-data"),
            _tool_step("task-0", 4, tool_name="read_file", text="正常内容"),
            _end("task-0", 5),
        ]
    }
    report = _analyze(analyze_module, [_run_row("r-1")], events)
    assert report["totals"]["tool_error_steps"] == 2
    assert report["tasks"][0]["tool_error_steps"] == 2


def _lead_tool_result(seq: int, *, name: str, text: str, status: str = "success") -> dict:
    """A lead-agent ``llm.tool.result`` event, as ``RunJournal`` persists it."""
    return {
        "seq": seq,
        "event_type": "llm.tool.result",
        "created_at": "2026-08-13T09:50:00+00:00",
        "content": {"type": "tool", "name": name, "status": status, "content": text},
        "metadata": {},
    }


# ── 失败检测口径（会话 a7c19ea1：报 4，实际 15）──────────────────────────────
#
# 旧口径只匹配「以 `Error:` 开头」且只扫子代理通道，于是漏掉：bash 非零退出（工具先回显
# 脚本自身输出、退出行在后）、Python traceback、argparse 用法转储，以及 **全部** lead 侧失败。


class TestToolFailureDetection:
    @pytest.mark.parametrize(
        "text",
        [
            pytest.param("Error: old_str is not unique in /mnt/x.json", id="error-prefix"),
            pytest.param("  Error: Permission denied accessing file: /mnt/skills/a.py", id="indented-error"),
            pytest.param('Traceback (most recent call last):\n  File "<string>", line 1\njson.decoder.JSONDecodeError: Extra data', id="traceback"),
            pytest.param("⛔ 未能在 试验方案.md 正文中定位章节标题\n\nExit Code: 2", id="exit-code-after-output"),
            pytest.param("[EX] total=46 原条号=20\n⛔ 未过闸的轨：['EX']\nEXIT=2", id="agent-echoed-exit"),
            pytest.param("EXIT CODE: 2\n\nStd Error:\ncheck_track_structure.py: error: argument --qc: expected one argument", id="argparse-usage"),
        ],
    )
    def test_real_failure_shapes_are_detected(self, analyze_module, text: str) -> None:
        assert analyze_module._tool_result_failed(text) is True

    @pytest.mark.parametrize(
        "text",
        [
            pytest.param("正常内容", id="plain-output"),
            pytest.param("- **不要自行降级**：若技能规定的退避重试后仍 `Error:`，在 result 中标 route_a_failed", id="skill-doc-quoting-error"),
            pytest.param("except json.JSONDecodeError as exc:\n    raise", id="source-code-read-back"),
            pytest.param("OCR batch done — 13 written, 0 repaired, 0 skipped, 0 failed", id="summary-mentioning-failed"),
            pytest.param("IN passed= True round= 2 blocking= 0\nEXIT=0", id="successful-exit-zero"),
            pytest.param("run with EXIT=2 to reproduce", id="exit-mentioned-mid-line"),
        ],
    )
    def test_non_failures_are_not_flagged(self, analyze_module, text: str) -> None:
        """假阳性同样有害：它会把「工具误用」指标变成噪声，没人再看。"""
        assert analyze_module._tool_result_failed(text) is False

    def test_lead_tool_results_are_counted(self, analyze_module) -> None:
        """lead 通道此前完全没被读取 —— a7c19ea1 的 15 处里有 9 处是 lead 的。"""
        events = {
            "r-1": [
                _lead_tool_result(1, name="bash", text="Error: Unsafe absolute paths in command: /Users/."),
                _lead_tool_result(2, name="apply_json_patches", text="Error: Permission denied accessing file: /mnt/skills/x.py"),
                _lead_tool_result(3, name="read_file", text="正常内容"),
            ]
        }
        totals = _analyze(analyze_module, [_run_row("r-1")], events)["totals"]
        assert totals["lead_tool_error_results"] == 2
        assert totals["lead_tool_errors_by_tool"] == {"apply_json_patches": 1, "bash": 1}

    def test_non_success_status_counts_even_without_an_error_payload(self, analyze_module) -> None:
        events = {"r-1": [_lead_tool_result(1, name="bash", text="", status="error")]}
        assert _analyze(analyze_module, [_run_row("r-1")], events)["totals"]["lead_tool_error_results"] == 1

    def test_total_sums_both_channels(self, analyze_module) -> None:
        events = {
            "r-1": [
                _start("r-1", "task-0", 1),
                _tool_step("task-0", 2, tool_name="bash", text="Error: boom"),
                _end("task-0", 3),
                _lead_tool_result(4, name="bash", text="Error: Unsafe absolute paths in command: /Users"),
            ]
        }
        totals = _analyze(analyze_module, [_run_row("r-1")], events)["totals"]
        assert (totals["tool_error_steps"], totals["lead_tool_error_results"], totals["tool_error_total"]) == (1, 1, 2)


# ── 闸：执行 vs 读源码（a7c19ea1 混报 56，实际 exec 47 / read_source 9）────────


class TestGateExecVersusInspect:
    def test_running_a_gate_counts_as_exec(self, analyze_module) -> None:
        cmd = "python3 /mnt/skills/custom/criteria-parser/scripts/check_track_structure.py --workspace /mnt/user-data/workspace --track EX"
        assert analyze_module._gate_script_execs_in_command(cmd) == ["check_track_structure.py"]
        assert analyze_module._gate_script_inspects_in_command(cmd) == []

    def test_grep_with_alternation_inside_its_quoted_pattern_counts_as_inspect(self, analyze_module) -> None:
        """真实命令：``\\|`` 在 grep 的引号内 —— 按 shell 分隔符切会永远到不了文件名。"""
        cmd = 'grep -n "闸10\\|闸 10\\|upstream\\|中性化" /mnt/skills/custom/criteria-parser/scripts/check_track_structure.py | head -40'
        assert analyze_module._gate_script_inspects_in_command(cmd) == ["check_track_structure.py"]
        assert analyze_module._gate_script_execs_in_command(cmd) == []

    def test_sed_range_read_counts_as_inspect(self, analyze_module) -> None:
        cmd = "sed -n '1,80p' /mnt/skills/custom/criteria-parser/scripts/check_track_structure.py"
        assert analyze_module._gate_script_inspects_in_command(cmd) == ["check_track_structure.py"]

    def test_exec_then_inspect_in_one_command_counts_both(self, analyze_module) -> None:
        cmd = "python3 scripts/gate.py --track EX || grep -n 'def _check' scripts/gate.py"
        assert analyze_module._gate_script_execs_in_command(cmd) == ["gate.py"]
        assert analyze_module._gate_script_inspects_in_command(cmd) == ["gate.py"]

    def test_a_filename_after_an_unrelated_command_is_not_an_inspect(self, analyze_module) -> None:
        cmd = "echo done && python3 scripts/gate.py"
        assert analyze_module._gate_script_inspects_in_command(cmd) == []

    def test_totals_report_both_dimensions(self, analyze_module) -> None:
        events = {
            "r-1": [
                _start("r-1", "task-0", 1),
                _ai_step("task-0", 2, tool_calls=[{"name": "bash", "args": {"command": "python3 scripts/gate.py --track EX"}}]),
                _ai_step("task-0", 3, tool_calls=[{"name": "bash", "args": {"command": "grep -n 'def _check' scripts/gate.py"}}]),
                _end("task-0", 4),
            ]
        }
        totals = _analyze(analyze_module, [_run_row("r-1")], events)["totals"]
        assert totals["gate_script_exec_total"] == 1
        assert totals["gate_script_inspect_total"] == 1
        assert totals["gate_script_call_total"] == 2, "混合口径保留以兼容既有基线"


def test_failed_tasks_counted(analyze_module) -> None:
    events = {
        "r-1": [
            _start("r-1", "task-0", 1),
            _end("task-0", 2, status="failed", usage={"total_tokens": 6_362_702}),
            _start("r-1", "task-1", 3),
            _end("task-1", 4, status="completed", usage={"total_tokens": 5_210_668}),
        ]
    }
    report = _analyze(analyze_module, [_run_row("r-1", total=11_573_370)], events)
    assert report["totals"]["failed_tasks"] == 1
    assert report["totals"]["task_count"] == 2


# --------------------------------------------------------------------------
# 5. 基线对比与渲染
# --------------------------------------------------------------------------


def test_new_metrics_are_compared_against_baseline(analyze_module) -> None:
    for key in ("empty_ai_steps", "gate_script_call_total", "failed_tasks", "tool_error_steps", "subagent_tokens_from_tasks"):
        assert key in analyze_module.COMPARED_METRICS, f"{key} 必须进 baseline 对比"

    delta = analyze_module.compare(
        {"totals": {"failed_tasks": 0, "empty_ai_steps": 10}},
        {"totals": {"failed_tasks": 2, "empty_ai_steps": 20}},
    )
    assert delta["failed_tasks"]["delta"] == -2
    assert delta["empty_ai_steps"]["pct"] == -50.0


def test_render_surfaces_cross_check_and_warnings(analyze_module) -> None:
    events = {
        "r-ghost": [
            _start("r-ghost", "task-0", 1),
            _ai_step("task-0", 2, text=""),
            _tool_step("task-0", 3, text="Error: Permission denied: /mnt/user-data"),
            _end("task-0", 4, usage={"total_tokens": 17_244_469}),
        ]
    }
    report = _analyze(analyze_module, [_run_row("r-ghost", status="running", total=0)], events)
    rendered = analyze_module._render(report, None)

    assert "from_tasks" in rendered, "交叉校验值必须打印，而不是只躺在 JSON 里"
    assert "17,244,469" in rendered
    assert "⚠" in rendered and "r-ghost" in rendered, "告警必须出现在人读的输出里"
    assert "empty_ai" in rendered
    assert "tool_errors=1" in rendered


# --------------------------------------------------------------------------
# 读取口径分档（会话 `93d8a2c6` 复盘）
# --------------------------------------------------------------------------
#
# 旧口径 `duplicate_read_calls = 路径引用数 - 唯一路径数` 把三件互不相干的事混成一个数：
#   ① 版本缓存能抑制的重复（同 task + 同 path + **同 range**）；
#   ② 换个窗口重发的行（范围重叠）；
#   ③ **必须**返回正文的跨 task 首读（子代理上下文隔离的要求）。
# 该会话它报 170，而真正可去重的只有 6 —— 照着它做了一整轮优化，收益 -3.8%。
# 现在拆成 `dedupable_read_calls` 与 `range_overlap_lines`，各自对应一种修法。


def _read(task_id: str, seq: int, path: str, *, start: int | None = None, end: int | None = None) -> dict:
    args: dict = {"path": path}
    if start is not None:
        args["start_line"] = start
    if end is not None:
        args["end_line"] = end
    return _ai_step(task_id, seq, text="", tool_calls=[{"name": "read_file", "args": args}])


OCR = "/mnt/user-data/workspace/patients/P1/ocr/exam/ocr_records.md"


def test_same_range_reread_in_one_task_is_dedupable(analyze_module):
    events = {
        "r-1": [
            _read("task-0", 1, OCR, start=100, end=200),
            _read("task-0", 2, OCR, start=100, end=200),
            _end("task-0", 3),
        ]
    }
    t = _analyze(analyze_module, [_run_row("r-1")], events)["totals"]
    assert t["dedupable_read_calls"] == 1, "同 task 同 path 同 range 的第二次读才是可去重的"
    assert t["range_overlap_lines"] == 101, "同一段读两遍，重叠行数就是那一段的长度"


def test_different_ranges_of_one_file_are_not_dedupable(analyze_module):
    """70% 的读带行范围且范围各不相同 —— 这些是**合法 miss**，不是浪费。"""
    events = {
        "r-1": [
            _read("task-0", 1, OCR, start=1, end=100),
            _read("task-0", 2, OCR, start=200, end=300),
            _read("task-0", 3, OCR, start=400, end=500),
            _end("task-0", 4),
        ]
    }
    t = _analyze(analyze_module, [_run_row("r-1")], events)["totals"]
    assert t["dedupable_read_calls"] == 0
    assert t["range_overlap_lines"] == 0
    assert t["ranged_read_calls"] == 3


def test_cross_task_first_reads_are_not_dedupable(analyze_module):
    """子代理上下文隔离要求每个 task 的首读都拿到正文，这不是可回收的浪费。

    ⚠️ 旧口径 `duplicate_read_calls` 在**这一维上是对的** —— 它按 task 累加后再求和，
    所以跨 task 的同路径首读本来就不计入。会话 `93d8a2c6` 的 170 全部是**同一 task 内**
    重复读同一路径（两个独立测算完全吻合）。它唯一缺的维度是 **line range**。
    """
    events = {
        "r-1": [
            _read("task-a", 1, OCR),
            _end("task-a", 2),
            _read("task-b", 3, OCR),
            _end("task-b", 4),
        ]
    }
    t = _analyze(analyze_module, [_run_row("r-1")], events)["totals"]
    assert t["dedupable_read_calls"] == 0, "跨 task 首读不得计入可去重"
    assert t["duplicate_read_calls"] == 0, "旧口径已按 task 归集，这一维不是它的问题"


def test_overlapping_windows_are_counted_as_overlap_not_as_dedupable(analyze_module):
    """`(1480,1520)` / `(1500,1530)` 这类相邻窗口 —— 缓存治不了，只有读取策略能治。"""
    events = {
        "r-1": [
            _read("task-0", 1, OCR, start=1, end=20),
            _read("task-0", 2, OCR, start=11, end=30),
            _end("task-0", 3),
        ]
    }
    t = _analyze(analyze_module, [_run_row("r-1")], events)["totals"]
    assert t["dedupable_read_calls"] == 0
    assert t["range_lines_requested"] == 40
    assert t["range_lines_distinct"] == 30
    assert t["range_overlap_lines"] == 10


def test_whole_file_and_ranged_reads_are_counted_separately(analyze_module):
    events = {
        "r-1": [
            _read("task-0", 1, OCR),
            _read("task-0", 2, OCR, start=1, end=10),
            _end("task-0", 3),
        ]
    }
    t = _analyze(analyze_module, [_run_row("r-1")], events)["totals"]
    assert t["whole_file_read_calls"] == 1
    assert t["ranged_read_calls"] == 1
    assert t["dedupable_read_calls"] == 0, "整篇 vs 范围是不同的 key，不构成可去重重复"


def test_overlap_is_measured_per_path(analyze_module):
    """两个文件的行号不得互相抵消。"""
    events = {
        "r-1": [
            _read("task-0", 1, OCR, start=1, end=10),
            _read("task-0", 2, "/mnt/user-data/workspace/other.md", start=1, end=10),
            _end("task-0", 3),
        ]
    }
    t = _analyze(analyze_module, [_run_row("r-1")], events)["totals"]
    assert t["range_overlap_lines"] == 0


def test_malformed_range_falls_back_to_whole_file(analyze_module):
    """`_int` 会把垃圾值压成 0；缺 start_line 必须读作「整篇」而不是「第 0 行」。"""
    events = {
        "r-1": [
            _ai_step("task-0", 1, text="", tool_calls=[{"name": "read_file", "args": {"path": OCR, "end_line": 50}}]),
            _ai_step("task-0", 2, text="", tool_calls=[{"name": "read_file", "args": {"path": OCR, "start_line": "x", "end_line": "y"}}]),
            _end("task-0", 3),
        ]
    }
    t = _analyze(analyze_module, [_run_row("r-1")], events)["totals"]
    assert t["whole_file_read_calls"] == 2
    assert t["range_lines_requested"] == 0
    assert t["dedupable_read_calls"] == 1, "两次都被归为「整篇读同一文件」，第二次可去重"


def test_render_separates_the_two_waste_figures(analyze_module):
    """报告不能再用一个含混的 `recoverable_duplicates` 打包两种修法。"""
    events = {
        "r-1": [
            _read("task-0", 1, OCR, start=1, end=20),
            _read("task-0", 2, OCR, start=11, end=30),
            _end("task-0", 3),
        ]
    }
    report = _analyze(analyze_module, [_run_row("r-1")], events)
    text = analyze_module._render(report, None)
    assert "dedupable=" in text and "range_overlap=" in text
    assert "recoverable_duplicates" not in text, "含混口径不得再出现在报告正文"


def test_compared_metrics_track_the_actionable_figures(analyze_module):
    assert "dedupable_read_calls" in analyze_module.COMPARED_METRICS
    assert "range_overlap_lines" in analyze_module.COMPARED_METRICS
    assert "duplicate_read_calls" not in analyze_module.COMPARED_METRICS, "含混口径的 delta 没有意义，不该进对比表"


def test_metric_absent_from_baseline_is_not_reported_as_zero(analyze_module):
    """老基线没有新口径时，必须显示「无此口径」，不能显示成 `0 -> 9`。

    `0 -> 9` 读起来像"从零涨到九"的回归，而真相是这份基线根本没测过这个数。
    自信地给出一个错数，正是这张对比表本身要防的事。
    """
    events = {"r-1": [_read("task-0", 1, OCR, start=1, end=20), _read("task-0", 2, OCR, start=1, end=20), _end("task-0", 3)]}
    current = _analyze(analyze_module, [_run_row("r-1")], events)
    stale_baseline = {"totals": {"total_tokens": 100, "ai_steps": 2}}  # 老格式：无 dedupable_read_calls
    delta = analyze_module.compare(current, stale_baseline)
    assert delta["dedupable_read_calls"]["baseline"] is None
    assert delta["dedupable_read_calls"]["delta"] is None
    assert delta["dedupable_read_calls"]["note"] == "baseline 无此口径"
    text = analyze_module._render(current, delta)
    assert "baseline 无此口径" in text
    assert "0 ->            1" not in text, "不得把缺失口径渲染成从 0 起算"


# --------------------------------------------------------------------------
# 三个护栏口径（thread 88df83a8:整份复读 → 压缩 → 子代理丢工作状态 → 产物缺失）
# --------------------------------------------------------------------------


class TestGuardMetrics:
    """这三个数是一条因果链,所以放在一起报:
    整份复读把任务上下文吹大 → 压缩触发 → 丢了工作状态的子代理不再产出被派的产物。
    """

    def test_whole_file_rereads_count_only_repeat_whole_reads(self, analyze_module):
        events = {
            "r-1": [
                _start("r-1", "task-0", 1),
                _read("task-0", 2, OCR),  # 首次整份读:不算复读
                _read("task-0", 3, OCR),  # 第 2 次整份读:算 1
                _read("task-0", 4, OCR),  # 第 3 次:算 2
                _read("task-0", 5, OCR, start=100, end=200),  # 带区间:永不计入
                _read("task-0", 6, "/mnt/user-data/workspace/other.json"),  # 别的路径首读
                _end("task-0", 7),
            ]
        }
        report = _analyze(analyze_module, [_run_row("r-1")], events)
        assert report["totals"]["whole_file_reread_calls"] == 2
        task = report["tasks"][0]
        assert task["whole_file_reread_paths"] == {OCR: 3}

    def test_compactions_are_attributed_per_task_and_lead_kept_separate(self, analyze_module):
        events = {
            "r-1": [
                _start("r-1", "task-0", 1),
                _summarize(2, task_id="task-0"),
                _summarize(3, task_id="task-0"),
                _end("task-0", 4),
                _start("r-1", "task-1", 5),
                _summarize(6, task_id="task-1"),
                _end("task-1", 7),
                # lead 压缩:task_id=None,不能记到任何 task 头上
                _summarize(8, task_id=None, is_subagent=False),
            ]
        }
        report = _analyze(analyze_module, [_run_row("r-1")], events)
        assert report["totals"]["subagent_compactions"] == 3
        assert report["totals"]["lead_compactions"] == 1
        by_task = {t["task_id"]: t["compactions"] for t in report["tasks"]}
        assert by_task == {"task-0": 2, "task-1": 1}

    def test_artifact_gate_failure_is_distinguishable_from_an_ordinary_failure(self, analyze_module):
        marker = analyze_module._ARTIFACT_GATE_MARKER
        events = {
            "r-1": [
                _start("r-1", "gate-fail", 1),
                _end("gate-fail", 2, status="failed", error=f"Subagent reported completion but {marker}. missing=['/mnt/user-data/workspace/x.json']"),
                _start("r-1", "other-fail", 3),
                _end("other-fail", 4, status="failed", error="Recursion limit of 150 reached"),
                _start("r-1", "ok", 5),
                _end("ok", 6),
            ]
        }
        report = _analyze(analyze_module, [_run_row("r-1")], events)
        assert report["totals"]["failed_tasks"] == 2, "两个都是 failed"
        assert report["totals"]["artifact_gate_failures"] == 1, "只有一个是产物闸失败"
        flags = {t["task_id"]: t["artifact_gate_failed"] for t in report["tasks"]}
        assert flags == {"gate-fail": True, "other-fail": False, "ok": False}

    def test_guard_metrics_are_in_the_comparison_table_and_the_report(self, analyze_module):
        for key in ("subagent_compactions", "whole_file_reread_calls", "artifact_gate_failures"):
            assert key in analyze_module.COMPARED_METRICS, key

        events = {"r-1": [_start("r-1", "task-0", 1), _summarize(2, task_id="task-0"), _read("task-0", 3, OCR), _read("task-0", 4, OCR), _end("task-0", 5)]}
        report = _analyze(analyze_module, [_run_row("r-1")], events)
        text = analyze_module._render(report, None)
        assert "guards" in text
        assert "whole_file_rereads=1" in text
        assert "subagent_compactions=1" in text
        assert "artifact_gate_failures=0" in text
