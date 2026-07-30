#!/usr/bin/env python3
"""
md2html.py — 将 markdown 报告转换为精美 HTML，通过 Claude Code CLI 生成。

用法:
  python md2html.py report.md                       # 单文件模式（无参考文档）
  python md2html.py ./reports/                      # 目录批量模式（自动识别任务结构）
  python md2html.py -t T03                          # 单任务模式（模糊匹配）
  python md2html.py -t T03 --input-dir /path/to/reports/demo
  python md2html.py ./reports/ --force
  python md2html.py report.md --model sonnet

任务模式 (-t):
  在 --input-dir 目录下查找匹配的 {task_name}_report.md 主报告，
  并读取对应子目录 {task_name}/outputs/ 下的所有 .md 文件作为参考信息。

批量模式 (目录):
  自动识别目录中所有 *_report.md 文件，并为每个文件查找同名子目录的 outputs/ 作为参考。
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path


# ── 配置 ─────────────────────────────────────────────────────────────────────
DEFAULT_INPUT_DIR = Path("/Volumes/data/share/AACR-Confrence/reports")
LOG_FILE = "/tmp/md2html.log"
CLAUDE_LOG_DIR = Path("/tmp/md2html_claude_logs")  # Claude CLI 原始输出目录


# ── 日志 ──────────────────────────────────────────────────────────────────────
_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")

_fh = logging.FileHandler(LOG_FILE, mode="w", encoding="utf-8")
_fh.setFormatter(_fmt)

_sh = logging.StreamHandler(sys.stdout)
_sh.setFormatter(_fmt)

logging.root.setLevel(logging.INFO)
logging.root.addHandler(_fh)
logging.root.addHandler(_sh)
log = logging.getLogger(__name__)


# ── 参数解析 ────────────────────────────────────────────────────────────────
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="md2html.py",
        description="将 markdown 文件转换为精美 HTML 报告（通过 Claude Code CLI）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例:
  python md2html.py report.md                       # 单文件
  python md2html.py ./reports/                      # 目录批量（自动识别任务）
  python md2html.py -t T03                          # 单任务模式（模糊匹配）
  python md2html.py -t T03 --input-dir /path/dir   # 指定搜索目录
  python md2html.py -t T03 --force                  # 强制重新生成
  python md2html.py report.md --model sonnet        # 指定模型""",
    )
    parser.add_argument(
        "input",
        nargs="?",
        default=None,
        help="单个 .md 文件路径 或 包含 .md 文件的目录路径（与 -t 互斥）",
    )
    parser.add_argument(
        "-t", "--task",
        metavar="TASK_NAME",
        default=None,
        help="任务模式：按名称模糊匹配（精确/前缀/子串），查找主报告及 outputs/ 参考文档",
    )
    parser.add_argument(
        "--input-dir",
        metavar="DIR",
        default=None,
        help=f"任务模式的搜索目录（默认: {DEFAULT_INPUT_DIR}）",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="覆盖已存在的 HTML 文件（默认跳过）",
    )
    parser.add_argument(
        "--model",
        default=None,
        metavar="NAME",
        help="指定 Claude 模型（如 sonnet、opus）；默认继承 Claude Code 配置",
    )
    return parser.parse_args()


# ── 前置检查 ────────────────────────────────────────────────────────────────
def check_prerequisites() -> None:
    """检查 claude CLI 可用性，失败则 sys.exit(1)。"""
    if shutil.which("claude") is None:
        log.error("未找到 claude 命令。请确保已安装 Claude Code CLI。")
        log.error("安装说明: https://claude.ai/claude-code")
        sys.exit(1)


# ── 数据结构：任务单元 ──────────────────────────────────────────────────────
class TaskUnit:
    """一个转换任务：主报告 + 可选的参考文档列表。"""
    def __init__(self, report_md: Path, ref_docs: list[Path] | None = None):
        self.report_md = report_md
        self.ref_docs = ref_docs or []

    def __repr__(self) -> str:
        return f"TaskUnit({self.report_md.name}, refs={len(self.ref_docs)})"


# ── 任务模式：模糊匹配查找文件 ────────────────────────────────────────────────
def find_task_units(task_name: str, input_dir: Path) -> list[TaskUnit]:
    """
    在 input_dir 下按任务名模糊匹配，返回 TaskUnit 列表：
      - 主报告: {task_name}_report.md
      - 参考文档: {task_name}/outputs/*.md
    """
    if not input_dir.exists():
        log.error("搜索目录不存在: %s", input_dir)
        sys.exit(1)

    all_reports = sorted(input_dir.glob("*_report.md"))
    matched_reports: list[Path] = []
    for report in all_reports:
        stem = report.stem.removesuffix("_report")
        if stem == task_name or stem.startswith(task_name) or task_name in stem:
            matched_reports.append(report)

    if not matched_reports:
        log.error("在 %s 中找不到匹配 '%s' 的 _report.md 文件", input_dir, task_name)
        if all_reports:
            log.info("可用的报告文件:")
            for r in all_reports[:10]:
                log.info("  %s", r.name)
        sys.exit(1)

    if len(matched_reports) > 1:
        log.warning("'%s' 匹配到多个任务，将全部处理:", task_name)
        for r in matched_reports:
            log.info("  %s", r.name)

    units: list[TaskUnit] = []
    for report in matched_reports:
        stem = report.stem.removesuffix("_report")
        outputs_dir = input_dir / stem / "outputs"
        ref_docs: list[Path] = []
        if outputs_dir.is_dir():
            ref_docs = sorted(outputs_dir.glob("*.md"))
            if ref_docs:
                log.info("  发现 %d 个参考文档: %s", len(ref_docs), outputs_dir)
        else:
            log.warning("  outputs 目录不存在: %s", outputs_dir)
        units.append(TaskUnit(report, ref_docs))

    return units


# ── 批量模式：从目录自动识别任务结构 ─────────────────────────────────────────
def build_task_units_from_dir(input_dir: Path) -> list[TaskUnit]:
    """
    自动识别目录中的任务结构：
      - 每个 *_report.md 作为主报告
      - 查找同名子目录的 outputs/*.md 作为参考文档
    """
    all_reports = sorted(input_dir.glob("*_report.md"))
    if not all_reports:
        all_md = sorted(input_dir.glob("*.md"))
        if not all_md:
            log.warning("目录中未找到 .md 文件: %s", input_dir)
            sys.exit(0)
        log.info("发现 %d 个 markdown 文件（无任务结构，逐个转换）", len(all_md))
        return [TaskUnit(md) for md in all_md]

    log.info("发现 %d 个主报告文件", len(all_reports))
    units: list[TaskUnit] = []
    for report in all_reports:
        stem = report.stem.removesuffix("_report")
        outputs_dir = input_dir / stem / "outputs"
        ref_docs: list[Path] = []
        if outputs_dir.is_dir():
            ref_docs = sorted(outputs_dir.glob("*.md"))
            log.info("  %s: %d 个参考文档", report.name, len(ref_docs))
        else:
            log.info("  %s: 无 outputs 目录", report.name)
        units.append(TaskUnit(report, ref_docs))

    return units


# ── 单文件模式 ────────────────────────────────────────────────────────────────
def build_task_unit_from_file(md_file: Path) -> TaskUnit:
    """单文件模式：尝试智能发现参考文档。"""
    if md_file.suffix.lower() != ".md":
        log.error("输入文件必须是 .md 文件: %s", md_file)
        sys.exit(1)

    ref_docs: list[Path] = []
    if md_file.stem.endswith("_report"):
        stem = md_file.stem.removesuffix("_report")
        outputs_dir = md_file.parent / stem / "outputs"
        if outputs_dir.is_dir():
            ref_docs = sorted(outputs_dir.glob("*.md"))
            if ref_docs:
                log.info("  发现 %d 个参考文档: %s", len(ref_docs), outputs_dir)

    return TaskUnit(md_file, ref_docs)


# ── System Prompt ───────────────────────────────────────────────────────────
SYSTEM_PROMPT = """你是一个专业的学术报告 HTML 生成器。根据用户提供的 markdown 内容，生成一个完整的、精美的单页 HTML 报告。

## ⚠️ 设计风格强制规范（不得违反）

- **绝对禁止暗色/深色主题**（禁止黑色/深灰背景、霓虹色、荧光色、发光效果）
- **必须使用浅色主题**：`body { background: #f5f5f7; color: #1d1d1f; }`
- **字体**：Outfit（标题/UI）+ Source Serif 4（正文）+ JetBrains Mono（代码/数据）
- **Google Fonts CDN**：`https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600;700;800&family=Source+Serif+4:ital,wght@0,300;0,400;0,600;1,400&family=JetBrains+Mono:wght@400;500&display=swap`
- **marked.js CDN**：`https://cdn.jsdelivr.net/npm/marked/marked.min.js`

## CSS 变量（必须原样使用）

```css
:root {
  --white:#ffffff; --bg:#f5f5f7; --bg2:#fbfbfd;
  --text:#1d1d1f; --text2:#424245; --text3:#6e6e73; --text4:#86868b;
  --blue:#0071e3; --blue-light:#e8f2ff; --teal:#00b4d8;
  --green:#28cd41; --orange:#ff9f0a; --red:#ff453a; --purple:#bf5af2;
  --border:rgba(0,0,0,0.08); --border-mid:rgba(0,0,0,0.12);
  --shadow-sm:0 2px 12px rgba(0,0,0,0.07); --shadow-md:0 8px 32px rgba(0,0,0,0.10);
  --radius:18px; --radius-sm:10px;
}
```

## 页面结构（必须完整实现）

### 固定导航栏 `.top-nav`（高 52px，毛玻璃 `background:rgba(255,255,255,0.72); backdrop-filter:blur(20px)`）
- 左：`.nav-logo`（蓝色 `.dot` 圆点 + 报告编号/标题缩写）
- 中：`.nav-links`（各 section 锚点链接，active 时 `color:var(--blue); background:var(--blue-light)`）
- 右：`.view-toggle`（可视化/原文切换按钮）+ `.nav-badge`（渐变蓝紫徽章）

### Markdown 原文视图（`id="md-view"`，默认 `display:none`）
`<div id="md-view"><div class="md-container"><div id="md-content">加载中...</div></div></div>`

### Report 视图（`id="report-view"`）
1. **Hero**（`id="top"`，`min-height:100vh`，两栏 grid 1.2fr/1fr）
   - 左：`h1.hero-title`（关键词用 `<em>` 蓝色渐变）、`.hero-subtitle`、`.hero-meta`（`.meta-tag` 胶囊）
   - 右：`.hero-stats`（2×2 grid，4个 `.stat-card`，颜色依次 blue/teal/green/purple）
   - Hero 背景：`linear-gradient(180deg, var(--white) 0%, var(--bg) 100%)`

2. **内容 section**（每个 `##` 对应一个，交替 `.bg-white`/`.bg-alt`）
   - 结构：`.section-full.bg-xxx > .section（max-width:1200px）> .section-header > 内容体`
   - `.section-header`：居中，包含 `.section-number`（蓝色大写字母）、`h2.section-title`、`.section-subtitle`

3. **Footer**（`.report-footer`）：左报告标题，右版本号

## 内容组件映射

| 内容类型 | 组件 |
|---------|------|
| 核心研判/结论 | `.judgment-card`（白色卡片，带 `h4` + `p`）|
| 风险/限制列表 | `.risk-list > .risk-item`（橙色圆点 + 文字）|
| 机制流程 | `.mechanism-flow`（flex wrap）+ `.flow-node`/`.flow-node.highlight`/`.flow-arrow` |
| 数据表 | `.data-table`（圆角，带 `.val` 蓝色等宽字体）|
| 竞争对比 | `.compare-wrapper > .compare-table`（`.asset-name.primary` 蓝色）|
| 免疫/信号链 | `.icd-chain > .icd-node`（绿色）+ `.icd-arrow` |
| 创新卡片 | `.innovation-grid`（3列）`> .innovation-card`（`.layer-tag` 蓝色小标）|
| 评分环 | `.score-grid`（4列）`> .score-item > .score-ring`（SVG circle，`stroke-dasharray:188.5`）|
| 时间线 | `.timeline > .timeline-item.completed/.pending`（左侧蓝紫渐变竖线）|
| 缺口/手风琴 | `.gap-priority > .accord-item`（`+`/`×` 切换，`max-height` 动画）|
| 标签组 | `.summary-chips > .chip`（圆角，前3个颜色 blue/teal/purple）|

## JavaScript（必须完整包含）

```javascript
function switchView(v){
  const r=document.getElementById('report-view'),m=document.getElementById('md-view'),b=document.querySelectorAll('.view-toggle button');
  if(v==='md'){r.style.display='none';m.style.display='block';b[0].classList.remove('active');b[1].classList.add('active');loadMarkdown();}
  else{r.style.display='block';m.style.display='none';b[0].classList.add('active');b[1].classList.remove('active');}
}
let mdLoaded=false;
function loadMarkdown(){
  if(mdLoaded)return;
  fetch('MD_FILENAME').then(r=>{if(!r.ok)throw 0;return r.text();})
  .then(t=>{document.getElementById('md-content').innerHTML=marked.parse(t);mdLoaded=true;})
  .catch(()=>{document.getElementById('md-content').innerHTML='<p style="text-align:center;padding:60px 0;color:var(--text3)">无法加载Markdown文件</p>';});
}
function toggleAccord(h){
  const item=h.parentElement,body=item.querySelector('.accord-body'),open=item.classList.contains('open');
  if(open){body.style.maxHeight='0';item.classList.remove('open');}
  else{body.style.maxHeight=body.scrollHeight+'px';item.classList.add('open');}
}
const _obs=new IntersectionObserver(es=>es.forEach(e=>{if(e.isIntersecting)e.target.classList.add('visible');}),{threshold:0.1});
document.querySelectorAll('.animate-in').forEach(el=>_obs.observe(el));
const _secs=document.querySelectorAll('section[id],.hero[id]'),_navs=document.querySelectorAll('.nav-links a');
function _updateNav(){let c='';_secs.forEach(s=>{if(window.scrollY>=s.offsetTop-100)c=s.id;});_navs.forEach(a=>{a.classList.toggle('active',a.getAttribute('href')==='#'+c);});}
window.addEventListener('scroll',_updateNav,{passive:true});_updateNav();
```

## 输出要求（极其重要）

1. 使用 Write 工具将完整 HTML 写入用户指定的「HTML 输出路径」
2. 完整 `<!DOCTYPE html lang="zh-CN">` 到 `</html>`
3. `<title>` 用 markdown 第一个 `#` 标题
4. fetch 路径写为占位符 `MD_FILENAME`（脚本自动替换）
5. 所有 CSS/JS 内联，仅 Google Fonts 和 marked.js 用 CDN
6. 写入完成后只需简短回复「已生成: [文件路径]」
"""


# ── 单任务处理 ──────────────────────────────────────────────────────────────
def process_task(task: TaskUnit, force: bool, model: str | None) -> str:
    """
    处理一个任务单元，claude 通过 Write 工具将 HTML 写入与 md 同目录同名 .html 文件。
    返回 'ok'（生成成功）、'skipped'（跳过）或 'fail'（失败）。
    """
    md_file = task.report_md
    html_file = md_file.with_suffix(".html")

    if html_file.exists() and not force:
        log.info("[跳过] %s（已存在 %s，使用 --force 覆盖）", md_file.name, html_file.name)
        return "skipped"

    log.info("[处理] %s → %s", md_file.name, html_file.name)

    # 读取 markdown 内容
    md_content = md_file.read_text(encoding="utf-8")
    log.info("  主报告: %d 字符", len(md_content))

    # 读取参考文档内容（跳过与主报告重复的文件）
    ref_section = ""
    if task.ref_docs:
        log.info("  加载 %d 个参考文档...", len(task.ref_docs))
        ref_parts: list[str] = []
        total_ref_chars = 0
        MAX_REF_CHARS = 20000
        MAX_PER_FILE  = 4000
        for ref_file in task.ref_docs:
            # 跳过与主报告同名或同内容的文件
            if ref_file.resolve() == md_file.resolve():
                log.info("  - %s（与主报告为同一文件，跳过）", ref_file.name)
                continue
            if total_ref_chars >= MAX_REF_CHARS:
                log.warning("  已达参考内容上限（%d 字符），跳过后续文档", MAX_REF_CHARS)
                break
            try:
                ref_content = ref_file.read_text(encoding="utf-8")
                remaining = MAX_REF_CHARS - total_ref_chars
                limit = min(MAX_PER_FILE, remaining)
                truncated = len(ref_content) > limit
                if truncated:
                    ref_content = ref_content[:limit] + "\n...(内容已截断)"
                total_ref_chars += len(ref_content)
                ref_parts.append(f"### 参考文档: {ref_file.name}\n{ref_content}")
                log.info("  + %s (%d 字符%s)", ref_file.name, len(ref_content), ", 已截断" if truncated else "")
            except Exception as e:
                log.warning("  读取参考文档失败: %s: %s", ref_file.name, e)
        if ref_parts:
            log.info("  参考文档共 %d 字符", total_ref_chars)
            ref_section = (
                "\n\n--- 参考文档（用于丰富 HTML 内容展示，提取关键数据和信息） ---\n"
                + "\n\n".join(ref_parts)
                + "\n--- 参考文档结束 ---"
            )

    # 注意：SYSTEM_PROMPT 通过 --system-prompt-file 参数传递，此处 combined_prompt 仅为用户消息
    combined_prompt = (
        f"请将以下 markdown 内容转换为精美的单页 HTML 报告。\n"
        f"{'参考文档中包含额外的数据和分析信息，请整合到 HTML 可视化展示中（如 stats 卡片、对比表格、时间线等组件）。' if ref_section else ''}\n\n"
        f"markdown 文件名：{md_file.name}\n"
        f"HTML 输出路径：{html_file}\n\n"
        f"--- markdown 主报告内容开始 ---\n"
        f"{md_content}\n"
        f"--- markdown 主报告内容结束 ---"
        f"{ref_section}"
    )

    prompt_size = len(combined_prompt)
    log.info("  Prompt 总大小: %d 字符 (%.1f KB)", prompt_size, prompt_size / 1024)

    # 禁用 microsoft-docs MCP 工具（避免不必要的联网查询）
    _blocked = (
        "mcp__plugin_microsoft-docs_microsoft-learn__microsoft_code_sample_search,"
        "mcp__plugin_microsoft-docs_microsoft-learn__microsoft_docs_search"
    )

    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        if attempt > 1:
            log.warning("[重试 %d/%d] %s", attempt, max_attempts, md_file.name)

        sys_prompt_file: Path | None = None
        try:
            # ── 写入系统提示到临时文件 ─────────────────────────────────────────
            with tempfile.NamedTemporaryFile(
                mode="w", suffix="_sysprompt.txt", delete=False, encoding="utf-8"
            ) as spf:
                spf.write(SYSTEM_PROMPT)
                sys_prompt_file = Path(spf.name)

            # ── 构建 claude 命令 ───────────────────────────────────────────────
            # claude 会使用 Write 工具将 HTML 写入指定路径
            cmd = [
                "claude", "-p",
                "--dangerously-skip-permissions",
                "--system-prompt-file", str(sys_prompt_file),
                "--disable-slash-commands",
                "--output-format", "text",
                "--disallowed-tools", _blocked,
            ]
            if model:
                cmd += ["--model", model]

            log.info("  调用 claude CLI（尝试 %d/%d）...", attempt, max_attempts)
            log.info("  期望输出: %s", html_file)
            t0 = time.monotonic()

            env = os.environ.copy()
            env["ANTHROPIC_TIMEOUT"] = "1200000"   # 20 分钟

            # 准备 Claude 日志目录
            CLAUDE_LOG_DIR.mkdir(parents=True, exist_ok=True)
            task_stem = md_file.stem
            stdout_log = CLAUDE_LOG_DIR / f"{task_stem}_attempt{attempt}_stdout.txt"
            log.info("  Claude 日志: %s", stdout_log)

            # 记录 html_file 在调用前的修改时间（用于判断是否新写入）
            html_mtime_before = html_file.stat().st_mtime if html_file.exists() else 0

            # 通过 stdin pipe 传递用户 prompt，实时流式写入日志

            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                env=env,
            )

            # 先写入 stdin 再关闭（非阻塞发送 prompt）
            proc.stdin.write(combined_prompt)
            proc.stdin.close()

            # 实时 drain stdout/stderr 到日志文件
            stdout_chunks: list[str] = []
            stderr_chunks: list[str] = []

            def _drain_stdout(pipe, log_file: Path, chunks: list[str]):
                with open(log_file, "w", encoding="utf-8") as f:
                    for line in pipe:
                        f.write(line)
                        f.flush()
                        chunks.append(line)

            def _drain_stderr(pipe, chunks: list[str]):
                for line in pipe:
                    chunks.append(line)

            t_out = threading.Thread(
                target=_drain_stdout, args=(proc.stdout, stdout_log, stdout_chunks)
            )
            t_err = threading.Thread(
                target=_drain_stderr, args=(proc.stderr, stderr_chunks)
            )
            t_out.start()
            t_err.start()

            # 等待完成（带超时）
            TIMEOUT_SECS = 1200  # 20 分钟
            t_out.join(timeout=TIMEOUT_SECS)
            if t_out.is_alive():
                proc.kill()
                t_out.join(timeout=5)
                t_err.join(timeout=5)
                log.error("  claude 调用超时（%ds）: %s", TIMEOUT_SECS, md_file.name)
                if attempt < max_attempts:
                    continue
                return "fail"

            t_err.join(timeout=10)
            proc.wait()

            stdout_text = "".join(stdout_chunks)
            stderr_text = "".join(stderr_chunks)

            elapsed = time.monotonic() - t0
            log.info("  claude 完成，耗时 %.1fs，退出码 %d，stdout %d 字符",
                     elapsed, proc.returncode, len(stdout_text))

        except FileNotFoundError:
            log.error("claude 命令未找到")
            return "fail"
        finally:
            if sys_prompt_file:
                sys_prompt_file.unlink(missing_ok=True)

        if proc.returncode != 0:
            log.error("[错误] claude 调用失败（退出码 %d）: %s", proc.returncode, md_file.name)
            if stdout_text:
                log.error("  stdout: %s", stdout_text[:300])
            if stderr_text:
                log.error("  stderr: %s", stderr_text[:300])
            if attempt < max_attempts:
                continue
            return "fail"

        # ── 检测输出结果 ───────────────────────────────────────────────────────
        # 策略：优先检查 claude 是否通过 Write 工具写了 html_file
        # 如果没有，再尝试从 stdout 提取 HTML

        html_written_by_tool = (
            html_file.exists()
            and html_file.stat().st_mtime > html_mtime_before
            and html_file.stat().st_size > 1000
        )

        if html_written_by_tool:
            html_size = html_file.stat().st_size
            log.info("[完成] claude 已写入: %s (%.1f KB)", html_file, html_size / 1024)
            # 替换占位符
            content = html_file.read_text(encoding="utf-8")
            if "MD_FILENAME" in content:
                content = content.replace("MD_FILENAME", md_file.name)
                html_file.write_text(content, encoding="utf-8")
            return "ok"

        # 如果 claude 没有通过工具写文件，尝试从 stdout 提取 HTML
        raw_output = stdout_text
        raw_output = re.sub(r"<think>.*?</think>", "", raw_output, flags=re.DOTALL).strip()
        log.info("  未检测到文件写入，尝试从 stdout 提取 HTML（%d 字符）", len(raw_output))

        match = re.search(r"(<!DOCTYPE\s+html.*?</html>)", raw_output, re.IGNORECASE | re.DOTALL)
        if match:
            html_output = match.group(1)
            log.info("  从 stdout 提取 HTML: %d 字符", len(html_output))
        else:
            start = re.search(r"<!DOCTYPE\s+html", raw_output, re.IGNORECASE)
            if not start:
                log.warning("  stdout 中未找到 HTML（前200字符: %s）", raw_output[:200])
                if attempt < max_attempts:
                    log.warning("  重试中...")
                    continue
                log.error("[失败] %d 次尝试均未输出有效 HTML: %s", max_attempts, md_file.name)
                return "fail"
            html_output = raw_output[start.start():].strip()
            if not html_output.endswith("</html>"):
                if not html_output.endswith("</body>"):
                    html_output += "\n</body>"
                html_output += "\n</html>"
            log.warning("  HTML 输出被截断，已自动补全（%d 字符）", len(html_output))

        # 替换占位符并写入
        html_output = html_output.replace("MD_FILENAME", md_file.name)
        html_file.write_text(html_output, encoding="utf-8")
        html_size = html_file.stat().st_size
        log.info("[完成] 已生成: %s (%.1f KB)", html_file, html_size / 1024)
        return "ok"

    return "fail"


def main() -> None:
    args = parse_args()

    log.info("=" * 50)
    log.info("md2html.py 启动  日志: %s", LOG_FILE)
    log.info("=" * 50)

    if args.task and args.input:
        log.error("-t/--task 与位置参数 input 不能同时使用")
        sys.exit(1)
    if not args.task and not args.input:
        log.error("需要指定 input 路径或 -t/--task 任务名称")
        sys.exit(1)

    check_prerequisites()

    tasks: list[TaskUnit] = []

    if args.task:
        input_dir = Path(args.input_dir) if args.input_dir else DEFAULT_INPUT_DIR
        if not input_dir.exists():
            log.error("搜索目录不存在: %s", input_dir)
            sys.exit(1)
        tasks = find_task_units(args.task, input_dir)
    else:
        input_path = Path(args.input)
        if not input_path.exists():
            log.error("路径不存在: %s", input_path)
            sys.exit(1)
        if input_path.is_file():
            tasks = [build_task_unit_from_file(input_path)]
        elif input_path.is_dir():
            tasks = build_task_units_from_dir(input_path)
        else:
            log.error("路径既不是文件也不是目录: %s", input_path)
            sys.exit(1)

    if not tasks:
        log.warning("未找到需要处理的任务。")
        sys.exit(0)

    total = len(tasks)
    failed = 0
    skipped = 0

    log.info("")
    log.info("开始处理 %d 个任务", total)
    log.info("-" * 50)

    for i, task in enumerate(tasks, 1):
        log.info("")
        log.info("[%d/%d] %s  (refs=%d)", i, total, task.report_md.name, len(task.ref_docs))
        status = process_task(task, force=args.force, model=args.model)
        if status == "fail":
            failed += 1
        elif status == "skipped":
            skipped += 1

    generated = total - failed - skipped
    log.info("")
    log.info("=" * 50)
    log.info("处理完成  生成: %d  跳过: %d  失败: %d", generated, skipped, failed)
    log.info("=" * 50)

    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
