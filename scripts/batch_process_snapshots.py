#!/usr/bin/env python3
"""
批量处理 /Volumes/data/share/Snapshots 下的图片文件。

并发模型：2 个 worker，每个 worker 一次只处理一个批次。
worker 确认 MD 文件已写入 outputs 后，才从队列取下一批。
失败则无限重试（等待时间逐渐增大），直到成功为止。
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx

# ── 配置 ─────────────────────────────────────────────────────────────────────
BASE_URL = "http://127.0.0.1:2026"
SNAPSHOTS_DIR = Path("/Volumes/data/share/Snapshots")
OUTPUT_DIR = Path("/Volumes/data/share/accr")
THREADS_BASE = Path("/Volumes/data/github/clinical-sci-team/backend/.deer-flow/threads")
#PROMPT = "解读附件图片,每个文件输出一份markdown格式的解读报告，保存为同名的.md文件。"
PROMPT = "并行解读附件照片中的演讲内容,每个照片输出一份markdown格式的解读报告，保存为同名的.md文件。不要追问反问,尽可能客观、详细的解析，转换成中文的文本解读报告。"

BATCH_SIZE = 3
NUM_WORKERS = 2          # 严格并发上限
TASK_TIMEOUT = 1800      # 单次 agent 运行最长 30 分钟
POST_STREAM_POLL = 90    # 流结束后再最多等 90 秒等文件落盘
POLL_INTERVAL = 3        # 轮询文件间隔（秒）
COMPRESS_THRESHOLD = 3 * 1024 * 1024   # 超过 3 MB 则压缩

# ── 日志 ─────────────────────────────────────────────────────────────────────
LOG_FILE = "/tmp/batch_process_snapshots.log"
_fmt = logging.Formatter("%(asctime)s %(message)s")

_fh = logging.FileHandler(LOG_FILE, mode="a")
_fh.setFormatter(_fmt)

_sh = logging.StreamHandler(sys.stdout)
_sh.setFormatter(_fmt)

logging.root.setLevel(logging.INFO)
logging.root.addHandler(_fh)
# 只在交互终端（非重定向）下才加 stdout handler，避免双写
if sys.stdout.isatty():
    logging.root.addHandler(_sh)
log = logging.getLogger(__name__)


def _tag(worker_id: int, batch_id: int) -> str:
    return f"[W{worker_id} B{batch_id:03d}]"


# ── 图片压缩 ──────────────────────────────────────────────────────────────────

def _compress_image(src: Path) -> tuple[Path, bool]:
    """如果 src 超过 COMPRESS_THRESHOLD，用 sips 压缩到 /tmp/，返回 (path, was_compressed)。"""
    if src.stat().st_size <= COMPRESS_THRESHOLD:
        return src, False

    tmp = Path(f"/tmp/{src.stem}_c.jpg")
    result = subprocess.run(
        [
            "sips", "-s", "format", "jpeg",
            "-s", "formatOptions", "75",
            "--resampleHeightWidthMax", "2048",
            str(src), "--out", str(tmp),
        ],
        capture_output=True,
    )
    if result.returncode == 0 and tmp.exists():
        return tmp, True

    # sips 失败时尝试 ImageMagick
    result2 = subprocess.run(
        ["convert", "-resize", "2048x2048>", "-quality", "75", str(src), str(tmp)],
        capture_output=True,
    )
    if result2.returncode == 0 and tmp.exists():
        return tmp, True

    log.warning("压缩失败（sips + convert 均失败），将使用原始文件: %s", src.name)
    return src, False


# ── API 调用 ──────────────────────────────────────────────────────────────────

async def create_thread(client: httpx.AsyncClient) -> str:
    r = await client.post(f"{BASE_URL}/api/langgraph/threads", json={}, timeout=30)
    r.raise_for_status()
    thread_id: str = r.json()["thread_id"]
    await client.patch(
        f"{BASE_URL}/api/langgraph/threads/{thread_id}",
        json={"metadata": {"agent_name": "markdown-transformer"}},
        timeout=30,
    )
    return thread_id


_MIME_MAP = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".heic": "image/heic",
    ".heif": "image/heic",
}


def _mime_type(path: Path) -> str:
    return _MIME_MAP.get(path.suffix.lower(), "image/jpeg")


async def upload_files(client: httpx.AsyncClient, thread_id: str, images: list[Path]) -> list[dict]:
    temps: list[Path] = []
    handles = []
    for p in images:
        upload_path, compressed = _compress_image(p)
        if compressed:
            orig_mb = p.stat().st_size / 1_048_576
            comp_mb = upload_path.stat().st_size / 1_048_576
            log.info("压缩 %s (%.1fMB → %.1fMB)", p.name, orig_mb, comp_mb)
            temps.append(upload_path)
        # 压缩后输出固定为 JPEG；未压缩则按原始扩展名决定 MIME
        mime = "image/jpeg" if compressed else _mime_type(p)
        handles.append((p.name, open(upload_path, "rb"), mime))
    try:
        r = await client.post(
            f"{BASE_URL}/api/threads/{thread_id}/uploads",
            files=[("files", h) for h in handles],
            timeout=120,
        )
        r.raise_for_status()
        return r.json()["files"]
    finally:
        for _, f, _ in handles:
            f.close()
        for t in temps:
            t.unlink(missing_ok=True)


async def stream_run(client: httpx.AsyncClient, thread_id: str, file_infos: list[dict]) -> None:
    """发送消息并完整读取 SSE 直到流关闭；收到 error 事件则抛出异常。"""
    files_meta = [
        {"filename": f["filename"], "size": str(f["size"]),
         "path": f["virtual_path"], "status": "uploaded"}
        for f in file_infos
    ]
    payload = {
        "assistant_id": "lead_agent",
        "input": {
            "messages": [{
                "type": "human",
                "content": [{"type": "text", "text": PROMPT}],
                "additional_kwargs": {"files": files_meta},
            }]
        },
        "config": {
            "recursion_limit": 1000,
            "configurable": {
                "agent_name": "markdown-transformer",
                "model_name": "gpt-5-4",
                "thinking_enabled": True,
                "is_plan_mode": False,
                "subagent_enabled": True,
                "thread_id": thread_id,
            },
        },
        "stream_mode": ["values", "messages", "custom"],
        "stream_subgraphs": True,
    }

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


def find_md_files(thread_id: str, stems: list[str]) -> list[Path]:
    out = THREADS_BASE / thread_id / "user-data" / "outputs"
    if not out.exists():
        return []
    return [out / f"{s}.md" for s in stems if (out / f"{s}.md").exists()]


async def poll_until_files(thread_id: str, stems: list[str]) -> list[Path]:
    """流结束后再等最多 POST_STREAM_POLL 秒，直到 MD 文件全部落盘。"""
    deadline = time.monotonic() + POST_STREAM_POLL
    while time.monotonic() < deadline:
        files = find_md_files(thread_id, stems)
        if len(files) == len(stems):
            return files
        await asyncio.sleep(POLL_INTERVAL)
    return find_md_files(thread_id, stems)


# ── 单次尝试 ──────────────────────────────────────────────────────────────────

async def try_once(tag: str, images: list[Path]) -> bool:
    stems = [p.stem for p in images]
    async with httpx.AsyncClient() as client:
        thread_id = await create_thread(client)
        log.info("%s Thread %s", tag, thread_id)

        file_infos = await upload_files(client, thread_id, images)
        log.info("%s 已上传 %d 个文件", tag, len(file_infos))

        t0 = time.monotonic()
        await stream_run(client, thread_id, file_infos)
        log.info("%s Agent 流结束，耗时 %.1fs", tag, time.monotonic() - t0)

    # 在流关闭后继续轮询文件（httpx client 已关闭，但服务端可能还在写文件）
    md_files = await poll_until_files(thread_id, stems)

    if len(md_files) < len(stems):
        missing = [s for s in stems if not any(f.stem == s for f in md_files)]
        log.warning("%s 缺少 MD 文件: %s", tag, missing)
        return False

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for src in md_files:
        dest = OUTPUT_DIR / src.name
        shutil.copy2(src, dest)
        log.info("%s ✓ 已复制 %s", tag, src.name)
    return True


# ── worker：从队列取批次，失败则无限重试 ─────────────────────────────────────

async def worker(worker_id: int, queue: asyncio.Queue) -> None:
    while True:
        item = await queue.get()
        if item is None:          # 哨兵：退出
            queue.task_done()
            break

        batch_id, images = item
        tag = _tag(worker_id, batch_id)
        names = ", ".join(p.name for p in images)
        log.info("%s 开始处理: %s", tag, names)

        attempt = 0
        while True:
            attempt += 1
            log.info("%s 尝试 #%d", tag, attempt)
            try:
                success = await try_once(tag, images)
            except Exception as exc:
                log.warning("%s 出错: %s", tag, exc)
                success = False

            if success:
                log.info("%s 批次完成（尝试 %d 次）", tag, attempt)
                break

            wait = min(10 * attempt, 120)
            log.info("%s 将在 %ds 后重试…", tag, wait)
            await asyncio.sleep(wait)

        queue.task_done()


# ── main ──────────────────────────────────────────────────────────────────────

async def main() -> None:
    _GLOB_PATTERNS = ("*.jpg", "*.JPG", "*.jpeg", "*.JPEG", "*.png", "*.PNG", "*.heic", "*.HEIC")
    all_images = sorted({f for pat in _GLOB_PATTERNS for f in SNAPSHOTS_DIR.glob(pat)})

    done_stems = {p.stem for p in OUTPUT_DIR.glob("*.md")} if OUTPUT_DIR.exists() else set()
    pending = [f for f in all_images if f.stem not in done_stems]

    batches = [pending[i : i + BATCH_SIZE] for i in range(0, len(pending), BATCH_SIZE)]

    log.info("Snapshots: %d 张 | 已处理: %d 张 | 待处理: %d 张 | 批次: %d | 并发: %d",
             len(all_images), len(done_stems), len(pending), len(batches), NUM_WORKERS)

    if not batches:
        log.info("无待处理文件，退出。")
        return

    queue: asyncio.Queue = asyncio.Queue()
    for i, b in enumerate(batches):
        queue.put_nowait((i + 1, b))
    # 为每个 worker 放一个哨兵
    for _ in range(NUM_WORKERS):
        queue.put_nowait(None)

    # 启动固定数量的 worker，队列为空时自动退出
    workers = [asyncio.create_task(worker(w + 1, queue)) for w in range(NUM_WORKERS)]
    await asyncio.gather(*workers)

    final_done = len(list(OUTPUT_DIR.glob("*.md"))) if OUTPUT_DIR.exists() else 0
    log.info("=== 全部完成，accr/ 共 %d 个 MD 文件 ===", final_done)


if __name__ == "__main__":
    asyncio.run(main())
