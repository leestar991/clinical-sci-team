#!/usr/bin/env python3
"""为已聚合的 `ocr_records.md` 建一份页码 → 行区间索引（`ocr_page_index.json`）。

为什么需要这一步
----------------
聚合脚本按页拼接 `ocr_records.md` 时**知道每一页从第几行开始**，但这个信息落盘即丢。
判定子代理拿到的只是一个几千行的 `.md`，于是它必须自己一轮轮 `grep` 把页边界摸出来。

会话 `09eeaffb` 实测（IN 轨判定子代理，99 个 AI 回合）：

* `筛选期检查/ocr_records.md`（7,604 行）被 `read_file` **34 次**、`筛选期病历` 14 次；
* `read_file` 共 67 次，unique path 只有 12 个；整份重读 23 次；
* 请求区间 15,068 行里 10,326 行是重复（overlap 69%）；
* 该子代理在第 143 回合才靠 grep 拼出页表（"page_001: lines 1-377 / page_003: 739-1151…"），
  而这张表在聚合时就是已知的。

这不只是浪费字节：每次读进来的正文会被**后续每一轮重新继承**，把 `tokens_before` 推过
压缩触发线，于是任务内反复压缩（该会话 26–28 次/任务），最终撞 `recursion_limit`。

设计约束
--------
- **只读、不改 `ocr_records.md`**：页块起始行的写入方是 `parse_image_batch` /
  `collect_text_pages.py`，本脚本只**解析**那一行，绝不补写或改写
  （契约见 `tests/skills/test_ocr_provenance_contract.py`）。
- **页码以文件名 `_page_{NNN}` 为准**，不信正文里的 `第 N 页`（可能缺失/错位）——
  与 `references/aggregate-ocr.md`「页块结构」同一口径。
- **无页块不是错误**：路线 A 的整份解析产物（`{source}_full.md`）本就没有页块，
  此时该来源记 `pages: []` + `has_page_blocks: false`，判定侧据此知道"这份没有页码可引"。
- **幂等**：纯函数式重算，重跑覆盖，不累积。

用法
----
    # 单患者/多患者，扫 workspace/patients/*/ocr/*/ocr_records.md
    python3 ocr_page_index.py --workspace /mnt/user-data/workspace
    # 只建某一位患者
    python3 ocr_page_index.py --workspace /mnt/user-data/workspace --patient P001

产物：`workspace/patients/{id}/ocr_page_index.json`

`exit 2` 表示有 `ocr_records.md` 存在却解析不出任何页块（写入方漏写来源标注，
即 thread `1fee1395` 那类静默丢可追溯性的故障）；其余情况 `exit 0`。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# 页块起始行的唯一契约（与 tests/skills/test_ocr_provenance_contract.py 同源）：
# `（来源图片：{虚拟路径}）`，路径后可带 ` 文本层…` 等后缀说明。
PROVENANCE_RE = re.compile(r"^（来源图片：(?P<path>[^）\s]+)(?P<suffix>[^）]*)）\s*$")
# 页码只认文件名里的 `_page_{NNN}`；正文的「第 N 页」可能缺失或错位，不作为来源。
PAGE_NO_RE = re.compile(r"_page_(\d+)\b")


def parse_page_blocks(text: str) -> list[dict]:
    """把 `ocr_records.md` 正文切成页块记录。

    返回 `[{page, image, start_line, end_line, lines}]`，行号 **1-based、闭区间**，
    与 `read_file` 的行区间参数同一口径（子代理直接照抄即可，不需要自己 ±1）。

    `end_line` 取下一个页块起始行的前一行；末页取文件最后一行。页块之间的空行归属
    上一页——判定要的是"这一页的正文在哪几行"，把分隔空行算进去不影响定位，而少算
    会让区间读丢掉紧贴边界的内容。
    """
    lines = text.splitlines()
    starts: list[tuple[int, str, int | None]] = []
    for idx, line in enumerate(lines, start=1):
        m = PROVENANCE_RE.match(line.strip())
        if not m:
            continue
        image = m.group("path")
        pm = PAGE_NO_RE.search(Path(image).name)
        starts.append((idx, image, int(pm.group(1)) if pm else None))

    blocks: list[dict] = []
    for i, (start, image, page) in enumerate(starts):
        end = starts[i + 1][0] - 1 if i + 1 < len(starts) else len(lines)
        blocks.append(
            {
                "page": page,
                "image": image,
                "start_line": start,
                "end_line": end,
                "lines": end - start + 1,
            }
        )
    return blocks


VIRTUAL_USER_DATA_ROOT = "/mnt/user-data"


def virtual_user_data_path(path: Path, workspace: Path | None = None) -> str:
    """把 *path* 换算成 `/mnt/user-data/...` 形态的虚拟路径。

    沙箱执行命令前会把命令行里的 `/mnt/user-data/...` 重写成宿主机真实路径（否则脚本
    读不到文件），但写进 `.json` 的 `file`/`index_file` 是**数据**，判定子代理要在容器
    语境里回读它们--宿主机绝对路径换部署即失效（会话 `156a476e` 的泄漏形态）。与
    `collect_text_pages.py` 的同名函数同口径。

    给了 *workspace* 就按相对位置重拼（最可靠）；没给就按路径里的 `user-data` 段定位
    （宿主机形态 `.../user-data/workspace/...`）；两者都不适用时原样返回，不硬造。
    """
    p = str(path)
    if p.startswith(VIRTUAL_USER_DATA_ROOT):
        return p
    resolved = Path(p).resolve()
    if workspace is not None:
        try:
            rel = resolved.relative_to(Path(workspace).resolve())
            return f"{VIRTUAL_USER_DATA_ROOT}/workspace/{rel.as_posix()}"
        except ValueError:
            pass
    parts = resolved.parts
    if "user-data" in parts:
        i = parts.index("user-data")
        return VIRTUAL_USER_DATA_ROOT + "/".join(("", *parts[i + 1 :]))
    return p


def index_source(records: Path, workspace: Path | None = None) -> dict:
    """为一份 `ocr_records.md`（或 `{source}_full.md`）建索引条目。"""
    text = records.read_text(encoding="utf-8")
    total = len(text.splitlines())
    blocks = parse_page_blocks(text)
    return {
        "file": virtual_user_data_path(records, workspace),
        "total_lines": total,
        "has_page_blocks": bool(blocks),
        "page_count": len(blocks),
        "pages": blocks,
    }


def build_patient_index(patient_dir: Path, workspace: Path) -> dict:
    """扫 `patients/{id}/ocr/{source}/ocr_records.md`，每个 source 一条索引。"""
    sources: dict[str, dict] = {}
    ocr_root = patient_dir / "ocr"
    if ocr_root.is_dir():
        for source_dir in sorted(p for p in ocr_root.iterdir() if p.is_dir()):
            records = source_dir / "ocr_records.md"
            if records.exists():
                sources[source_dir.name] = index_source(records, workspace)
    return {"patient_id": patient_dir.name, "sources": sources}


def _write(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def build(workspace: Path, only: str | None = None) -> dict:
    patients_root = workspace / "patients"
    results: list[dict] = []
    problems: list[str] = []

    if not patients_root.is_dir():
        return {"patients": [], "problems": [f"⛔ 无 {patients_root} —— 先跑按患者聚合（references/aggregate-ocr.md）"]}

    for patient_dir in sorted(p for p in patients_root.iterdir() if p.is_dir()):
        if only and patient_dir.name != only:
            continue
        index = build_patient_index(patient_dir, workspace)
        if not index["sources"]:
            continue
        for source, entry in index["sources"].items():
            if not entry["has_page_blocks"]:
                problems.append(
                    f"⛔ {patient_dir.name}/{source}：{entry['total_lines']} 行里解析不出任何页块 —— "
                    f"页块起始行 `（来源图片：…）` 缺失（写入方是 parse_image_batch / collect_text_pages.py，"
                    f"聚合只做原样拼接）。判定产物的 page / screenshot_ref 会整体为空，"
                    f"故障档案 thread `1fee1395`。整份解析产物（路线 A）无页块属正常，不必建本患者索引。"
                )
        out = _write(patient_dir / "ocr_page_index.json", index)
        index["index_file"] = virtual_user_data_path(out, workspace)
        results.append(index)

    return {"patients": results, "problems": problems}


def summarize(result: dict) -> str:
    lines: list[str] = []
    for index in result["patients"]:
        pid = index["patient_id"]
        lines.append(f"[{pid}] → {index.get('index_file')}")
        for source, entry in index["sources"].items():
            lines.append(f"[{pid}] {source}: {entry['page_count']} 页 / {entry['total_lines']} 行")
            for blk in entry["pages"]:
                page = f"page_{blk['page']:03d}" if blk["page"] else "page_?"
                lines.append(f"[{pid}]   {page}: {blk['start_line']}-{blk['end_line']} ({blk['lines']} 行)")
    for prob in result["problems"]:
        lines.append(prob)
    if not result["patients"] and not result["problems"]:
        lines.append("未找到任何 ocr_records.md（整份解析路线无需本索引）")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="为聚合后的 ocr_records.md 建页码→行区间索引")
    ap.add_argument("--workspace", required=True, help="workspace 目录")
    ap.add_argument("--patient", help="只处理该患者ID（默认全部）")
    ap.add_argument("--json", help="可选：把完整结果另写到该路径")
    args = ap.parse_args(argv)

    result = build(Path(args.workspace), args.patient)
    print(summarize(result))
    if args.json:
        out = _write(Path(args.json), result)
        print(f"结果已写入：{out}")
    return 2 if result["problems"] else 0


if __name__ == "__main__":
    sys.exit(main())
