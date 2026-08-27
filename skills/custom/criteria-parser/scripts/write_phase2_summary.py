#!/usr/bin/env python3
"""P2 收尾 phase2_summary.json 机械写盘。

为什么由脚本写(而不是主代理手写 20 行字段清单):
- 字段全部机械可算(路径拼接 / QC 状态合取 / 四分类计数 / ocr_results),手写只会引入
  「先写占位 stub」「字段手写错」两类故障;
- `patient_mode` 是唯一非机械字段(模式2/3 的 `ocr_route` 都是 B,无法从产物推导)——
  显式 `--patient-mode`(枚举校验)或读 `pdf_classification.json` 落盘的 `patient_mode` 键,
  两种来源都比手写整份文件安全。

字段形态与 f9231297 会话的既有产物逐字段兼容(下游 P2.5/P3 与判定域按此消费)。

用法
----
    write_phase2_summary.py --workspace <ws> [--patient-mode single_whole|single_paged|mixed_paged]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

USER_DATA = "/mnt/user-data"
PATIENT_MODES = ("single_whole", "single_paged", "mixed_paged")
REQUIRED_FILES = (
    "criteria_meta.json",
    "criteria_parsed_IN.json",
    "criteria_parsed_EX.json",
    "criteria_judge_IN.json",
    "criteria_judge_EX.json",
    "criteria_qc_IN.json",
    "criteria_qc_EX.json",
)


class SummaryBlocked(Exception):
    """输入不合法——拒绝写盘比写出错 summary 好。"""


def _load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SummaryBlocked(f"⛔ 读取失败:{path.name}:{exc}") from exc
    except json.JSONDecodeError as exc:
        raise SummaryBlocked(f"⛔ JSON 解析失败:{path.name}:{exc}") from exc
    if not isinstance(data, dict):
        raise SummaryBlocked(f"⛔ {path.name} 顶层必须是 JSON 对象")
    return data


def _qc_state(qc_in: dict, qc_ex: dict) -> tuple[bool, str]:
    """两轨合取 + 三态;检出「自标 blocked_round_limit 但 passed=true」的自相矛盾形态。"""
    for name, qc in (("IN", qc_in), ("EX", qc_ex)):
        if qc.get("criteria_qc_status") == "blocked_round_limit" and qc.get("passed") is True:
            raise SummaryBlocked(
                f"⛔ criteria_qc_{name}.json 自标 blocked_round_limit 但 passed=true —— 矛盾形态,"
                "由 QC 子代理修正报告后重跑(会话 881e7ba8 的 EX R3 同款形态)"
            )
    passed = qc_in.get("passed") is True and qc_ex.get("passed") is True
    if not passed:
        return False, "blocked_round_limit"
    has_advisories = bool(qc_in.get("residual_issues")) or bool(qc_ex.get("residual_issues"))
    return True, ("passed_with_advisories" if has_advisories else "passed")


def _criteria_counts(parsed_in: dict, parsed_ex: dict) -> dict[str, int]:
    counts = {k: 0 for k in ("入选_可从病例获取", "入选_不可从病例获取", "排除_可从病例获取", "排除_不可从病例获取")}
    for parsed in (parsed_in, parsed_ex):
        for cat, entries in (parsed.get("四分类") or {}).items():
            if cat in counts:
                counts[cat] += len(entries) if isinstance(entries, dict) else 0
    return counts


def _ocr_results(ws: Path, classification: dict) -> list[dict]:
    results: list[dict] = []
    for entry in classification.get("files") or []:
        if not isinstance(entry, dict):
            continue
        source, route = entry.get("source_name"), entry.get("ocr_route")
        if not source or route not in ("A", "B"):
            continue
        ocr_dir = ws / "ocr" / str(source)
        if route == "A":
            mds = sorted(ocr_dir.glob("*_full.md")) or sorted(ocr_dir.glob("*.md")) if ocr_dir.is_dir() else []
            if not mds:
                raise SummaryBlocked(f"⛔ 路线 A 的 {source} 在 {ocr_dir} 下找不到整份 OCR 产物(.md)")
            results.append({"source": source, "ocr_route": "A",
                            "ocr_file": f"{USER_DATA}/workspace/ocr/{source}/{mds[0].name}"})
        else:
            if not ocr_dir.is_dir():
                raise SummaryBlocked(f"⛔ 路线 B 的 {source} 缺 ocr/{source}/ 目录")
            md_count = len(list(ocr_dir.glob("*.md")))
            if md_count == 0:
                raise SummaryBlocked(f"⛔ 路线 B 的 {source} 在 ocr/{source}/ 下一个 .md 都没有")
            item = {"source": source, "ocr_route": "B",
                    "ocr_dir": f"{USER_DATA}/workspace/ocr/{source}", "ocr_md_count": md_count}
            if isinstance(entry.get("total_pages"), int):
                item["total_pages"] = entry["total_pages"]
            results.append(item)
    return results


def build_summary(ws: Path, patient_mode: str | None) -> dict:
    for name in REQUIRED_FILES:
        if not (ws / name).exists():
            raise SummaryBlocked(f"⛔ 缺 {name} —— 收尾顺序错误:先 slim×2 → assemble → 再写 summary")
    qc_in, qc_ex = _load(ws / "criteria_qc_IN.json"), _load(ws / "criteria_qc_EX.json")
    passed, status = _qc_state(qc_in, qc_ex)
    parsed_in = _load(ws / "criteria_parsed_IN.json")
    parsed_ex = _load(ws / "criteria_parsed_EX.json")
    classification = {}
    if (ws / "pdf_classification.json").exists():
        classification = _load(ws / "pdf_classification.json")
    if patient_mode is None:
        patient_mode = classification.get("patient_mode")
    if patient_mode not in PATIENT_MODES:
        raise SummaryBlocked(
            f"⛔ patient_mode 缺失或非法({patient_mode!r}):模式2/3 的 ocr_route 都是 B,无法从产物推导 —— "
            "传 --patient-mode 或先在 pdf_classification.json 落盘该键(取模式确认阶段的用户选择)"
        )
    summary = {name.removesuffix(".json"): f"{USER_DATA}/workspace/{name}" for name in REQUIRED_FILES}
    summary["criteria_parsed"] = f"{USER_DATA}/workspace/criteria_parsed.json"
    summary["criteria_qc"] = {"IN": f"{USER_DATA}/workspace/criteria_qc_IN.json",
                              "EX": f"{USER_DATA}/workspace/criteria_qc_EX.json"}
    summary["criteria_qc_passed"] = passed
    summary["criteria_qc_status"] = status
    summary["criteria_count"] = _criteria_counts(parsed_in, parsed_ex)
    summary["patient_mode"] = patient_mode
    summary["ocr_results"] = _ocr_results(ws, classification)
    return summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="机械写盘 phase2_summary.json(收尾最后一步)")
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--patient-mode", default=None, help="single_whole|single_paged|mixed_paged")
    args = ap.parse_args(argv)

    try:
        summary = build_summary(Path(args.workspace), args.patient_mode)
    except SummaryBlocked as exc:
        print(str(exc), file=sys.stderr)
        return 2
    out = Path(args.workspace) / "phase2_summary.json"
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"✅ {out} | qc_passed={summary['criteria_qc_passed']} status={summary['criteria_qc_status']} "
          f"mode={summary['patient_mode']} ocr={len(summary['ocr_results'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
