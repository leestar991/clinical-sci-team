#!/Volumes/data/github/clinical-sci-team/backend/.venv/bin/python
"""
batch_report_aacr_monitor.py — 监控 batch_report_aacr.py 运行状态。

用法（由 batch_report_aacr.py 自动启动，也可独立运行）：
  python batch_report_aacr_monitor.py --thread THREAD_ID --task-name TASK_NAME [--pid PID]

输出：
  /tmp/batch_report_aacr_monitor.log  — 结构化监控日志
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from collections import deque
from pathlib import Path

import httpx

# ── 配置（与主脚本保持一致）──────────────────────────────────────────────────
BASE_URL     = "http://127.0.0.1:2026"
THREADS_BASE = Path("/Volumes/data/github/clinical-sci-team/backend/.deer-flow/threads")

MONITOR_LOG_FILE  = "/tmp/batch_report_aacr_monitor.log"
INTERVAL          = 90    # 轮询间隔（秒）
STALL_THRESHOLD   = 300   # 连续 step 不变超过此秒数判定为 stall（5分钟）
MAX_RUNTIME       = 5400  # 最长运行时间（秒，90分钟）
HISTORY_WINDOW    = 15    # 每次拉取最近 N 个 checkpoint 用于深度分析
LOOP_TOOL_WINDOW  = 10    # 检测循环时看最近 N 次工具调用序列

# ── 日志 ─────────────────────────────────────────────────────────────────────
_fmt = logging.Formatter("%(asctime)s [MONITOR] %(message)s")
_fh  = logging.FileHandler(MONITOR_LOG_FILE, mode="a", encoding="utf-8")
_fh.setFormatter(_fmt)
logging.root.setLevel(logging.INFO)
logging.root.addHandler(_fh)
log = logging.getLogger("monitor")


# ── API 查询 ─────────────────────────────────────────────────────────────────

def fetch_state(thread_id: str) -> dict:
    """拉取最新 state（单个 checkpoint，高效）。"""
    try:
        r = httpx.get(
            f"{BASE_URL}/api/langgraph/threads/{thread_id}/state",
            timeout=15,
        )
        if r.status_code != 200:
            return {}
        return json.loads(r.text, strict=False)
    except Exception as e:
        log.warning("fetch_state 失败: %s", e)
        return {}


def fetch_history(thread_id: str, limit: int = HISTORY_WINDOW) -> list[dict]:
    """拉取最近 N 个 checkpoint 列表（用于深度分析）。"""
    try:
        r = httpx.post(
            f"{BASE_URL}/api/langgraph/threads/{thread_id}/history",
            json={"limit": limit},
            timeout=30,
        )
        if r.status_code != 200:
            return []
        data = json.loads(r.text, strict=False)
        return data if isinstance(data, list) else []
    except Exception as e:
        log.warning("fetch_history 失败: %s", e)
        return []


# ── Checkpoint 解析 ───────────────────────────────────────────────────────────

def parse_checkpoint(cp: dict) -> dict:
    """从单个 checkpoint 提取所有监控关键字段。"""
    meta   = cp.get("metadata", {})
    vals   = cp.get("values", {})
    msgs   = vals.get("messages", [])
    todos  = vals.get("todos", [])
    tasks  = cp.get("tasks", [])   # LangGraph middleware-level pending tasks
    nxt    = cp.get("next", [])

    # ── Step 计数器（真实进度指标）──
    step = meta.get("step", -1)

    # ── Middleware 层 pending 任务（result=None 且 error=None）──
    # 注意：这是 LangGraph middleware 内部任务，不是 'task' 工具调用的子 agent
    mw_pending = sum(1 for t in tasks if t.get("result") is None and t.get("error") is None)

    # ── 当前 middleware 位置 ──
    current_node = nxt[0] if nxt else "(idle)"

    # ── 并行工具调用数（next 中出现多个 'tools'）──
    parallel_tools = nxt.count("tools") if isinstance(nxt, list) else 0

    # ── 消息分析 ──
    msg_count   = len(msgs)
    last_tool   = ""
    last_ai_text = ""
    tool_calls_in_last_ai: list[str] = []  # 最后一条 ai 消息里的 tool_use 名称列表
    ask_clarification_detected = False
    summarization_detected = False

    for m in reversed(msgs):
        mtype   = m.get("type", "")
        content = m.get("content", "")
        name    = m.get("name", "")

        # 最新工具结果
        if mtype == "tool" and not last_tool:
            last_tool = name or "(unknown)"
            # 检测 ask_clarification
            if name == "ask_clarification":
                ask_clarification_detected = True

        # 最新 ai 消息
        if mtype == "ai" and not last_ai_text:
            if isinstance(content, list):
                texts = []
                for c in content:
                    if isinstance(c, dict):
                        if c.get("type") == "text":
                            texts.append(c.get("text", ""))
                        elif c.get("type") == "tool_use":
                            tool_calls_in_last_ai.append(c.get("name", "unknown"))
                last_ai_text = " ".join(t for t in texts if t).strip()[:200]
            elif isinstance(content, str):
                last_ai_text = content.strip()[:200]

    # ── Summarization 事件检测（msg 数骤降）── 在历史分析中处理，此处记录 msg_count
    # ── checkpoint_id（用于检测 step 真正冻结）──
    checkpoint_id = cp.get("checkpoint_id", "")

    # ── Todos ──
    done_n  = sum(1 for t in todos if t.get("status") == "completed")
    ip_n    = sum(1 for t in todos if t.get("status") == "in_progress")
    total_n = len(todos)

    return {
        "step":              step,
        "checkpoint_id":     checkpoint_id,
        "current_node":      current_node,
        "parallel_tools":    parallel_tools,   # next 中有几个 'tools'
        "mw_pending":        mw_pending,        # LangGraph middleware pending tasks
        "msg_count":         msg_count,
        "tool_calls_in_ai":  tool_calls_in_last_ai,  # 最新 ai 消息的工具调用列表
        "last_tool":         last_tool,
        "last_ai_text":      last_ai_text,
        "ask_clarification": ask_clarification_detected,
        "todos":             todos,
        "todo_done":         done_n,
        "todo_ip":           ip_n,
        "todo_total":        total_n,
    }


def analyze_history(checkpoints: list[dict]) -> dict:
    """跨多个 checkpoint 的深度分析：循环检测、Summarization 检测、step 速率。"""
    if len(checkpoints) < 2:
        return {}

    steps       = [cp.get("metadata", {}).get("step", -1) for cp in checkpoints]
    msg_counts  = [len(cp.get("values", {}).get("messages", [])) for cp in checkpoints]

    # ── Step 速率（最新 step 与最早 step 之差除以 checkpoint 数）──
    step_delta = steps[0] - steps[-1]   # checkpoints 是倒序的（最新在前）

    # ── Summarization 事件（msg 数骤降：后一个比前一个多 ≥3）──
    # 倒序列表中 index i 比 index i+1 更新，所以 msg_counts[i+1] > msg_counts[i]+2 = 骤降
    summ_events = 0
    for i in range(len(msg_counts) - 1):
        if msg_counts[i + 1] - msg_counts[i] >= 3:   # 旧的 msgs 多于新的（骤降）
            summ_events += 1

    # ── 工具循环检测（提取每个 checkpoint 的 last_tool，检测重复序列）──
    last_tools = []
    for cp in checkpoints:
        msgs = cp.get("values", {}).get("messages", [])
        for m in reversed(msgs):
            if m.get("type") == "tool" and m.get("name"):
                last_tools.append(m["name"])
                break

    # 检测最近 LOOP_TOOL_WINDOW 个工具中是否有同名工具出现 ≥3 次
    recent = last_tools[:LOOP_TOOL_WINDOW]
    loop_tools: list[str] = []
    from collections import Counter
    cnt = Counter(recent)
    for tool, count in cnt.most_common():
        if count >= 3:
            loop_tools.append(f"{tool}×{count}")

    # ── 检测 ask_clarification（任意 checkpoint 的消息里出现）──
    ask_clar_step = None
    for cp in checkpoints:
        msgs = cp.get("values", {}).get("messages", [])
        for m in msgs:
            if m.get("type") == "tool" and m.get("name") == "ask_clarification":
                ask_clar_step = cp.get("metadata", {}).get("step")
                break
        if ask_clar_step is not None:
            break

    return {
        "step_delta":    step_delta,
        "summ_events":   summ_events,
        "loop_tools":    loop_tools,
        "ask_clar_step": ask_clar_step,
    }


# ── 文件系统检查 ──────────────────────────────────────────────────────────────

def check_workspace(thread_id: str, task_name: str) -> dict:
    """检查 thread workspace 中各类中间产出的数量/存在性。"""
    base = THREADS_BASE / thread_id / "user-data"
    ws   = base / "workspace"

    doc_count    = len(list((ws / "doc_summaries").glob("*.md"))) if (ws / "doc_summaries").exists() else 0
    search_count = len(list((ws / "search_results").glob("*.md"))) if (ws / "search_results").exists() else 0
    has_master   = (ws / "master_summary.md").exists()
    plan_files   = list(ws.glob("*_plan.md"))
    output_files = list((base / "outputs").glob("*.md")) if (base / "outputs").exists() else []

    return {
        "doc_summaries":  doc_count,
        "search_results": search_count,
        "master_summary": has_master,
        "plan_file":      plan_files[0].name if plan_files else None,
        "output_files":   [f.name for f in output_files],
    }


# ── 主监控循环 ────────────────────────────────────────────────────────────────

def run_monitor(thread_id: str, task_name: str, parent_pid: int | None) -> None:
    log.info("=" * 60)
    log.info("监控启动  thread=%s  task=%s  pid=%s", thread_id, task_name, parent_pid or "N/A")
    log.info("=" * 60)

    start_time      = time.time()
    prev_step       = -1
    stall_since     = 0.0
    prev_checkpoint = ""

    while True:
        time.sleep(INTERVAL)

        now     = time.time()
        elapsed = int(now - start_time)

        # ── 检查主进程是否还活着 ──
        if parent_pid:
            try:
                os.kill(parent_pid, 0)
            except ProcessLookupError:
                log.info("[%ds] 主进程 PID=%d 已结束，执行最终检查", elapsed, parent_pid)
                _final_check(thread_id, task_name, elapsed)
                break

        # ── 拉取最新 state（轻量）──
        state_raw = fetch_state(thread_id)
        if not state_raw:
            log.warning("[%ds] 无法获取 thread state", elapsed)
            continue

        cp_basic = parse_checkpoint(state_raw)
        ws       = check_workspace(thread_id, task_name)

        step           = cp_basic["step"]
        checkpoint_id  = cp_basic["checkpoint_id"]
        current_node   = cp_basic["current_node"]
        parallel_tools = cp_basic["parallel_tools"]
        mw_pending     = cp_basic["mw_pending"]
        msg_count      = cp_basic["msg_count"]
        last_tool      = cp_basic["last_tool"]
        ask_clar       = cp_basic["ask_clarification"]
        todo_done      = cp_basic["todo_done"]
        todo_ip        = cp_basic["todo_ip"]
        todo_total     = cp_basic["todo_total"]
        todos          = cp_basic["todos"]
        tool_calls_ai  = cp_basic["tool_calls_in_ai"]

        # ── 拉取历史（每次都拉，用于深度分析）──
        history = fetch_history(thread_id, limit=HISTORY_WINDOW)
        hist    = analyze_history(history) if history else {}

        summ_events   = hist.get("summ_events", 0)
        loop_tools    = hist.get("loop_tools", [])
        hist_ask_clar = hist.get("ask_clar_step")
        step_delta    = hist.get("step_delta", 0)

        # ── 判断是否有并行子任务（parallel_tools > 1 或 mw_pending > 1）──
        has_parallel = parallel_tools > 1 or mw_pending > 1

        # ── 格式化 todos ──
        todo_lines = []
        for t in todos:
            icon = {"completed": "✅", "in_progress": "🔄", "pending": "⬜"}.get(t.get("status", ""), "❓")
            todo_lines.append(f"    {icon} {t.get('content', '')[:65]}")

        # ── 打印主状态行 ──
        log.info(
            "[%ds] step=%d(Δ%d)  node=%-40s  msgs=%d  parallel=%d  mw_pending=%d",
            elapsed, step, step_delta, current_node[:40], msg_count, parallel_tools, mw_pending,
        )
        log.info(
            "         todos=%d/%d(🔄%d)  plan=%s  docs=%d  search=%d  master=%s  outputs=%d",
            todo_done, todo_total, todo_ip,
            "✅" if ws["plan_file"] else "⬜",
            ws["doc_summaries"],
            ws["search_results"],
            "✅" if ws["master_summary"] else "⬜",
            len(ws["output_files"]),
        )

        # ── 深度分析行 ──
        if tool_calls_ai:
            log.info("         ai_tool_calls=%s", tool_calls_ai[:5])
        if last_tool:
            log.info("         last_tool=%-20s  last_ai=%s", last_tool, cp_basic["last_ai_text"][:80])
        if summ_events:
            log.info("         📦 SUMM事件=%d（近%d个checkpoint内消息数骤降）", summ_events, HISTORY_WINDOW)
        if loop_tools:
            log.info("         🔁 工具循环警告: %s", "  ".join(loop_tools))

        # ── Todos 列表 ──
        for line in todo_lines:
            log.info(line)

        # ── ask_clarification 检测 ──
        if ask_clar or hist_ask_clar is not None:
            step_ref = hist_ask_clar or step
            log.warning(
                "🚨 ask_clarification 检测！step=%d — thread 可能已被中断，等待重新注入上下文",
                step_ref,
            )

        # ── 输出文件出现 → 成功 ──
        if ws["output_files"]:
            log.info("✅ 输出文件已生成: %s", ws["output_files"])
            break

        # ── Stall 检测（基于 step 计数器，并行时豁免）──
        # 使用 step 而非 msg_count：step 是 LangGraph 单调递增的，更可靠
        if step == prev_step and checkpoint_id == prev_checkpoint:
            # step 完全没动
            if not has_parallel:
                if stall_since == 0:
                    stall_since = now
                stall_dur = int(now - stall_since)
                if stall_dur >= STALL_THRESHOLD:
                    log.warning(
                        "⚠️  STALL 检测: step=%d 已 %ds 未变化（阈值 %ds，无并行任务）",
                        step, stall_dur, STALL_THRESHOLD,
                    )
                    log.warning("     node=%s  last_tool=%s", current_node, last_tool)
            else:
                if stall_since:
                    log.info("    ⚙️  并行任务运行中（parallel_tools=%d / mw_pending=%d），stall 计时暂停", parallel_tools, mw_pending)
                stall_since = 0
        else:
            stall_since = 0

        prev_step       = step
        prev_checkpoint = checkpoint_id

        # ── 超时保护 ──
        if elapsed > MAX_RUNTIME:
            log.error("⏰ 超时 %ds（最大 %ds），监控退出", elapsed, MAX_RUNTIME)
            break

    log.info("监控结束")


def _final_check(thread_id: str, task_name: str, elapsed: int) -> None:
    """主进程退出后做一次最终状态快照（拉取完整历史）。"""
    state_raw = fetch_state(thread_id)
    history   = fetch_history(thread_id, limit=HISTORY_WINDOW)
    ws        = check_workspace(thread_id, task_name)
    hist      = analyze_history(history) if history else {}

    log.info("─" * 50)
    log.info("最终状态快照 (elapsed=%ds)", elapsed)

    if state_raw:
        cp = parse_checkpoint(state_raw)
        log.info("  step=%d  node=%s  msgs=%d", cp["step"], cp["current_node"], cp["msg_count"])
        log.info("  todos=%d/%d  last_tool=%s", cp["todo_done"], cp["todo_total"], cp["last_tool"])
        if cp["ask_clarification"]:
            log.warning("  🚨 最终状态: ask_clarification 已触发，thread 中断")

    if hist:
        if hist.get("loop_tools"):
            log.warning("  🔁 工具循环（近%d个checkpoint）: %s", HISTORY_WINDOW, "  ".join(hist["loop_tools"]))
        if hist.get("summ_events", 0):
            log.info("  📦 Summarization 事件: %d 次", hist["summ_events"])
        if hist.get("ask_clar_step") is not None:
            log.warning("  🚨 ask_clarification 发生于 step=%d", hist["ask_clar_step"])

    log.info(
        "  plan=%s  doc_summaries=%d  search_results=%d  master_summary=%s  outputs=%d",
        ws["plan_file"] or "无",
        ws["doc_summaries"],
        ws["search_results"],
        "✅" if ws["master_summary"] else "⬜",
        len(ws["output_files"]),
    )
    if ws["output_files"]:
        log.info("  ✅ 输出文件: %s", ws["output_files"])
    else:
        log.warning("  ❌ 未找到输出文件，任务可能失败")

    # ── 打印最近 checkpoint 步骤轨迹 ──
    if history:
        log.info("  最近 %d 个 checkpoint 步骤轨迹：", len(history))
        for cp_raw in history[:10]:
            meta   = cp_raw.get("metadata", {})
            nxt    = cp_raw.get("next", [])
            cp_msg = cp_raw.get("values", {}).get("messages", [])
            last_t = ""
            for m in reversed(cp_msg):
                if m.get("type") == "tool" and m.get("name"):
                    last_t = m["name"]
                    break
            log.info(
                "    step=%-4s  next=%-45s  msgs=%d  last_tool=%s",
                meta.get("step", "?"),
                str(nxt)[:45],
                len(cp_msg),
                last_t,
            )

    log.info("─" * 50)


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="监控 batch_report_aacr.py 任务状态")
    parser.add_argument("--thread",    required=True,  help="DeerFlow thread ID")
    parser.add_argument("--task-name", required=True,  help="任务名称（md_file.stem）")
    parser.add_argument("--pid",       type=int, default=None, help="主进程 PID（用于进程退出检测）")
    args = parser.parse_args()

    run_monitor(args.thread, args.task_name, args.pid)


if __name__ == "__main__":
    main()
