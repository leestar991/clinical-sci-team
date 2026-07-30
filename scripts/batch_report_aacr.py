#!/Volumes/data/github/clinical-sci-team/backend/.venv/bin/python
"""
批量生成 AACR 会议深度研究报告（Markdown 格式）。

输入目录：/Volumes/data/share/AACR-Confrence/excel-summarize
每个任务需要：
  - {stem}.md          ← 核心关注主题（必须存在）
  - {stem}/            ← 子目录内的 PDF/PNG/JPG 参考文献（必须非空）

工作流：
  1. 预处理：超限 PDF（> PDF_MAX_SIZE_MB）转为 JPEG 图片，原始大文件从上传列表剔除
  2. 创建 thread，一次性上传所有文件（主题 md + 全部参考文献）
  3. 发送一条引导消息（携带所有文件附件信息）
  4. 发送报告生成指令（从 SYSTEM_PROMPT_FILE 读取），读取 SSE 流直到完成
  5. 轮询 thread uploads 目录，找到输出 MD 文件后复制到 OUTPUT_DEST

输出：每个任务生成 {stem}_report.md，保存到 OUTPUT_DEST。
并发：2 个 worker，失败最多重试 5 次，按文件名升序处理。

用法：
  python batch_report_aacr.py                          # 批量处理所有未完成任务
  python batch_report_aacr.py -t "T03_RAS靶向..."      # 仅处理指定任务（支持前缀匹配）
  python batch_report_aacr.py -t T03 --force           # 强制重新处理（已有报告也重做）
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import NamedTuple
import os

# 监控脚本路径（与本脚本同目录）
MONITOR_SCRIPT = Path(__file__).parent / "batch_report_aacr_monitor.py"

import httpx

# ── 配置 ─────────────────────────────────────────────────────────────────────
BASE_URL     = "http://127.0.0.1:2026"
INPUT_DIR    = Path("/Volumes/data/share/AACR-Confrence/excel-summarize")
OUTPUT_DEST  = Path("/Volumes/data/share/AACR-Confrence/reports")
THREADS_BASE = Path("/Volumes/data/github/clinical-sci-team/backend/.deer-flow/threads")

PDF_MAX_SIZE_MB = 20  # 超过此大小的 PDF 先转成图片再上传，原文件从上传列表剔除

SYSTEM_PROMPT_FILE  = Path("/Volumes/data/github/clinical-sci-team/scripts/prompts/report-gen.md")
PDF_TO_IMAGE_SCRIPT = Path("/Volumes/data/github/clinical-sci-team/scripts/pdf_to_image.py")

# 分步 prompt 文件（方案四：每个步骤独立 run）
_PROMPTS_DIR = Path("/Volumes/data/github/clinical-sci-team/scripts/prompts")
STEP_PROMPT_FILES: dict[int, Path] = {
    0: _PROMPTS_DIR / "report-gen-s0.md",
    1: _PROMPTS_DIR / "report-gen-s1.md",
    2: _PROMPTS_DIR / "report-gen-s2.md",
    3: _PROMPTS_DIR / "report-gen-s3.md",
    4: _PROMPTS_DIR / "report-gen-s4.md",
}

NUM_WORKERS      = 2     # 严格并发上限
MAX_RETRIES      = 5     # 每个任务最多重试次数（整体重试，每次重新建 thread）
STEP_RETRIES     = 3     # 每个步骤内部最多重试次数
TASK_TIMEOUT     = 18000  # 单次 agent 运行最长 300 分钟（保留，用于整体兜底）
POST_STREAM_POLL = 300   # 流结束后再最多等 5 分钟等文件落盘
POLL_INTERVAL    = 5     # 轮询间隔（秒）
UPLOAD_TIMEOUT   = 1800  # 单次上传超时（秒）；本地服务，265MB/90文件约需数分钟
STATUS_INTERVAL  = 15    # 状态监控刷新间隔（秒）

# 各步骤独立超时（秒）
STEP_TIMEOUTS: dict[int, int] = {
    0: 600,    # 步骤0: 10分钟（只写计划文件）
    1: 14400,  # 步骤1: 240分钟（解读N篇文献；90篇任务分30批，每批约4min）
    2: 1200,   # 步骤2: 20分钟（综合汇总）
    3: 1800,   # 步骤3: 30分钟（6个检索主题）
    4: 2400,   # 步骤4+5: 40分钟（写章节+拼接）
}


# ── 日志 ─────────────────────────────────────────────────────────────────────
LOG_FILE = "/tmp/batch_report_aacr.log"
_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
_fh  = logging.FileHandler(LOG_FILE, mode="a")
_fh.setFormatter(_fmt)
_sh  = logging.StreamHandler(sys.stdout)
_sh.setFormatter(_fmt)
logging.root.setLevel(logging.INFO)
logging.root.addHandler(_fh)
if sys.stdout.isatty():
    logging.root.addHandler(_sh)
log = logging.getLogger(__name__)


# ── 任务状态追踪 ──────────────────────────────────────────────────────────────

class TaskStatus(NamedTuple):
    task_name: str
    worker_id: int
    task_idx: int
    phase: str          # "uploading" | "running" | "polling" | "done" | "failed"
    attempt: int
    thread_id: str | None
    started_at: float   # time.monotonic()


# 全局运行时状态表，key = md_file.stem
_active: dict[str, TaskStatus] = {}
_active_lock = asyncio.Lock()


async def set_task_status(task_name: str, status: TaskStatus) -> None:
    async with _active_lock:
        _active[task_name] = status


async def clear_task_status(task_name: str) -> None:
    async with _active_lock:
        _active.pop(task_name, None)


# ── API 轮询辅助：读取 todos 和计划文件 ──────────────────────────────────────

async def fetch_thread_todos(client: httpx.AsyncClient, thread_id: str) -> list[dict]:
    """从 thread history 最新检查点获取 todos 列表。"""
    try:
        r = await client.post(
            f"{BASE_URL}/api/langgraph/threads/{thread_id}/history",
            json={"limit": 1},
            timeout=15,
        )
        if r.status_code != 200:
            return []
        data = json.loads(r.text, strict=False)
        if not isinstance(data, list) or not data:
            return []
        todos = data[0].get("values", {}).get("todos", [])
        return todos if isinstance(todos, list) else []
    except Exception:
        return []


def _read_plan_file(thread_id: str, task_name: str) -> str | None:
    """读取 agent 写入的任务计划文件内容（如已创建）。"""
    plan_path = THREADS_BASE / thread_id / "user-data" / "workspace" / f"{task_name}_plan.md"
    if plan_path.exists():
        try:
            return plan_path.read_text(encoding="utf-8")
        except Exception:
            return None
    return None


# ── 状态显示 ──────────────────────────────────────────────────────────────────

_STATUS_ICONS = {
    "uploading": "📤",
    "running":   "🔄",
    "polling":   "🔍",
    "done":      "✅",
    "failed":    "❌",
}
_TODO_ICONS = {
    "completed":   "✅",
    "in_progress": "🔄",
    "pending":     "⬜",
    "cancelled":   "🚫",
}
# ANSI
_BOLD  = "\033[1m"
_DIM   = "\033[2m"
_CYAN  = "\033[36m"
_GREEN = "\033[32m"
_RED   = "\033[31m"
_YLW   = "\033[33m"
_RST   = "\033[0m"

_USE_COLOR = sys.stdout.isatty()

def _c(code: str, text: str) -> str:
    return f"{code}{text}{_RST}" if _USE_COLOR else text


def _fmt_elapsed(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    return f"{m}m{s:02d}s"


def _render_status_block(
    active_snapshot: dict[str, TaskStatus],
    todo_snapshot: dict[str, list[dict]],       # task_name -> todos
    plan_snapshot: dict[str, str | None],        # task_name -> plan_file content (first 20 lines)
    results: dict[str, str],
    total_pending: int,
    elapsed_total: float,
) -> str:
    now = time.monotonic()
    lines: list[str] = []

    ts_str = time.strftime("%H:%M:%S")
    lines.append(_c(_BOLD, f"{'─'*60}"))
    lines.append(
        _c(_BOLD, f"📊 批处理状态监控")
        + _c(_DIM, f"  [{ts_str}  已运行 {_fmt_elapsed(elapsed_total)}]")
    )

    # ── 整体进度 ──
    done    = sum(1 for v in results.values() if v == "success")
    failed  = sum(1 for v in results.values() if v == "failed")
    running = len(active_snapshot)
    total   = total_pending
    lines.append(
        f"  总进度: {_c(_GREEN, str(done))}✅  "
        f"{_c(_RED, str(failed))}❌  "
        f"{_c(_CYAN, str(running))}🔄  "
        f"待处理 {total - done - failed - running} / {total}"
    )
    lines.append("")

    # ── 各活跃任务 ──
    if active_snapshot:
        lines.append(_c(_BOLD, "  活跃任务:"))
        for task_name, st in sorted(active_snapshot.items(), key=lambda x: x[1].task_idx):
            icon = _STATUS_ICONS.get(st.phase, "❓")
            elapsed = now - st.started_at
            tid_short = st.thread_id[:8] + "…" if st.thread_id else "—"
            lines.append(
                f"    {icon} [W{st.worker_id} T{st.task_idx:03d}] "
                f"{_c(_BOLD, task_name[:40])}  "
                f"{_c(_DIM, st.phase)}  "
                f"{_c(_YLW, _fmt_elapsed(elapsed))}  "
                f"第{st.attempt}次  "
                f"{_c(_DIM, tid_short)}"
            )

            # Todos
            todos = todo_snapshot.get(task_name, [])
            if todos:
                done_n = sum(1 for t in todos if t.get("status") == "completed")
                in_prog = [t for t in todos if t.get("status") == "in_progress"]
                lines.append(
                    f"       Todo: {done_n}/{len(todos)} 完成"
                    + (f"  🔄 {in_prog[0]['content'][:35]}…" if in_prog else "")
                )
                # Show last 3 todos (most recent first)
                for t in todos[-5:]:
                    t_icon = _TODO_ICONS.get(t.get("status", ""), "❓")
                    content = t.get("content", "")[:50]
                    lines.append(f"         {t_icon} {content}")

            # Plan file progress section (parse "执行进度" table)
            plan_text = plan_snapshot.get(task_name)
            if plan_text:
                progress_lines = _extract_plan_progress(plan_text)
                if progress_lines:
                    lines.append(f"       计划进度:")
                    for pl in progress_lines:
                        lines.append(f"         {pl}")

            lines.append("")

    return "\n".join(lines)


def _extract_plan_progress(plan_text: str) -> list[str]:
    """从计划文件中提取执行进度表格行（步骤0-5），跳过表头和分隔线。"""
    result = []
    in_table = False
    header_passed = False
    for line in plan_text.splitlines():
        if "执行进度" in line and "##" in line:
            in_table = True
            header_passed = False
            continue
        if in_table:
            if not line.startswith("|"):
                if line.startswith("##"):
                    break  # next section
                continue
            # 跳过 markdown 表格分隔线（|---|...）
            if "|---" in line or "|──" in line:
                header_passed = True
                continue
            # 跳过列名表头行（分隔线之前）
            if not header_passed:
                continue
            parts = [p.strip() for p in line.strip("|").split("|")]
            if len(parts) >= 3 and "步骤" in parts[0]:
                step_id = parts[0]
                name    = parts[1]
                status  = parts[2]
                result.append(f"{status}  {step_id} {name}")
    return result


# ── 监控协程 ──────────────────────────────────────────────────────────────────

async def status_monitor(
    results: dict[str, str],
    total_pending: int,
    stop_event: asyncio.Event,
    start_time: float,
) -> None:
    """
    后台协程：每 STATUS_INTERVAL 秒向终端输出一次状态摘要。
    仅在 TTY 模式下运行（非管道/文件重定向）。
    """
    if not sys.stdout.isatty():
        return  # 非交互终端不输出状态块，日志文件已有足够信息

    client = httpx.AsyncClient()
    try:
        while not stop_event.is_set():
            await asyncio.sleep(STATUS_INTERVAL)
            if stop_event.is_set():
                break

            async with _active_lock:
                snapshot = dict(_active)

            # 并发拉取每个活跃任务的 todos 和计划文件
            todo_snapshot: dict[str, list[dict]] = {}
            plan_snapshot: dict[str, str | None] = {}

            async def _fetch(task_name: str, st: TaskStatus) -> None:
                if st.thread_id:
                    todos = await fetch_thread_todos(client, st.thread_id)
                    todo_snapshot[task_name] = todos
                    plan_snapshot[task_name] = _read_plan_file(st.thread_id, task_name)

            if snapshot:
                await asyncio.gather(*[_fetch(n, s) for n, s in snapshot.items()])

            elapsed = time.monotonic() - start_time
            block = _render_status_block(
                snapshot, todo_snapshot, plan_snapshot,
                results, total_pending, elapsed,
            )
            # 打印到 stdout（不经过 logging，避免污染日志文件）
            print(block, flush=True)
    finally:
        await client.aclose()

def _tag(worker_id: int, task_idx: int) -> str:
    return f"[W{worker_id} T{task_idx:03d}]"


def _start_monitor(thread_id: str, task_name: str, parent_pid: int) -> subprocess.Popen | None:
    """启动后台监控进程，将监控日志写入 MONITOR_LOG_FILE。"""
    if not MONITOR_SCRIPT.exists():
        log.warning("监控脚本不存在，跳过监控: %s", MONITOR_SCRIPT)
        return None
    cmd = [
        sys.executable, str(MONITOR_SCRIPT),
        "--thread", thread_id,
        "--task-name", task_name,
        "--pid", str(parent_pid),
    ]
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        log.info("监控进程已启动 PID=%d → %s", proc.pid, "/tmp/batch_report_aacr_monitor.log")
        return proc
    except Exception as e:
        log.warning("启动监控进程失败: %s", e)
        return None


def _load_system_prompt(task_name: str) -> str:
    """加载系统提示词，将 {TASK_NAME} 占位符替换为实际任务名称。"""
    if not SYSTEM_PROMPT_FILE.exists():
        log.warning("系统提示词文件不存在: %s，将使用空提示词", SYSTEM_PROMPT_FILE)
        return ""
    template = SYSTEM_PROMPT_FILE.read_text(encoding="utf-8")
    return template.replace("{TASK_NAME}", task_name)


# ── 方案四：分步执行辅助函数 ──────────────────────────────────────────────────

def detect_completed_step(thread_id: str, task_name: str, ref_count: int) -> int:
    """
    通过磁盘文件检查判断已完成到哪个步骤。
    返回值：-1=未开始, 0=步骤0完成, 1=步骤1完成, 2=步骤2完成, 3=步骤3完成, 4=步骤4+5完成
    """
    base = THREADS_BASE / thread_id / "user-data"
    ws   = base / "workspace"

    # 步骤4+5完成：最终报告已写入 outputs/
    if (base / "outputs" / f"{task_name}_report.md").exists():
        return 4

    # 步骤3完成（但4未完成）：search_results/ 有 ≥6 个文件
    if len(list((ws / "search_results").glob("*.md"))) >= 6:
        return 3

    # 步骤2完成：master_summary.md 严格文件名
    if (ws / "master_summary.md").exists():
        return 2

    # 步骤1完成：doc_summaries/ 文件数 ≥ ref_count
    if len(list((ws / "doc_summaries").glob("*.md"))) >= ref_count:
        return 1

    # 步骤0完成：计划文件存在
    if (ws / f"{task_name}_plan.md").exists():
        return 0

    return -1


def _verify_step_output(thread_id: str, task_name: str, ref_count: int, step: int) -> bool:
    """验证指定步骤的磁盘产出是否存在。"""
    completed = detect_completed_step(thread_id, task_name, ref_count)
    return completed >= step


def _load_step_prompt(
    step: int, task_name: str, ref_count: int, completed_step: int
) -> str:
    """
    加载指定步骤的 prompt，替换占位符。
    step: 0-4（4 = 步骤4+5合并）
    """
    import datetime
    prompt_file = STEP_PROMPT_FILES.get(step)
    if not prompt_file or not prompt_file.exists():
        raise FileNotFoundError(f"步骤{step} prompt 文件不存在: {prompt_file}")
    template = prompt_file.read_text(encoding="utf-8")
    today = datetime.date.today().strftime("%Y-%m-%d")
    return (
        template
        .replace("{TASK_NAME}", task_name)
        .replace("{REF_COUNT}", str(ref_count))
        .replace("{COMPLETED_STEP}", str(completed_step))
        .replace("{TODAY}", today)
    )


def _get_unprocessed_docs(thread_id: str, all_files: list[Path]) -> list[Path]:
    """
    返回尚未生成解读文件的参考文献。
    解读文件命名规则：doc_summaries/{stem}_解读.md
    """
    done_dir = THREADS_BASE / thread_id / "user-data" / "workspace" / "doc_summaries"
    done_stems: set[str] = set()
    if done_dir.exists():
        for f in done_dir.glob("*_解读.md"):
            # 还原原始文件名 stem
            done_stems.add(f.stem.removesuffix("_解读"))
    return [f for f in all_files if f.stem not in done_stems]


async def send_message_step(
    client: httpx.AsyncClient,
    thread_id: str,
    text: str,
    file_infos: list[dict],
    step_timeout: int,
) -> str | None:
    """
    发送单步消息（SSE 流式），使用步骤专属超时。
    返回 run_id（如可从响应头或 SSE 事件中获取），否则返回 None。
    """
    payload = {
        "assistant_id": "lead_agent",
        "input": {
            "messages": [{
                "type": "human",
                "content": [{"type": "text", "text": text}],
                "additional_kwargs": {"files": _files_meta(file_infos)} if file_infos else {},
            }]
        },
        "config": {
            "recursion_limit": 1000,
            "configurable": {
                "agent_name": "clinical-medicine",
                "model_name": "gpt-5-4",
                "thinking_enabled": True,
                "reasoning_effort": "high",
                "is_plan_mode": True,
                "subagent_enabled": True,
                "thread_id": thread_id,
            },
        },
        "stream_mode": ["values", "messages", "custom"],
        "stream_subgraphs": True,
    }

    run_id: str | None = None
    agent_error: str | None = None
    async with client.stream(
        "POST",
        f"{BASE_URL}/api/langgraph/threads/{thread_id}/runs/stream",
        json=payload,
        timeout=httpx.Timeout(step_timeout),
    ) as resp:
        resp.raise_for_status()
        # 尝试从响应头获取 run_id
        loc = resp.headers.get("content-location", "")
        if "/runs/" in loc:
            run_id = loc.split("/runs/")[-1].split("/")[0]
        async for line in resp.aiter_lines():
            if not line.startswith("data:"):
                continue
            raw = line[5:].strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
                if isinstance(obj, dict) and "error" in obj:
                    agent_error = f"{obj['error']}: {obj.get('message', '')}"
                # 从 SSE 事件中提取 run_id（备选方式）
                if run_id is None and isinstance(obj, dict) and "run_id" in obj:
                    run_id = obj["run_id"]
            except json.JSONDecodeError:
                pass

    if agent_error:
        raise RuntimeError(f"Agent 返回错误: {agent_error}")
    return run_id


async def try_once_step(
    tag: str,
    thread_id: str,
    step: int,
    task_name: str,
    ref_count: int,
    file_infos: list[dict],
    all_ref_paths: list[Path],  # 仅用于步骤1子批次过滤
) -> bool:
    """
    执行单个步骤。步骤1会分批发送未处理的文献（每批3篇）。
    返回 True 表示该步骤磁盘产出验证通过。
    """
    completed = detect_completed_step(thread_id, task_name, ref_count)
    if completed >= step:
        log.info("%s 步骤%d 磁盘验证：已完成，跳过", tag, step)
        return True

    step_timeout = STEP_TIMEOUTS.get(step, 1800)
    t0 = time.monotonic()

    async with httpx.AsyncClient() as client:
        if step == 1:
            # 步骤1：找出未处理的文档，分批（每批3篇）逐批发送
            # 每批各自发一条消息（复用同一 thread，agent 续接进度）
            ref_paths = [p for p in all_ref_paths if p.suffix.lower() != ".md"]
            unprocessed = _get_unprocessed_docs(thread_id, ref_paths)
            log.info(
                "%s 步骤1：共 %d 篇参考文献，已处理 %d 篇，待处理 %d 篇",
                tag, ref_count, ref_count - len(unprocessed), len(unprocessed),
            )

            if not unprocessed:
                log.info("%s 步骤1：所有文献已处理，直接跳过", tag)
                return True

            BATCH_SIZE = 3
            batches = [unprocessed[i:i+BATCH_SIZE] for i in range(0, len(unprocessed), BATCH_SIZE)]

            for batch_idx, batch in enumerate(batches):
                # 构造本批次附件信息（从 file_infos 中过滤）
                batch_names = {p.name for p in batch}
                batch_infos = [fi for fi in file_infos if fi["filename"] in batch_names]

                prompt = _load_step_prompt(step, task_name, ref_count,
                                           detect_completed_step(thread_id, task_name, ref_count))
                log.info(
                    "%s 步骤1 批次 %d/%d：发送 %d 篇文献",
                    tag, batch_idx + 1, len(batches), len(batch),
                )
                run_id: str | None = None
                try:
                    run_id = await send_message_step(client, thread_id, prompt, batch_infos, step_timeout)
                except Exception:
                    if run_id:
                        await cancel_run(client, thread_id, run_id, tag)
                    raise

                # 批次间短暂等待，让 agent 落盘
                await asyncio.sleep(2)

        else:
            # 步骤 0/2/3/4：单次发送，无需附件（步骤0用主题md附件）
            attach_infos: list[dict] = []
            if step == 0:
                # 步骤0只需要主题 md 文件附件
                attach_infos = [fi for fi in file_infos
                                if fi["filename"].endswith(".md")
                                and not any(kw in fi["filename"]
                                            for kw in ("_解读", "master_summary"))]

            prompt = _load_step_prompt(step, task_name, ref_count,
                                       detect_completed_step(thread_id, task_name, ref_count))
            log.info("%s 步骤%d：发送 prompt（%d 字）", tag, step, len(prompt))
            run_id = None
            try:
                run_id = await send_message_step(client, thread_id, prompt, attach_infos, step_timeout)
            except Exception:
                if run_id:
                    await cancel_run(client, thread_id, run_id, tag)
                raise

    elapsed = time.monotonic() - t0
    log.info("%s 步骤%d stream 结束，耗时 %.1fs，验证磁盘产出…", tag, step, elapsed)

    # 等待文件落盘（步骤1/4需要稍长一点的等待）
    poll_wait = 30 if step in (1, 4) else 10
    for _ in range(poll_wait):
        if _verify_step_output(thread_id, task_name, ref_count, step):
            log.info("%s 步骤%d 完成（磁盘验证通过）", tag, step)
            return True
        await asyncio.sleep(POLL_INTERVAL)

    ok = _verify_step_output(thread_id, task_name, ref_count, step)
    if ok:
        log.info("%s 步骤%d 完成（最终验证通过）", tag, step)
    else:
        log.warning("%s 步骤%d 磁盘验证失败", tag, step)
    return ok


def _mime(p: Path) -> str:
    s = p.suffix.lower()
    return {
        ".md":   "text/markdown",
        ".pdf":  "application/pdf",
        ".png":  "image/png",
        ".jpg":  "image/jpeg",
        ".jpeg": "image/jpeg",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }.get(s, "application/octet-stream")


def _pdf_size_mb(p: Path) -> float:
    return p.stat().st_size / (1024 * 1024)


# ── PDF 大文件转图片 ──────────────────────────────────────────────────────────

def _convert_pdf_to_images(pdf_path: Path, output_dir: Path) -> list[Path]:
    """调用 pdf_to_image.py 将单个 PDF 转换为 JPEG，返回生成的图片列表。"""
    if not PDF_TO_IMAGE_SCRIPT.exists():
        raise FileNotFoundError(f"pdf_to_image.py 不存在: {PDF_TO_IMAGE_SCRIPT}")
    output_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable, str(PDF_TO_IMAGE_SCRIPT), str(pdf_path),
        "--format", "jpeg", "--quality", "85", "--dpi", "150",
        "--output-dir", str(output_dir),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if result.returncode != 0:
        raise RuntimeError(
            f"pdf_to_image.py 失败 (code {result.returncode}): {result.stderr[:200]}"
        )
    images = sorted(output_dir.glob(f"{pdf_path.stem}*.jpg"))
    if not images:
        images = sorted(output_dir.glob(f"{pdf_path.stem}*.png"))
    return images


def prepare_files_for_upload(files: list[Path], tmp_dir: Path, tag: str) -> list[Path]:
    """
    预处理上传文件列表：
    - 超限 PDF（> PDF_MAX_SIZE_MB）→ 转为 JPEG 图片，原 PDF 从列表中剔除
    - 转换失败时：记录警告，仍保留原 PDF（fallback）
    - 其余文件直接保留
    返回最终待上传文件列表。
    """
    result: list[Path] = []
    for f in files:
        if f.suffix.lower() == ".pdf" and _pdf_size_mb(f) > PDF_MAX_SIZE_MB:
            log.info("%s PDF %.1fMB 超限 → 转图片（剔除原文件）: %s", tag, _pdf_size_mb(f), f.name)
            img_dir = tmp_dir / f"{f.stem}_images"
            try:
                images = _convert_pdf_to_images(f, img_dir)
                if images:
                    log.info("%s 转换完成: %d 张图片，已剔除原 PDF", tag, len(images))
                    result.extend(images)
                    # 原 PDF 不加入 result，已被图片替代
                else:
                    log.warning("%s 转换未生成图片，fallback 保留原 PDF: %s", tag, f.name)
                    result.append(f)
            except Exception as e:
                log.warning("%s PDF 转图片失败 (%s)，fallback 保留原 PDF: %s", tag, e, f.name)
                result.append(f)
        else:
            result.append(f)
    return result


# ── DeerFlow API ──────────────────────────────────────────────────────────────

async def create_thread(client: httpx.AsyncClient) -> str:
    r = await client.post(f"{BASE_URL}/api/langgraph/threads", json={}, timeout=30)
    r.raise_for_status()
    thread_id: str = r.json()["thread_id"]
    await client.patch(
        f"{BASE_URL}/api/langgraph/threads/{thread_id}",
        json={"metadata": {"agent_name": "clinical-medicine"}},
        timeout=30,
    )
    return thread_id


async def upload_files(
    client: httpx.AsyncClient, thread_id: str, files: list[Path]
) -> list[dict]:
    handles: list[tuple[str, any, str]] = []
    try:
        for p in files:
            fh = open(p, "rb")
            handles.append((p.name, fh, _mime(p)))
        r = await client.post(
            f"{BASE_URL}/api/threads/{thread_id}/uploads",
            files=[("files", h) for h in handles],
            timeout=UPLOAD_TIMEOUT,
        )
        r.raise_for_status()
        return r.json()["files"]
    finally:
        for _, fh, _ in handles:
            fh.close()


def _files_meta(file_infos: list[dict]) -> list[dict]:
    return [
        {"filename": f["filename"], "size": str(f["size"]),
         "path": f["virtual_path"], "status": "uploaded"}
        for f in file_infos
    ]


async def send_message(
    client: httpx.AsyncClient,
    thread_id: str,
    text: str,
    file_infos: list[dict],
    *,
    stream: bool = False,
) -> None:
    """
    发送一条 human 消息。
    stream=False → /runs（非流式，等待 run 完成，适合中间批次）
    stream=True  → /runs/stream（SSE，适合最终报告生成指令）
    """
    payload = {
        "assistant_id": "lead_agent",
        "input": {
            "messages": [{
                "type": "human",
                "content": [{"type": "text", "text": text}],
                "additional_kwargs": {"files": _files_meta(file_infos)} if file_infos else {},
            }]
        },
        "config": {
            "recursion_limit": 1000,
            "configurable": {
                "agent_name": "clinical-medicine",
                "model_name": "gpt-5-4",
                "thinking_enabled": True,
                "reasoning_effort": "high",
                "is_plan_mode": True,
                "subagent_enabled": True,
                "thread_id": thread_id,
            },
        },
        "stream_mode": ["values", "messages", "custom"],
        "stream_subgraphs": True,
    }

    if not stream:
        r = await client.post(
            f"{BASE_URL}/api/langgraph/threads/{thread_id}/runs",
            json=payload, timeout=120,
        )
        r.raise_for_status()
        run_id = r.json().get("run_id")
        if run_id:
            await _wait_for_run(client, thread_id, run_id)
        return

    agent_error: str | None = None
    async with client.stream(
        "POST",
        f"{BASE_URL}/api/langgraph/threads/{thread_id}/runs/stream",
        json=payload,
        timeout=httpx.Timeout(TASK_TIMEOUT),
    ) as resp:
        resp.raise_for_status()
        async for line in resp.aiter_lines():
            if not line.startswith("data:"):
                continue
            raw = line[5:].strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
                if isinstance(obj, dict) and "error" in obj:
                    agent_error = f"{obj['error']}: {obj.get('message', '')}"
            except json.JSONDecodeError:
                pass

    if agent_error:
        raise RuntimeError(f"Agent 返回错误: {agent_error}")


async def _wait_for_run(
    client: httpx.AsyncClient, thread_id: str, run_id: str, timeout: float = 120
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = await client.get(
            f"{BASE_URL}/api/langgraph/threads/{thread_id}/runs/{run_id}",
            timeout=30,
        )
        if r.status_code == 200:
            if r.json().get("status", "") in ("success", "error", "timeout"):
                return
        await asyncio.sleep(3)


# ── 资源清理 ─────────────────────────────────────────────────────────────────

async def cleanup_thread(client: httpx.AsyncClient, thread_id: str, tag: str) -> None:
    """任务完成后删除 thread，释放磁盘和服务端资源（文件系统 + Store + Checkpointer）。"""
    try:
        r = await client.delete(
            f"{BASE_URL}/api/threads/{thread_id}",
            timeout=30,
        )
        if r.status_code == 200:
            log.info("%s Thread %s 已清理", tag, thread_id)
        else:
            log.warning("%s 清理 Thread 失败 (HTTP %d): %s", tag, r.status_code, thread_id)
    except Exception as e:
        log.warning("%s 清理 Thread 异常: %s", tag, e)


async def cancel_run(client: httpx.AsyncClient, thread_id: str, run_id: str, tag: str) -> None:
    """取消运行中的 agent run，释放 LLM 资源。"""
    if not run_id:
        return
    try:
        r = await client.post(
            f"{BASE_URL}/api/threads/{thread_id}/runs/{run_id}/cancel",
            params={"action": "interrupt", "wait": "false"},
            timeout=15,
        )
        if r.status_code in (202, 204):
            log.info("%s Run %s 已取消", tag, run_id[:12])
        elif r.status_code == 409:
            log.debug("%s Run %s 已结束，无需取消", tag, run_id[:12])
        else:
            log.warning("%s 取消 Run 失败 (HTTP %d)", tag, r.status_code)
    except Exception as e:
        log.debug("%s 取消 Run 异常（非致命）: %s", tag, e)


# ── 输出文件查找 ──────────────────────────────────────────────────────────────

def find_output_md(thread_id: str, expected_name: str, upload_cutoff: float | None = None) -> Path | None:
    """
    在 thread 的 outputs/ 目录查找输出报告。
    report-gen.md 提示词将报告写入 outputs/。
    精确匹配优先；模糊匹配时只查找 upload_cutoff 时间之后新建的 .md（排除上传的原始文件）。
    同时排除中间产物（_解读.md、master_summary.md 等）。
    """
    search_dirs = [
        THREADS_BASE / thread_id / "user-data" / "outputs",
    ]
    # 中间产物关键词，命中则跳过
    _skip_keywords = ("_解读", "master_summary", "search_results")

    for out_dir in search_dirs:
        if not out_dir.exists():
            continue
        # 精确匹配优先
        target = out_dir / expected_name
        if target.exists():
            return target
        # 模糊匹配：找 upload_cutoff 后新建的非中间产物 .md
        candidates = [
            f for f in out_dir.glob("*.md")
            if not any(kw in f.name for kw in _skip_keywords)
            and (upload_cutoff is None or f.stat().st_mtime > upload_cutoff)
        ]
        if candidates:
            return max(candidates, key=lambda f: f.stat().st_mtime)
    return None


async def poll_until_file(
    thread_id: str, expected_name: str, upload_cutoff: float | None = None
) -> Path | None:
    deadline = time.monotonic() + POST_STREAM_POLL
    while time.monotonic() < deadline:
        result = find_output_md(thread_id, expected_name, upload_cutoff)
        if result:
            return result
        await asyncio.sleep(POLL_INTERVAL)
    return find_output_md(thread_id, expected_name, upload_cutoff)


# ── 单次任务执行 ──────────────────────────────────────────────────────────────

async def try_once(
    tag: str,
    md_file: Path,
    source_files: list[Path],
    output_md_name: str,   # agent 写入的文件名（用于轮询定位）
    dest_md_path: Path,    # 最终复制目标路径
    *,
    worker_id: int = 0,
    task_idx: int = 0,
    attempt: int = 1,
) -> bool:
    """
    方案四：分步执行流程
      0. 超限 PDF 转图片，原大文件从上传列表剔除
      1. 创建 thread（整体任务唯一）
      2. 一次性上传所有文件
      3. 按步骤 0→1→2→3→4 依次执行，每步独立 run + 磁盘验证
         - 步骤1 进一步分批（每批3篇），确保单次 run context 不超限
      4. 最终复制报告到 OUTPUT_DEST
    """
    import tempfile

    task_name = md_file.stem
    ref_count = len([f for f in source_files
                     if f.suffix.lower() in (".pdf", ".png", ".jpg", ".jpeg")])

    with tempfile.TemporaryDirectory(prefix="aacr_pdf2img_") as _tmp:
        tmp_dir = Path(_tmp)

        # 预处理参考文献（超限 PDF → 图片）
        processed_refs = prepare_files_for_upload(source_files, tmp_dir, tag)
        all_files = [md_file] + processed_refs
        log.info(
            "%s 准备上传 %d 个文件（主题1 + 参考文献%d，原始%d）",
            tag, len(all_files), len(processed_refs), len(source_files),
        )

        async with httpx.AsyncClient() as client:
            await set_task_status(task_name, TaskStatus(
                task_name=task_name, worker_id=worker_id, task_idx=task_idx,
                phase="uploading", attempt=attempt, thread_id=None,
                started_at=time.monotonic(),
            ))
            thread_id = await create_thread(client)
            log.info("%s Thread %s 已创建", tag, thread_id)

            upload_start = time.time()
            all_infos = await upload_files(client, thread_id, all_files)
            upload_cutoff = time.time()
            log.info(
                "%s 全部文件已上传（%d 个）: %s…",
                tag, len(all_files),
                ", ".join(p.name for p in all_files[:3]) + ("…" if len(all_files) > 3 else ""),
            )

        # 启动监控进程
        monitor_proc = _start_monitor(thread_id, task_name, os.getpid())

        await set_task_status(task_name, TaskStatus(
            task_name=task_name, worker_id=worker_id, task_idx=task_idx,
            phase="running", attempt=attempt, thread_id=thread_id,
            started_at=time.monotonic(),
        ))

        # 依次执行步骤 0→1→2→3→4
        steps_all_ok = True
        try:
            for step in range(5):
                already = detect_completed_step(thread_id, task_name, ref_count)
                if already >= step:
                    log.info("%s 步骤%d 已完成（磁盘验证），跳过", tag, step)
                    continue

                step_ok = False
                for step_attempt in range(1, STEP_RETRIES + 1):
                    log.info("%s 步骤%d 第%d/%d次", tag, step, step_attempt, STEP_RETRIES)
                    try:
                        step_ok = await try_once_step(
                            tag, thread_id, step, task_name, ref_count,
                            all_infos, all_files,
                        )
                    except Exception as exc:
                        log.warning("%s 步骤%d 出错: %s", tag, step, exc)
                        step_ok = False

                    if step_ok:
                        break

                    if step_attempt < STEP_RETRIES:
                        wait = min(10 * step_attempt, 60)
                        log.info("%s 步骤%d 失败，%ds 后重试…", tag, step, wait)
                        await asyncio.sleep(wait)

                if not step_ok:
                    log.error("%s 步骤%d 三次均失败，整体任务失败，保留 thread 供调试: %s",
                              tag, step, thread_id)
                    steps_all_ok = False
                    break
        finally:
            # 无论成功/失败，确保 monitor 子进程被终止（回收僵尸进程）
            if monitor_proc:
                monitor_proc.terminate()
                monitor_proc.wait()

        if not steps_all_ok:
            return False

    # with tempfile 块在此结束，临时目录被清理
    # thread_id / upload_cutoff / all_infos 均在 with 块内定义，此处仍可访问
    # （Python with 块不引入新作用域，局部变量在 with 结束后仍有效）

    # 定位并复制最终报告
    await set_task_status(task_name, TaskStatus(
        task_name=task_name, worker_id=worker_id, task_idx=task_idx,
        phase="polling", attempt=attempt, thread_id=thread_id,
        started_at=time.monotonic(),
    ))
    output_md = await poll_until_file(thread_id, output_md_name, upload_cutoff)
    if not output_md:
        log.warning("%s 未找到输出文件: %s", tag, output_md_name)
        return False

    dest_md_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(output_md, dest_md_path)
    size_kb = dest_md_path.stat().st_size // 1024
    log.info("%s ✓ 报告已保存 → %s（%d KB）", tag, dest_md_path, size_kb)

    # 将 outputs/ 目录下所有文件拷贝到共享目录 reports/{task_name}/outputs/
    _copy_outputs_to_share(thread_id, task_name, tag)

    # 保留完成的工作目录，不清理 thread（便于后续检查和调试）
    log.info("%s Thread %s 工作目录已保留", tag, thread_id)

    return True


# ── worker ────────────────────────────────────────────────────────────────────

async def worker(worker_id: int, queue: asyncio.Queue, results: dict) -> None:
    while True:
        item = await queue.get()
        if item is None:
            queue.task_done()
            break

        task_idx, md_file, source_files, output_md_name, dest_md_path = item
        tag = _tag(worker_id, task_idx)
        task_name = md_file.stem
        log.info("%s 开始处理: %s（%d 个源文件）", tag, md_file.name, len(source_files))

        success = False
        for attempt in range(1, MAX_RETRIES + 1):
            log.info("%s 尝试 #%d/%d", tag, attempt, MAX_RETRIES)
            try:
                success = await try_once(
                    tag, md_file, source_files, output_md_name, dest_md_path,
                    worker_id=worker_id, task_idx=task_idx, attempt=attempt,
                )
            except Exception as exc:
                log.warning("%s 出错: %s", tag, exc)
                success = False

            if success:
                log.info("%s 完成（第 %d 次尝试）", tag, attempt)
                results[task_name] = "success"
                break

            if attempt < MAX_RETRIES:
                wait = min(15 * attempt, 120)
                log.info("%s %ds 后重试…", tag, wait)
                await asyncio.sleep(wait)

        if not success:
            log.error("%s 失败，已达最大重试次数 %d: %s", tag, MAX_RETRIES, md_file.name)
            results[task_name] = "failed"

        await clear_task_status(task_name)
        queue.task_done()


# ── 任务收集 ──────────────────────────────────────────────────────────────────

def _collect_source_files(task_dir: Path) -> list[Path]:
    """收集任务子目录中的 PDF/PNG/JPG 文件，排除 .md 主题文件（已单独上传）。"""
    files: list[Path] = []
    for ext in ("*.pdf", "*.PDF", "*.png", "*.PNG", "*.jpg", "*.JPG", "*.jpeg", "*.JPEG"):
        files.extend(task_dir.glob(ext))
    return sorted(set(files))


def _find_task(task_name: str) -> tuple[Path, list[Path]] | None:
    """按名称（精确 or 前缀/子串）查找任务，返回 (md_file, source_files)。"""
    # 精确匹配
    exact_dir = INPUT_DIR / task_name
    exact_md  = INPUT_DIR / f"{task_name}.md"
    if exact_dir.is_dir() and exact_md.exists():
        files = _collect_source_files(exact_dir)
        if files:
            return exact_md, files

    # 前缀/子串匹配
    candidates = [
        (d, INPUT_DIR / f"{d.name}.md")
        for d in sorted(INPUT_DIR.iterdir())
        if d.is_dir() and (d.name.startswith(task_name) or task_name in d.name)
        and (INPUT_DIR / f"{d.name}.md").exists()
    ]
    if not candidates:
        return None
    if len(candidates) > 1:
        log.warning("'%s' 匹配到多个任务，使用第一个: %s", task_name, candidates[0][0].name)

    d, md_file = candidates[0]
    files = _collect_source_files(d)
    if not files:
        log.warning("任务目录 %s 中未找到参考文献文件", d.name)
        return None
    return md_file, files


def _scan_all_tasks() -> list[tuple[Path, list[Path]]]:
    """扫描 INPUT_DIR，返回所有有效任务列表。"""
    tasks = []
    for d in sorted(INPUT_DIR.iterdir()):
        if not d.is_dir():
            continue
        md_file = INPUT_DIR / f"{d.name}.md"
        if not md_file.exists():
            log.warning("跳过 %s：找不到对应 .md 文件", d.name)
            continue
        files = _collect_source_files(d)
        if not files:
            log.warning("跳过 %s：子目录无参考文献文件", d.name)
            continue
        tasks.append((md_file, files))
    return tasks


def _dest_md_path(md_file: Path) -> Path:
    """输出的 Markdown 报告路径。"""
    return OUTPUT_DEST / f"{md_file.stem}_report.md"


def _copy_outputs_to_share(thread_id: str, task_name: str, tag: str) -> None:
    """
    将 thread outputs/ 目录下的所有文件拷贝到
    /Volumes/data/share/AACR-Confrence/reports/{task_name}/outputs/
    子目录和隐藏文件（如 .DS_Store）会被跳过，只拷贝普通文件。
    """
    src_dir = THREADS_BASE / thread_id / "user-data" / "outputs"
    dest_dir = OUTPUT_DEST / task_name / "outputs"
    if not src_dir.exists():
        log.warning("%s outputs 目录不存在，跳过拷贝: %s", tag, src_dir)
        return
    dest_dir.mkdir(parents=True, exist_ok=True)
    copied = 0
    for f in src_dir.iterdir():
        if f.is_file() and not f.name.startswith("."):
            shutil.copy2(f, dest_dir / f.name)
            copied += 1
    log.info("%s ✓ 已拷贝 %d 个文件 → %s", tag, copied, dest_dir)


# ── main ─────────────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="batch_report_aacr.py",
        description="批量生成 AACR 会议深度研究报告（Markdown 格式）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例:
  python batch_report_aacr.py                              # 处理所有未完成任务
  python batch_report_aacr.py -t "T03_RAS靶向..."         # 仅处理指定任务（前缀匹配）
  python batch_report_aacr.py -t T03 --force              # 强制重新处理
  python batch_report_aacr.py --cleanup --dry-run         # 预览可清理的残留 thread
  python batch_report_aacr.py --cleanup                   # 删除所有残留 thread 释放磁盘
""",
    )
    parser.add_argument(
        "-t", "--task", metavar="TASK_NAME", default=None,
        help="仅处理此任务（精确名称或前缀/子串匹配）",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="强制重新处理，即使输出报告已存在",
    )
    parser.add_argument(
        "--cleanup", action="store_true",
        help="清理所有残留 thread 数据，释放磁盘和服务端资源",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="与 --cleanup 配合使用，仅显示待清理内容，不实际删除",
    )
    return parser.parse_args()


async def cleanup_old_threads(dry_run: bool = False) -> None:
    """
    扫描 THREADS_BASE，删除所有残留的 thread 目录。
    使用 --dry-run 仅预览大小不执行删除。
    """
    if not THREADS_BASE.exists():
        log.info("threads 目录不存在，无需清理: %s", THREADS_BASE)
        return

    to_delete: list[tuple[str, int]] = []
    total_size = 0
    for thread_dir in sorted(THREADS_BASE.iterdir()):
        if not thread_dir.is_dir():
            continue
        dir_size = sum(f.stat().st_size for f in thread_dir.rglob("*") if f.is_file())
        total_size += dir_size
        to_delete.append((thread_dir.name, dir_size))

    if not to_delete:
        log.info("无残留 thread 目录，无需清理")
        return

    log.info("发现 %d 个 thread，总占用 %.2f GB", len(to_delete), total_size / (1024 ** 3))

    if dry_run:
        log.info("[DRY-RUN] 以下 thread 将被删除（仅预览，不实际删除）：")
        for tid, size in to_delete:
            log.info("  [DRY-RUN] %s  (%.1f MB)", tid, size / (1024 ** 2))
        log.info("[DRY-RUN] 预计可释放 %.2f GB", total_size / (1024 ** 3))
        return

    log.info("开始清理 %d 个 thread…", len(to_delete))
    async with httpx.AsyncClient() as client:
        for tid, size in to_delete:
            await cleanup_thread(client, tid, "[CLEANUP]")
    log.info("清理完成，共释放约 %.2f GB", total_size / (1024 ** 3))


async def main() -> None:
    args = _parse_args()

    # --cleanup 模式：清理残留 thread 后退出
    if args.cleanup:
        await cleanup_old_threads(dry_run=args.dry_run)
        return

    if args.task:
        result = _find_task(args.task)
        if result is None:
            log.error("找不到任务: %s（%s 中无匹配）", args.task, INPUT_DIR)
            sys.exit(1)
        md_file, source_files = result
        log.info("单任务模式: %s（%d 个参考文献）", md_file.stem, len(source_files))

        dest = _dest_md_path(md_file)
        if not args.force and dest.exists():
            log.info("输出已存在，跳过（--force 强制重做）: %s", dest)
            return
        pending = [(md_file, source_files)]

    else:
        all_tasks = _scan_all_tasks()
        done_stems: set[str] = set()
        if not args.force and OUTPUT_DEST.exists():
            done_stems = {f.stem.removesuffix("_report") for f in OUTPUT_DEST.glob("*_report.md")}

        pending = [(md, files) for md, files in all_tasks
                   if md.stem not in done_stems]

        log.info(
            "总任务: %d | 已完成: %d | 待处理: %d | 并发: %d | 最大重试: %d",
            len(all_tasks), len(done_stems), len(pending), NUM_WORKERS, MAX_RETRIES,
        )

    if not pending:
        log.info("无待处理任务，退出。")
        return

    queue: asyncio.Queue = asyncio.Queue()
    results: dict[str, str] = {}
    total_pending = len(pending)
    batch_start   = time.monotonic()

    for i, (md, files) in enumerate(pending):
        # agent 写入的临时文件名（用于轮询定位，提示词中固定为任务名_report.md）
        output_md_name = f"{md.stem}_report.md"
        queue.put_nowait((i + 1, md, files, output_md_name, _dest_md_path(md)))

    num_workers = min(NUM_WORKERS, len(pending))
    for _ in range(num_workers):
        queue.put_nowait(None)

    # 启动状态监控（仅 TTY 模式）
    stop_monitor = asyncio.Event()
    monitor_task = asyncio.create_task(
        status_monitor(results, total_pending, stop_monitor, batch_start)
    )

    worker_tasks = [
        asyncio.create_task(worker(w + 1, queue, results))
        for w in range(num_workers)
    ]
    await asyncio.gather(*worker_tasks)

    # 停止监控
    stop_monitor.set()
    await monitor_task

    success_count = sum(1 for v in results.values() if v == "success")
    fail_count    = sum(1 for v in results.values() if v == "failed")
    total_done    = len(list(OUTPUT_DEST.glob("*_report.md"))) if OUTPUT_DEST.exists() else 0

    log.info("=" * 60)
    log.info("批处理完成")
    log.info("  本次成功: %d", success_count)
    log.info("  本次失败: %d", fail_count)
    log.info("  输出目录共: %d 份 Markdown 报告", total_done)
    log.info("=" * 60)

    if fail_count > 0:
        log.error("失败任务: %s", [k for k, v in results.items() if v == "failed"])
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
