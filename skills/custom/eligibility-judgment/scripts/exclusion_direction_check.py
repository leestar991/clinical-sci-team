#!/usr/bin/env python3
"""确定性「排除项结论方向」校验闸（O3）。

背景（真实故障，患者 M016_ZALO）：judgments 中 EX-10 / EX-12 / EX-15 / EX-16 四条
的 reason 分别写着"未见活动性感染""膀胱壁光滑未描述梗阻""HBsAg/HCV/HIV/梅毒
全阴性""已完成 PSMA 显像未见禁忌"——语义上都是**排除条件未被触发**，但
conclusion 却写成了 `不符合`。而技能规定排除项 `不符合` = 排除条件**被触发**
（患者应被排除），于是四条本应"可入选"的判定被编码成了四条"建议排除"。

根因：`符合/不符合` 在排除项上的语义是**反直觉**的——模型容易按口语理解成
"不符合该排除标准的描述"，方向正好反转。这类错误纯自然语言提示很难兜住，
必须叠加一道**不依赖模型注意力的机械方向校验**。

判定方法（全部确定性、可复现）：
1. 显式声明优先：reason 中若出现「未触发/不构成排除/…」或「触发排除/应排除/…」
   等方向短语，直接据此得出 reason 侧方向；
2. 无显式声明时，退化为**否定/肯定证据词计数**（带否定前窗，"未见活动性"不计
   为肯定），只有单侧证据才给出方向；
3. 可选字段 `exclusion_triggered`（true/false）与 conclusion 冲突 → 直接阻断级。

用法：
    python3 exclusion_direction_check.py \
        --judgments /mnt/user-data/workspace/patients/{id}/judgments_draft.json \
        [--criteria /mnt/user-data/workspace/criteria_parsed.json] \
        --out /mnt/user-data/workspace/patients/{id}/exclusion_direction_check.json

产物结构：
    {
      "patient_id": "M016_ZALO",
      "checked": 17,
      "conflicts": ["EX-10", "EX-12"],   # 阻断级：方向反转/字段冲突
      "advisories": ["EX-3"],            # 建议级：方向未显式声明或弱信号
      "entries": [
        {"条件ID": "EX-10", "document": "...", "conclusion": "不符合",
         "expected_conclusion": "符合", "direction_in_reason": "未触发",
         "severity": "阻断级", "issue": "direction_conflict",
         "evidence_hits": {"negative": [...], "positive": []},
         "reason": "..."}
      ]
    }

设计约束：
- 只读 judgments（+ 可选 criteria 用于识别排除项），不读 OCR、不读 uploads；
- 只做**方向一致性**校验，不改写判定、不做语义修订；
- 始终 exit 0；是否有问题以 JSON 的 `conflicts` / `advisories` 为准。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

MET = "符合"
NOT_MET = "不符合"
DIRECTIONAL_CONCLUSIONS = (MET, NOT_MET)

# 排除项条件ID 兜底识别（criteria_parsed.json 缺失时使用）
EX_ID_PATTERN = re.compile(r"^\s*(EX|EXC|排除)", re.IGNORECASE)

# --- 方向短语（显式声明，优先级最高）---------------------------------------
# 注意：必须先匹配"未触发"类，再从剩余文本里匹配"触发"类，
# 否则"未被触发"会被"被触发"误命中。
NOT_TRIGGERED_PHRASES = [
    "未触发",
    "不触发",
    "未被触发",
    "不会触发",
    "未命中",
    "未满足排除",
    "不满足排除",
    "不构成排除",
    "不构成该排除",
    "排除条件不成立",
    "不予排除",
    "无需排除",
    "不应排除",
    "不适用该排除",
    "该排除条件不适用",
    "可入选",
]
TRIGGERED_PHRASES = [
    "触发排除",
    "触发该排除",
    "已触发",
    "被触发",
    "命中排除",
    "命中该排除",
    "构成排除",
    "满足排除",
    "应排除",
    "应被排除",
    "予以排除",
    "建议排除",
]

# --- 证据词（无显式声明时的退化信号）---------------------------------------
NEGATIVE_EVIDENCE = [
    "阴性",
    "未见",
    "未发现",
    "未提示",
    "未描述",
    "未提及",
    "未探及",
    "未记录",
    "未查见",
    "未出现",
    "未合并",
    "无异常",
    "未见异常",
    "正常",
    "通畅",
    "光滑",
    "不存在",
    "无相关",
    "无禁忌",
]
# 肯定证据词：命中前 NEGATION_WINDOW 个字符内若有否定标记则不计（"未见活动性"不算肯定）
POSITIVE_EVIDENCE = [
    "阳性",
    "确诊",
    "病史明确",
    "曾接受",
    "已接受",
    "正在接受",
    "存在",
    "合并",
    "超出",
    "超过",
    "高于",
    "低于",
]
NEGATION_MARKERS = ("未", "无", "不", "非", "否", "阴")
NEGATION_WINDOW = 4


def _find_phrases(text: str, phrases: list[str]) -> list[str]:
    return [p for p in phrases if p in text]


def _find_evidence(text: str, keywords: list[str], *, skip_negated: bool) -> list[str]:
    """返回命中的证据词；skip_negated=True 时跳过被前置否定标记修饰的命中。"""
    hits: list[str] = []
    for kw in keywords:
        for m in re.finditer(re.escape(kw), text):
            if skip_negated:
                window = text[max(0, m.start() - NEGATION_WINDOW) : m.start()]
                if any(neg in window for neg in NEGATION_MARKERS):
                    continue
            hits.append(kw)
            break
    return hits


def detect_direction(reason: str) -> tuple[str | None, str, dict]:
    """从 reason 文本推断排除项方向。

    返回 (direction, basis, evidence_hits)：
    - direction: "未触发" | "触发" | None（无法确定）
    - basis: "explicit"（显式方向短语）| "evidence"（否定/肯定证据词）| "none"
    """
    text = reason or ""
    not_hits = _find_phrases(text, NOT_TRIGGERED_PHRASES)
    # 从文本中剔除"未触发"类短语后再找"触发"类，避免 未被触发 → 被触发 误判
    stripped = text
    for p in not_hits:
        stripped = stripped.replace(p, "")
    trig_hits = _find_phrases(stripped, TRIGGERED_PHRASES)

    evidence_hits = {
        "negative": _find_evidence(text, NEGATIVE_EVIDENCE, skip_negated=False),
        "positive": _find_evidence(text, POSITIVE_EVIDENCE, skip_negated=True),
        "explicit_not_triggered": not_hits,
        "explicit_triggered": trig_hits,
    }

    if not_hits and not trig_hits:
        return "未触发", "explicit", evidence_hits
    if trig_hits and not not_hits:
        return "触发", "explicit", evidence_hits
    if trig_hits and not_hits:
        # 两类短语同时出现 → 表述矛盾，交给人工/LLM 判读
        return None, "explicit", evidence_hits

    neg, pos = evidence_hits["negative"], evidence_hits["positive"]
    if neg and not pos:
        return "未触发", "evidence", evidence_hits
    if pos and not neg:
        return "触发", "evidence", evidence_hits
    return None, "none", evidence_hits


def _exclusion_ids(criteria: dict | None) -> set[str] | None:
    """从 criteria_parsed.json 取排除项条件ID 集合；取不到返回 None（走 ID 前缀兜底）。"""
    if not isinstance(criteria, dict):
        return None
    four = criteria.get("四分类")
    if not isinstance(four, dict):
        return None
    ids: set[str] = set()
    for category, items in four.items():
        if "排除" not in str(category):
            continue
        # 类目形态：dict（key=条件ID，当前形态）或 list（旧 workspace，只读兼容）。
        if isinstance(items, dict):
            entries: list = list(items.values())
        elif isinstance(items, list):
            entries = items
        else:
            continue
        for item in entries:
            if isinstance(item, dict) and item.get("条件ID"):
                ids.add(str(item["条件ID"]))
    return ids or None


def _is_exclusion(cid: str, ex_ids: set[str] | None) -> bool:
    if ex_ids is not None:
        return cid in ex_ids
    return bool(EX_ID_PATTERN.match(cid))


def check(judgments: dict, criteria: dict | None = None) -> dict:
    ex_ids = _exclusion_ids(criteria)
    entries: list[dict] = []
    conflicts: list[str] = []
    advisories: list[str] = []

    documents = judgments.get("documents") or {}
    if not isinstance(documents, dict):
        documents = {}
    top = judgments.get("judgments")
    if isinstance(top, dict) and top:
        # 统一判定产物：顶层 judgments（doc_key 空 = 无物料限定），包回 {judgments: ...} 形态统一迭代
        documents = {"": {"judgments": top}}
    for doc_key, doc in documents.items():
        if not isinstance(doc, dict):
            continue
        for cid, jdg in (doc.get("judgments") or {}).items():
            cid = str(cid)
            if not isinstance(jdg, dict) or not _is_exclusion(cid, ex_ids):
                continue
            conclusion = jdg.get("conclusion")
            if conclusion not in DIRECTIONAL_CONCLUSIONS:
                continue  # 存疑/无法判断 无方向可校验

            reason = str(jdg.get("reason") or "")
            direction, basis, evidence_hits = detect_direction(reason)
            expected = MET if direction == "未触发" else NOT_MET if direction == "触发" else None
            declared = jdg.get("exclusion_triggered")

            issue = None
            severity = None
            # 1) 可选字段与 conclusion 冲突 → 阻断级
            if isinstance(declared, bool):
                field_expected = NOT_MET if declared else MET
                if field_expected != conclusion:
                    issue, severity = "field_conflict", "阻断级"
            # 2) reason 方向与 conclusion 冲突
            if issue is None and expected is not None and expected != conclusion:
                if basis == "explicit":
                    issue, severity = "direction_conflict", "阻断级"
                elif conclusion == NOT_MET:
                    # 判"被触发"却只有否定证据 → M016 型反转，阻断级
                    issue, severity = "suspected_inversion", "阻断级"
                else:
                    # 判"未触发"却只有肯定证据 → 弱信号，建议级人工确认
                    issue, severity = "suspected_inversion_weak", "建议级"
            # 3) 方向未显式声明（违反技能原则九 B）→ 建议级
            if issue is None and basis != "explicit":
                issue, severity = "direction_undeclared", "建议级"

            entry = {
                "条件ID": cid,
                "document": doc_key,
                "conclusion": conclusion,
                "direction_in_reason": direction,
                "direction_basis": basis,
                "expected_conclusion": expected,
                "exclusion_triggered": declared if isinstance(declared, bool) else None,
                "evidence_hits": evidence_hits,
                "reason": reason,
            }
            if issue:
                entry["issue"] = issue
                entry["severity"] = severity
                if severity == "阻断级":
                    conflicts.append(cid)
                else:
                    advisories.append(cid)
            else:
                entry["issue"] = "ok"
                entry["severity"] = None
            entries.append(entry)

    return {
        "patient_id": judgments.get("patient_id"),
        "checked": len(entries),
        "conflicts": conflicts,
        "advisories": advisories,
        "entries": entries,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="确定性排除项结论方向校验闸（O3）")
    ap.add_argument("--judgments", required=True, help="judgments_draft.json 路径")
    ap.add_argument("--criteria", help="criteria_parsed.json 路径（可选，用于识别排除项）")
    ap.add_argument("--out", required=True, help="输出 exclusion_direction_check.json 路径")
    args = ap.parse_args(argv)

    judgments = json.loads(Path(args.judgments).read_text(encoding="utf-8"))
    criteria = None
    if args.criteria and Path(args.criteria).exists():
        criteria = json.loads(Path(args.criteria).read_text(encoding="utf-8"))

    result = check(judgments, criteria)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    if result["conflicts"]:
        print(
            "⚠️ 排除项结论方向冲突（阻断级）："
            f"{result['conflicts']} —— 排除项「不符合」=被触发/应排除、「符合」=未触发/可入选；"
            "上述条目 reason 与 conclusion 方向相反，必须按语义改判后重写 judgments_draft.json。"
        )
    if result["advisories"]:
        print(f"ℹ️ 建议级（方向未显式声明或弱信号，需在 reason 中补「触发/未触发」）：{result['advisories']}")
    if not result["conflicts"] and not result["advisories"]:
        print(f"✅ 方向校验通过：{result['checked']} 条排除项结论与理由方向一致。")
    print(f"产物已写入：{out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
