#!/usr/bin/env python3
"""判定机械闸一步式 wrapper：固定顺序跑完本轨三条（EX 四条）闸，可选先修 summary。

背景：判定/改判子代理手写闸命令犯过两类事故，都在会话 f9231297 实锤——
① 参数形态错（`check_reason_alignment --ocr` 双文档形态传错，EXIT:2 usage）；
② 顺序错（结构闸跑在 `uncertain_recheck` 落盘之前 → 闸6 把「产物未生成」当漏判）。
本 wrapper 把顺序与参数从模型自由度里整个移除：子代理只跑一条命令。

固定顺序（⛔ 不可拆开重排）：
    1. uncertain_recheck     —— 漏判反查（先落盘，结构闸闸6 依赖）
    2. check_reason_alignment—— reason 对齐（`--condition-ids` 收窄为批次口径时传入）
    3. 【仅 EX】exclusion_direction_check —— 排除项方向
    4. [--fix-summary] 重算各 document 的 summary 四计数（发现的漂移直接机械修正）
    5. check_judgment_structure —— 结构闸（--qc 启用闸7/8，--batch 为批次口径）

用法：
    python3 run_judgment_gates.py \
        --workspace /mnt/user-data/workspace \
        --patient S042002 --track IN \
        --judgments /mnt/user-data/workspace/patients/S042002/judgments_draft_S042002_IN_b1.json \
        --ocr /mnt/user-data/workspace/patients/S042002/ocr/筛选期病历/ocr_records.md \
              /mnt/user-data/workspace/patients/S042002/ocr/筛选期检查/ocr_records.md \
        --condition-ids IN-1 IN-2-1 ...      # 分批判定必给（reason_alignment 批次口径）
        --batch 1                            # 结构闸批次口径（与本批初稿后缀一致）
        --fix-summary                        # 结构闸前机械重算 summary
        [--qc /mnt/user-data/outputs/qc_report_S042002_IN.json]   # 改判场景
        [--snapshot]                         # 结构闸写改判前基线（闸8 依赖）
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent


def _derived_outputs(judgments: Path) -> tuple[str, str, str]:
    """由判定文件名派生三条闸的产物名（命名与既有模板完全一致）。"""
    stem = judgments.stem  # judgments_draft_S042002_IN_b1 → 去掉前缀
    if not stem.startswith("judgments_draft_"):
        raise SystemExit(f"⛔ --judgments 必须是 judgments_draft_* 形态：{judgments.name}")
    base = stem[len("judgments_draft_") :]
    return (
        f"uncertain_recheck_{base}.json",
        f"reason_alignment_{base}.json",
        f"exclusion_direction_check_{base}.json",
    )


def _run(argv: list[str], label: str) -> int:
    print(f"\n===== {label} =====", flush=True)
    result = subprocess.run([sys.executable, *_argv_paths(argv)])
    # 子进程 stdout 已直接继承父进程终端（不捕获），故仅返回退出码。
    return result.returncode


def _argv_paths(argv: list[str]) -> list[str]:
    """把 Path 转 str（python 的 subprocess 不接受 Path 也没关系，保持显式）。"""
    return [str(a) for a in argv]


def _call_uncertain(criteria: Path, judgments: Path, ocr: list[Path], out: Path) -> int:
    return _run(
        [str(_SCRIPTS / "uncertain_recheck.py"), "--criteria", criteria, "--judgments", judgments, "--ocr", *ocr, "--out", out],
        f"1/5 uncertain_recheck  → {out.name}",
    )


def _call_reason_alignment(criteria: Path, judgments: Path, ocr: list[Path], out: Path, patient: str, track: str, condition_ids: list[str] | None) -> int:
    argv = [
        str(_SCRIPTS / "check_reason_alignment.py"),
        "--criteria", criteria, "--judgments", judgments,
        "--ocr", *ocr,
        "--out", out,
        "--patient", patient, "--track", track,
    ]
    if condition_ids:
        argv += ["--condition-ids", *condition_ids]
    return _run(argv, f"2/5 check_reason_alignment → {out.name}")


def _call_exclusion(judgments: Path, criteria: Path, out: Path) -> int:
    return _run(
        [str(_SCRIPTS / "exclusion_direction_check.py"), "--judgments", judgments, "--criteria", criteria, "--out", out],
        f"3/5 exclusion_direction_check → {out.name}",
    )


def _fix_summaries(judgments: Path) -> None:
    """机械重算各 document 的 summary 四计数（发现漂移直接修正）。"""
    data = json.loads(judgments.read_text(encoding="utf-8"))
    documents = data.get("documents")
    if not isinstance(documents, dict):
        print("⛔ [fix-summary] judgments 顶层无 documents dict，跳过修复", file=sys.stderr)
        return
    keys = ("符合", "不符合", "存疑", "无法判断")
    fixed: list[tuple[str, dict, dict]] = []
    for doc_name, doc in documents.items():
        if not isinstance(doc, dict):
            continue
        entries = doc.get("judgments")
        if not isinstance(entries, dict):
            continue
        actual = {k: 0 for k in keys}
        for entry in entries.values():
            if isinstance(entry, dict) and entry.get("conclusion") in actual:
                actual[entry["conclusion"]] += 1
        summary = doc.get("summary") if isinstance(doc.get("summary"), dict) else {}
        declared = {k: summary.get(k) for k in keys}
        if declared != actual:
            doc["summary"] = {**summary, **actual}
            fixed.append((doc_name, declared, dict(actual)))
    if fixed:
        judgments.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        for doc_name, declared, actual in fixed:
            print(f"🔧 [fix-summary] {doc_name}: 声明 {declared} → 实际 {actual}，已覆写 summary")
    else:
        print("[fix-summary] summary 四计数与条目一致，无需修正")


def _call_structure(
    workspace: Path, patient: str, track: str, judgments: Path, batch: int | None, qc: Path | None, snapshot: bool
) -> int:
    # 一致性防错：结构闸按 patient+track[+batch] 推导文件名，必须与 --judgments 一致。
    expected = f"judgments_draft_{patient}_{track}" + (f"_b{batch}" if batch is not None else "") + ".json"
    if judgments.name != expected:
        raise SystemExit(
            f"⛔ --judgments 与结构闸口径不一致：收到 {judgments.name}，"
            f"结构闸按 --patient/--track/--batch 只会检查 {expected}。请核对参数后重跑。"
        )
    argv = [str(_SCRIPTS / "check_judgment_structure.py"), "--workspace", workspace, "--patient", patient, "--track", track]
    if batch is not None:
        argv += ["--batch", str(batch)]
    if qc is not None:
        argv += ["--qc", qc]
    if snapshot:
        argv += ["--snapshot"]
    return _run(argv, f"5/5 check_judgment_structure（口径 {expected}）")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="判定机械闸一步式 wrapper（固定顺序）")
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--patient", required=True)
    ap.add_argument("--track", required=True, choices=["IN", "EX"])
    ap.add_argument("--judgments", required=True, type=Path, help="本批初稿 judgments_draft_{id}_{TRACK}[_bN].json")
    ap.add_argument("--ocr", required=True, nargs="+", type=Path, help="该患者全部 ocr_records.md（双文档传两个）")
    ap.add_argument("--criteria", type=Path, help="默认 {workspace}/criteria_judge_{TRACK}.json")
    ap.add_argument("--condition-ids", nargs="+", default=None, help="分批判定：本批条件ID（reason_alignment 批次口径）")
    ap.add_argument("--batch", type=int, default=None, help="结构闸批次口径（与初稿 _bN 后缀一致）")
    ap.add_argument("--qc", type=Path, default=None, help="qc_report，启用结构闸闸7/8")
    ap.add_argument("--fix-summary", action="store_true", help="结构闸前机械重算 summary 四计数")
    ap.add_argument("--snapshot", action="store_true", help="结构闸写改判前基线（闸8 依赖）")
    args = ap.parse_args(argv)

    judgments: Path = args.judgments
    criteria: Path = args.criteria or Path(args.workspace) / f"criteria_judge_{args.track}.json"
    uncertain_out, reason_out, exclusion_out = [
        judgments.parent / name for name in _derived_outputs(judgments)
    ]

    exits: dict[str, int] = {}
    exits["uncertain_recheck"] = _call_uncertain(criteria, judgments, args.ocr, uncertain_out)
    exits["reason_alignment"] = _call_reason_alignment(
        criteria, judgments, args.ocr, reason_out, args.patient, args.track, args.condition_ids
    )
    if args.track == "EX":
        exits["exclusion_direction"] = _call_exclusion(judgments, criteria, exclusion_out)

    if args.fix_summary:
        print("\n===== 4/5 fix-summary =====", flush=True)
        _fix_summaries(judgments)

    exits["structure"] = _call_structure(Path(args.workspace), args.patient, args.track, judgments, args.batch, args.qc, args.snapshot)

    blocked = {name: code for name, code in exits.items() if code != 0}
    print("\n===== 汇总 =====", flush=True)
    for name, code in exits.items():
        print(f"  {name:<24} exit={code}")
    if blocked:
        print(f"⛔ 有闸未过：{', '.join(blocked)} —— 按各闸输出据实改判后重跑本 wrapper（同一条命令，不要拆开、不要改参数）。", flush=True)
        return 2
    print("✅ 本轨机械闸全部通过。", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())