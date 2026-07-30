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

## ⚠️ 极其重要：设计风格强制规范

**本系列报告必须完全统一的视觉风格，严格按照以下规范执行，禁止任何偏离：**

- **绝对禁止暗色/深色主题**（禁止黑色/深灰背景、霓虹色、荧光色、发光效果）
- **必须使用浅色主题**：白色/浅灰底色，深色文字，蓝色主色调（Apple 设计语言）
- **字体严格限定**：Outfit（标题/UI）+ Source Serif 4（正文）+ JetBrains Mono（代码/数据）
- **配色严格按照下方 CSS 变量**，不得自行发明其他颜色变量或色系

---

## CSS 变量（必须原样使用，不得修改、新增或替换）

```css
:root {
  --white: #ffffff;
  --bg: #f5f5f7;
  --bg2: #fbfbfd;
  --text: #1d1d1f;
  --text2: #424245;
  --text3: #6e6e73;
  --text4: #86868b;
  --blue: #0071e3;
  --blue-light: #e8f2ff;
  --blue-mid: #0077ed;
  --teal: #00b4d8;
  --green: #28cd41;
  --orange: #ff9f0a;
  --red: #ff453a;
  --purple: #bf5af2;
  --border: rgba(0,0,0,0.08);
  --border-mid: rgba(0,0,0,0.12);
  --shadow-sm: 0 2px 12px rgba(0,0,0,0.07);
  --shadow-md: 0 8px 32px rgba(0,0,0,0.10);
  --shadow-lg: 0 20px 60px rgba(0,0,0,0.12);
  --radius: 18px;
  --radius-sm: 10px;
}
```

---

## 字体加载（必须原样引用此 link 标签）

```html
<link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600;700;800&family=Source+Serif+4:ital,wght@0,300;0,400;0,600;1,400&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
```

---

## 必须包含的完整 CSS 基础样式

以下 CSS 必须原样写入 `<style>` 标签，不得删减：

```css
* { margin: 0; padding: 0; box-sizing: border-box; }
html { scroll-behavior: smooth; scroll-padding-top: 64px; }
body {
  font-family: 'Source Serif 4', serif;
  color: var(--text);
  background: var(--bg);
  line-height: 1.8;
  font-size: 16px;
  -webkit-font-smoothing: antialiased;
}
h1, h2, h3, h4, h5, h6 { font-family: 'Outfit', sans-serif; line-height: 1.2; }
code, pre { font-family: 'JetBrains Mono', monospace; }

/* ── Nav ── */
.top-nav {
  position: fixed; top: 0; left: 0; right: 0; height: 52px;
  backdrop-filter: saturate(180%) blur(20px);
  -webkit-backdrop-filter: saturate(180%) blur(20px);
  background: rgba(255,255,255,0.72);
  border-bottom: 1px solid var(--border);
  z-index: 1000; display: flex; align-items: center;
  justify-content: space-between; padding: 0 24px;
}
.nav-logo { display: flex; align-items: center; gap: 8px; font-family: 'Outfit', sans-serif; font-weight: 600; font-size: 14px; color: var(--text); white-space: nowrap; }
.nav-logo .dot { width: 10px; height: 10px; border-radius: 50%; background: var(--blue); flex-shrink: 0; }
.nav-links { display: flex; gap: 4px; align-items: center; overflow-x: auto; max-width: 50%; }
.nav-links a { font-family: 'Outfit', sans-serif; font-size: 12px; font-weight: 500; color: var(--text3); text-decoration: none; padding: 6px 10px; border-radius: 6px; transition: all 0.2s; white-space: nowrap; }
.nav-links a:hover, .nav-links a.active { color: var(--blue); background: var(--blue-light); }
.nav-right { display: flex; align-items: center; gap: 8px; }
.view-toggle { display: flex; background: var(--bg); border-radius: 8px; padding: 3px; }
.view-toggle button { font-family: 'Outfit', sans-serif; font-size: 12px; font-weight: 500; border: none; padding: 5px 12px; border-radius: 6px; cursor: pointer; background: transparent; color: var(--text3); transition: all 0.2s; }
.view-toggle button.active { background: var(--white); color: var(--blue); box-shadow: var(--shadow-sm); }
.nav-badge { font-family: 'Outfit', sans-serif; font-size: 10px; font-weight: 600; background: linear-gradient(135deg, var(--blue), var(--purple)); color: white; padding: 3px 8px; border-radius: 4px; letter-spacing: 0.5px; }

/* ── Markdown View ── */
#md-view { display: none; padding-top: 72px; min-height: 100vh; }
.md-container { max-width: 800px; margin: 0 auto; padding: 40px 24px; }
#md-content { background: var(--white); border-radius: var(--radius); padding: 48px; box-shadow: var(--shadow-sm); line-height: 1.9; }
#md-content h1, #md-content h2, #md-content h3 { margin-top: 2em; margin-bottom: 0.5em; }
#md-content p { margin-bottom: 1em; }
#md-content code { background: var(--bg); padding: 2px 6px; border-radius: 4px; font-size: 0.9em; }
#md-content pre { background: var(--text); color: #f0f0f0; padding: 20px; border-radius: var(--radius-sm); overflow-x: auto; margin: 1em 0; }
#md-content table { width: 100%; border-collapse: collapse; margin: 1em 0; }
#md-content th, #md-content td { padding: 10px 14px; border: 1px solid var(--border-mid); text-align: left; font-size: 14px; }
#md-content th { background: var(--bg); font-weight: 600; }

/* ── Report View ── */
#report-view { padding-top: 52px; }

/* ── Hero ── */
.hero {
  min-height: 100vh; display: flex; align-items: center;
  padding: 80px 48px;
  background: linear-gradient(180deg, var(--white) 0%, var(--bg) 100%);
  position: relative; overflow: hidden;
}
.hero::before { content: ''; position: absolute; top: -200px; right: -200px; width: 600px; height: 600px; border-radius: 50%; background: radial-gradient(circle, rgba(0,113,227,0.05) 0%, transparent 70%); pointer-events: none; }
.hero::after  { content: ''; position: absolute; bottom: -150px; left: -100px; width: 400px; height: 400px; border-radius: 50%; background: radial-gradient(circle, rgba(191,90,242,0.04) 0%, transparent 70%); pointer-events: none; }
.hero-inner { max-width: 1200px; margin: 0 auto; display: grid; grid-template-columns: 1.2fr 1fr; gap: 60px; align-items: center; width: 100%; }
.hero-title { font-size: clamp(2.4rem, 5vw, 3.6rem); font-weight: 800; letter-spacing: -0.02em; line-height: 1.15; margin-bottom: 20px; color: var(--text); }
.hero-title em { font-style: normal; background: linear-gradient(135deg, var(--blue), var(--purple)); -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text; }
.hero-subtitle { font-size: 17px; color: var(--text2); line-height: 1.8; margin-bottom: 28px; max-width: 560px; }
.hero-meta { display: flex; flex-wrap: wrap; gap: 12px; }
.meta-tag { font-family: 'Outfit', sans-serif; font-size: 12px; font-weight: 500; color: var(--text3); background: var(--white); border: 1px solid var(--border); padding: 6px 14px; border-radius: 20px; display: flex; align-items: center; gap: 6px; }
.meta-tag .label { color: var(--text4); }
.meta-tag .value { color: var(--text); font-weight: 600; }
.hero-stats { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
.stat-card { background: var(--white); border-radius: var(--radius); padding: 24px; border: 1px solid var(--border); box-shadow: var(--shadow-sm); transition: all 0.3s ease; }
.stat-card:hover { transform: translateY(-4px); box-shadow: var(--shadow-md); }
.stat-label { font-family: 'Outfit', sans-serif; font-size: 12px; font-weight: 500; color: var(--text4); text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 8px; }
.stat-value { font-family: 'Outfit', sans-serif; font-size: 28px; font-weight: 700; color: var(--text); margin-bottom: 4px; }
.stat-desc { font-family: 'Outfit', sans-serif; font-size: 12px; color: var(--text3); }
.stat-card:nth-child(1) .stat-value { color: var(--blue); }
.stat-card:nth-child(2) .stat-value { color: var(--teal); }
.stat-card:nth-child(3) .stat-value { color: var(--green); }
.stat-card:nth-child(4) .stat-value { color: var(--purple); }

/* ── Sections ── */
.section { padding: 80px 48px; max-width: 1200px; margin: 0 auto; }
.section-full { padding: 80px 48px; }
.bg-white { background: var(--white); }
.bg-alt { background: var(--bg); }
.section-header { text-align: center; margin-bottom: 48px; }
.section-number { font-family: 'Outfit', sans-serif; font-size: 12px; font-weight: 600; color: var(--blue); letter-spacing: 2px; text-transform: uppercase; margin-bottom: 8px; }
.section-title { font-size: 2rem; font-weight: 700; color: var(--text); margin-bottom: 12px; }
.section-subtitle { font-size: 15px; color: var(--text3); max-width: 600px; margin: 0 auto; }

/* ── Summary chips ── */
.summary-chips { display: flex; justify-content: center; gap: 12px; margin-bottom: 40px; flex-wrap: wrap; }
.chip { font-family: 'Outfit', sans-serif; font-size: 13px; font-weight: 500; padding: 8px 20px; border-radius: 24px; background: var(--blue-light); color: var(--blue); border: 1px solid rgba(0,113,227,0.15); }
.chip:nth-child(2) { background: rgba(0,180,216,0.08); color: var(--teal); border-color: rgba(0,180,216,0.15); }
.chip:nth-child(3) { background: rgba(191,90,242,0.08); color: var(--purple); border-color: rgba(191,90,242,0.15); }

/* ── Judgment / callout card ── */
.judgment-card { background: var(--white); border-radius: var(--radius); padding: 32px; border: 1px solid var(--border); box-shadow: var(--shadow-sm); margin-bottom: 32px; max-width: 900px; margin-left: auto; margin-right: auto; }
.judgment-card h4 { font-size: 16px; font-weight: 600; color: var(--text); margin-bottom: 12px; display: flex; align-items: center; gap: 8px; }
.judgment-card p { color: var(--text2); font-size: 15px; line-height: 1.8; }

/* ── Risk list ── */
.risk-list { max-width: 900px; margin: 0 auto 32px; }
.risk-item { display: flex; align-items: flex-start; gap: 12px; padding: 16px 0; border-bottom: 1px solid var(--border); }
.risk-item:last-child { border-bottom: none; }
.risk-icon { width: 8px; height: 8px; border-radius: 50%; background: var(--orange); margin-top: 8px; flex-shrink: 0; }
.risk-item .risk-text { font-size: 15px; color: var(--text2); line-height: 1.6; }

/* ── Mechanism flow ── */
.mechanism-flow { display: flex; flex-wrap: wrap; align-items: center; justify-content: center; gap: 8px; padding: 40px 20px; margin-bottom: 40px; }
.flow-node { font-family: 'Outfit', sans-serif; font-size: 13px; font-weight: 500; padding: 10px 18px; border-radius: var(--radius-sm); background: var(--white); border: 1px solid var(--border); box-shadow: var(--shadow-sm); color: var(--text); text-align: center; max-width: 180px; }
.flow-node.highlight { background: linear-gradient(135deg, var(--blue-light), rgba(191,90,242,0.06)); border-color: rgba(0,113,227,0.2); color: var(--blue); font-weight: 600; }
.flow-arrow { font-size: 18px; color: var(--text4); }

/* ── Data / compare tables ── */
.data-table { width: 100%; max-width: 700px; margin: 0 auto 40px; border-collapse: separate; border-spacing: 0; background: var(--white); border-radius: var(--radius); overflow: hidden; box-shadow: var(--shadow-sm); border: 1px solid var(--border); }
.data-table th { font-family: 'Outfit', sans-serif; font-size: 12px; font-weight: 600; color: var(--text3); text-transform: uppercase; letter-spacing: 0.5px; padding: 14px 20px; background: var(--bg2); text-align: left; border-bottom: 1px solid var(--border); }
.data-table td { font-size: 14px; padding: 14px 20px; border-bottom: 1px solid var(--border); color: var(--text2); }
.data-table tr:last-child td { border-bottom: none; }
.data-table .val { font-family: 'JetBrains Mono', monospace; font-weight: 500; color: var(--blue); }
.compare-wrapper { overflow-x: auto; border-radius: var(--radius); box-shadow: var(--shadow-sm); border: 1px solid var(--border); max-width: 1100px; margin: 0 auto; }
.compare-table { width: 100%; border-collapse: collapse; background: var(--white); min-width: 800px; }
.compare-table th { font-family: 'Outfit', sans-serif; font-size: 12px; font-weight: 600; color: var(--text3); text-transform: uppercase; letter-spacing: 0.5px; padding: 16px 18px; background: var(--bg2); text-align: left; border-bottom: 1px solid var(--border-mid); white-space: nowrap; }
.compare-table td { font-size: 13px; padding: 16px 18px; border-bottom: 1px solid var(--border); color: var(--text2); vertical-align: top; line-height: 1.5; }
.compare-table tr:last-child td { border-bottom: none; }
.compare-table .asset-name { font-family: 'Outfit', sans-serif; font-weight: 600; color: var(--text); }
.compare-table .asset-name.primary { color: var(--blue); }

/* ── Innovation cards ── */
.innovation-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 20px; max-width: 900px; margin: 0 auto 48px; }
.innovation-card { background: var(--white); border-radius: var(--radius); padding: 28px; border: 1px solid var(--border); box-shadow: var(--shadow-sm); transition: all 0.3s ease; }
.innovation-card:hover { transform: translateY(-4px); box-shadow: var(--shadow-md); }
.innovation-card .layer-tag { font-family: 'Outfit', sans-serif; font-size: 11px; font-weight: 600; color: var(--blue); text-transform: uppercase; letter-spacing: 1px; margin-bottom: 12px; }
.innovation-card h4 { font-size: 16px; font-weight: 600; margin-bottom: 8px; }
.innovation-card p { font-size: 14px; color: var(--text3); line-height: 1.6; }

/* ── Score grid ── */
.score-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; max-width: 900px; margin: 0 auto; }
.score-item { text-align: center; background: var(--white); border-radius: var(--radius); padding: 24px 16px; border: 1px solid var(--border); }
.score-ring { width: 72px; height: 72px; margin: 0 auto 12px; position: relative; }
.score-ring svg { transform: rotate(-90deg); width: 100%; height: 100%; }
.score-ring .bg-ring { fill: none; stroke: var(--bg); stroke-width: 6; }
.score-ring .fg-ring { fill: none; stroke: var(--blue); stroke-width: 6; stroke-linecap: round; transition: stroke-dashoffset 1s ease; }
.score-num { position: absolute; top: 50%; left: 50%; transform: translate(-50%,-50%); font-family: 'Outfit', sans-serif; font-size: 16px; font-weight: 700; color: var(--text); }
.score-label { font-family: 'Outfit', sans-serif; font-size: 12px; font-weight: 500; color: var(--text3); }

/* ── Timeline ── */
.timeline { position: relative; max-width: 800px; margin: 0 auto; padding: 20px 0; }
.timeline::before { content: ''; position: absolute; left: 24px; top: 0; bottom: 0; width: 2px; background: linear-gradient(180deg, var(--blue), var(--purple)); border-radius: 1px; }
.timeline-item { position: relative; padding-left: 64px; padding-bottom: 36px; }
.timeline-item:last-child { padding-bottom: 0; }
.timeline-dot { position: absolute; left: 16px; top: 4px; width: 18px; height: 18px; border-radius: 50%; background: var(--white); border: 3px solid var(--blue); box-shadow: 0 0 0 4px rgba(0,113,227,0.1); }
.timeline-item.completed .timeline-dot { background: var(--blue); }
.timeline-item.pending .timeline-dot { border-color: var(--text4); box-shadow: 0 0 0 4px rgba(0,0,0,0.04); }
.timeline-title { font-family: 'Outfit', sans-serif; font-size: 15px; font-weight: 600; color: var(--text); margin-bottom: 6px; }
.timeline-desc { font-size: 14px; color: var(--text3); line-height: 1.6; }

/* ── Gap priority ── */
.gap-priority { max-width: 900px; margin: 0 auto 40px; }
.gap-priority-header { font-family: 'Outfit', sans-serif; font-size: 14px; font-weight: 600; margin-bottom: 16px; display: flex; align-items: center; gap: 8px; }
.priority-badge { font-size: 11px; font-weight: 700; padding: 3px 8px; border-radius: 4px; color: white; }
.priority-badge.p0 { background: var(--red); }
.priority-badge.p1 { background: var(--orange); }
.priority-badge.p2 { background: var(--text4); }

/* ── Accordion ── */
.accord-item { background: var(--white); border-radius: var(--radius-sm); border: 1px solid var(--border); margin-bottom: 8px; overflow: hidden; }
.accord-head { font-family: 'Outfit', sans-serif; font-size: 14px; font-weight: 500; padding: 16px 20px; cursor: pointer; display: flex; align-items: center; justify-content: space-between; transition: background 0.2s; }
.accord-head:hover { background: var(--bg2); }
.accord-head::after { content: '+'; font-size: 18px; color: var(--text4); transition: transform 0.3s; }
.accord-item.open .accord-head::after { transform: rotate(45deg); }
.accord-body { max-height: 0; overflow: hidden; transition: max-height 0.3s ease; }
.accord-body-inner { padding: 0 20px 16px; font-size: 14px; color: var(--text3); line-height: 1.7; }

/* ── ICD chain ── */
.icd-chain { display: flex; flex-wrap: wrap; align-items: center; justify-content: center; gap: 12px; padding: 24px; background: var(--white); border-radius: var(--radius); border: 1px solid var(--border); max-width: 800px; margin: 0 auto; }
.icd-node { font-family: 'Outfit', sans-serif; font-size: 12px; font-weight: 500; padding: 8px 14px; border-radius: 8px; background: rgba(40,205,65,0.08); color: #1a8f2d; border: 1px solid rgba(40,205,65,0.15); }
.icd-arrow { color: var(--text4); font-size: 14px; }

/* ── Footer ── */
.report-footer { padding: 24px 48px; border-top: 1px solid var(--border); display: flex; justify-content: space-between; align-items: center; background: var(--white); }
.footer-left { font-family: 'Outfit', sans-serif; font-size: 12px; color: var(--text4); }
.footer-right { font-family: 'Outfit', sans-serif; font-size: 11px; font-weight: 600; color: var(--text4); letter-spacing: 0.5px; }

/* ── Animations ── */
.animate-in { opacity: 0; transform: translateY(20px); transition: opacity 0.6s ease, transform 0.6s ease; }
.animate-in.visible { opacity: 1; transform: translateY(0); }

/* ── Responsive ── */
@media (max-width: 900px) {
  .hero-inner { grid-template-columns: 1fr; gap: 40px; }
  .hero { padding: 60px 24px; }
  .section, .section-full { padding: 60px 24px; }
  .innovation-grid { grid-template-columns: 1fr; }
  .score-grid { grid-template-columns: repeat(2, 1fr); }
  .nav-links { display: none; }
  .hero-stats { grid-template-columns: 1fr 1fr; }
}
@media (max-width: 600px) {
  .hero-stats { grid-template-columns: 1fr; }
  .score-grid { grid-template-columns: 1fr 1fr; }
  .report-footer { flex-direction: column; gap: 8px; text-align: center; }
  .mechanism-flow { flex-direction: column; }
}
```

---

## 必须包含的组件结构

### 1. 固定顶部导航栏 `.top-nav`（高度 52px，毛玻璃效果）
```html
<nav class="top-nav">
  <div class="nav-logo">
    <span class="dot"></span>
    <span>报告编号 + 标题缩写</span>
  </div>
  <div class="nav-links">
    <!-- 各 section 的锚点链接 -->
  </div>
  <div class="nav-right">
    <div class="view-toggle">
      <button class="active" onclick="switchView('report')">可视化</button>
      <button onclick="switchView('md')">原文</button>
    </div>
    <span class="nav-badge">AACR 2026</span>
  </div>
</nav>
```

### 2. Markdown 原文视图（默认隐藏）
```html
<div id="md-view">
  <div class="md-container">
    <div id="md-content"><p style="color:var(--text3);text-align:center;padding:60px 0;">加载中...</p></div>
  </div>
</div>
```

### 3. Report 视图结构
```html
<div id="report-view">
  <!-- a. Hero section（id="top"，min-height:100vh，两栏布局） -->
  <section class="hero" id="top">
    <div class="hero-inner">
      <div class="hero-left">
        <h1 class="hero-title"><em>关键词</em> 标题</h1>
        <p class="hero-subtitle">副标题摘要</p>
        <div class="hero-meta">
          <span class="meta-tag"><span class="label">报告</span><span class="value">#XX</span></span>
          <!-- 更多 meta-tag ... -->
        </div>
      </div>
      <div class="hero-right">
        <div class="hero-stats">
          <!-- 4个 .stat-card -->
        </div>
      </div>
    </div>
  </section>

  <!-- b. 内容各 section，交替 .bg-white / .bg-alt -->
  <section class="section-full bg-white" id="section-id">
    <div class="section" style="padding-top:0;padding-bottom:0;">
      <div class="section-header">
        <div class="section-number">Section 0X</div>
        <h2 class="section-title">标题</h2>
        <p class="section-subtitle">副标题</p>
      </div>
      <!-- 内容体 -->
    </div>
  </section>

  <!-- Footer -->
  <footer class="report-footer">
    <div class="footer-left">报告标题 | #XX | AACR 2026</div>
    <div class="footer-right">v1.0 · CONFIDENTIAL</div>
  </footer>
</div>
```

---

## 内容体组件选择规则

根据实际内容语义选择对应组件（**禁止自行发明新的组件样式**）：

| 内容类型 | 对应组件 |
|---------|---------|
| 关键结论/风险/亮点列表 | `.judgment-card` + `.risk-list` |
| 机制流程/步骤链 | `.mechanism-flow` + `.flow-node.highlight` + `.flow-arrow` |
| 数据对比表 | `.data-table` 或 `.compare-wrapper > .compare-table` |
| 免疫/信号通路链 | `.icd-chain` + `.icd-node` + `.icd-arrow` |
| 创新点卡片 | `.innovation-grid > .innovation-card` |
| 评分矩阵 | `.score-grid > .score-item` + SVG 环形 |
| 研发时间线 | `.timeline > .timeline-item.completed/.pending` |
| 可折叠证据缺口 | `.gap-priority` + `.accord-item` |
| 标签/关键词 | `.summary-chips > .chip` |

---

## JavaScript 必须包含（完整实现）

```javascript
// 1. 视图切换
function switchView(view) {
  const reportView = document.getElementById('report-view');
  const mdView = document.getElementById('md-view');
  const buttons = document.querySelectorAll('.view-toggle button');
  if (view === 'md') {
    reportView.style.display = 'none'; mdView.style.display = 'block';
    buttons[0].classList.remove('active'); buttons[1].classList.add('active');
    loadMarkdown();
  } else {
    reportView.style.display = 'block'; mdView.style.display = 'none';
    buttons[0].classList.add('active'); buttons[1].classList.remove('active');
  }
}

// 2. 加载 Markdown（fetch 路径使用占位符 MD_FILENAME）
let mdLoaded = false;
function loadMarkdown() {
  if (mdLoaded) return;
  fetch('MD_FILENAME')
    .then(r => { if (!r.ok) throw new Error('Failed'); return r.text(); })
    .then(text => { document.getElementById('md-content').innerHTML = marked.parse(text); mdLoaded = true; })
    .catch(() => { document.getElementById('md-content').innerHTML = '<p style="color:var(--text3);text-align:center;padding:60px 0;">无法加载Markdown文件</p>'; });
}

// 3. 手风琴
function toggleAccord(head) {
  const item = head.parentElement;
  const body = item.querySelector('.accord-body');
  const isOpen = item.classList.contains('open');
  if (isOpen) { body.style.maxHeight = '0'; item.classList.remove('open'); }
  else { body.style.maxHeight = body.scrollHeight + 'px'; item.classList.add('open'); }
}

// 4. 入场动画
const observer = new IntersectionObserver((entries) => {
  entries.forEach(entry => { if (entry.isIntersecting) entry.target.classList.add('visible'); });
}, { threshold: 0.1, rootMargin: '0px 0px -40px 0px' });
document.querySelectorAll('.animate-in').forEach(el => observer.observe(el));

// 5. 导航高亮
const sections = document.querySelectorAll('section[id], .hero[id]');
const navLinks = document.querySelectorAll('.nav-links a');
function updateActiveLink() {
  let current = '';
  sections.forEach(section => { if (window.scrollY >= section.offsetTop - 100) current = section.getAttribute('id'); });
  navLinks.forEach(link => { link.classList.remove('active'); if (link.getAttribute('href') === '#' + current) link.classList.add('active'); });
}
window.addEventListener('scroll', updateActiveLink, { passive: true });
updateActiveLink();
```

---

## 输出要求（极其重要）

1. 使用 Write 工具将完整 HTML 写入用户指定的「HTML 输出路径」
2. HTML 必须是完整的 `<!DOCTYPE html>` 开头到 `</html>` 结尾
3. `<title>` 使用 markdown 第一个 # 标题的文字内容
4. `lang="zh-CN"`
5. markdown 原文视图的 fetch 路径写为：**MD_FILENAME**（脚本自动替换）
6. 所有 CSS 和 JS 内联，不引用外部本地文件（CDN 引用除外）
7. **背景色必须是浅色**（`var(--bg)` 或 `var(--white)`），**body 背景绝对不能是深色**
8. 写入完成后，只需简短回复「已生成: [文件路径]」即可
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
