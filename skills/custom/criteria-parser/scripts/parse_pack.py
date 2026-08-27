#!/usr/bin/env python3
"""解析收尾工具：切判定输入包(slim) + 合成全量包(assemble)。

2026-08-27 自 `eligibility-judgment/scripts/judge_pack.py` 拆入解析域：slim/assemble
吃的是解析产物(criteria_parsed_{IN|EX}/criteria_qc_{IN|EX}/criteria_meta)、闸全是解析侧
闸、产出是解析阶段的交付物(判定输入包 criteria_judge_{IN|EX}.json 与全量包
criteria_parsed.json)——属于解析流程收尾,脚本随域走。判定域的批次切分与分片合并
(plan-batches/merge-judgments/merge-recheck)仍在 `/eligibility-judgment/scripts/judge_pack.py`。

子命令:

1. `slim` —— 把**单轨** criteria 精简成该轨判定输入包 `criteria_judge_{IN|EX}.json`:
   - 只保留 `四分类` 中本轨的类目,剔除 `方案元数据` / `解析说明` / `汇总统计` / `描述索引`;
   - 每条只保留判定必需字段(条件ID/来源标准/原文/子条件/逻辑关系/可从病例获取/转化条件/
     日期维度/非空备注,含 `或组`/`或组语义`);
   - **保留 `四分类` 外层结构**,使判定域闸脚本(`uncertain_recheck.py` 等)可直接吃该包;
   - 每个类目落盘为**以 `条件ID` 为键的 dict**,与 `criteria_parsed_*.json` 同形
     (输入是旧数组形态也照样接受——只读兼容,产出一律 dict)。

2. `assemble` —— 把 IN/EX 两轨合成全量 `criteria_parsed.json`(报告与全量终检的输入):
   注入 `方案元数据`(来自 `--meta`,全篇级,两轨都不产出)与 `解析说明`(常量样板),
   `四分类` 按类目取自对应轨,**重算** `汇总统计`,`描述索引` 取两轨并集。

3. `split` —— 已退役(单轨切分不再存在),调用时打印迁移提示并 `exit 2`。

`slim` / `assemble` 都是**带闸门**的,任一不过即 `exit 2` 且不产出任何文件:

| 闸门 | 拦什么 | 可否绕过 |
|------|--------|----------|
| QC 闸 | `passed != true` / 未修复 `blocking_issues` 非空 / 检出「带建议放行·轮次上限」自我放行痕迹 / coverage 漏看 | 仅 `--force-qc-unconverged`(人工知情放行) |
| 单轨结构闸 | 本轨缺本轨类目、含对侧类目条目、条件ID 前缀与轨不符 | 不可绕过 |
| 跨轨闸(assemble) | 条件ID 跨轨重复、`描述索引` 键跨轨冲突 | 不可绕过 |
| 全量结构闸(assemble) | 四类目不齐、前缀↔类目不一致、`汇总统计` 与实际条目数不一致 | 不可绕过 |
| 产出闸 | 分片条件数为 0 | 不可绕过 |
| 元数据闸(assemble) | `方案元数据` 为空(报告无法追溯来源) | 不可绕过 |

用法:
    python3 parse_pack.py slim --criteria .../criteria_parsed_IN.json \
                               --qc .../criteria_qc_IN.json --track IN \
                               --out .../criteria_judge_IN.json
    python3 parse_pack.py assemble --in-criteria .../criteria_parsed_IN.json --in-qc .../criteria_qc_IN.json \
                                   --ex-criteria .../criteria_parsed_EX.json --ex-qc .../criteria_qc_EX.json \
                                   --meta .../criteria_meta.json --out .../criteria_parsed.json

`_sort_key`(条件ID 自然序)与 `SplitBlocked` 在判定域 `judge_pack.py` 各有一份内联副本
(注释互指);算法改动须两边同步。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# 分片定义：分片名 → 该分片包含的 `四分类` 类目关键词
SHARDS: dict[str, str] = {"IN": "入选", "EX": "排除"}
TRACKS = tuple(SHARDS)  # ("IN", "EX")


def category_entries(items: object) -> list[dict]:
    """把一个 `四分类` 类目容器归一成条目列表。

    * **dict（当前形态）** —— key 是 `条件ID`，`apply_json_patches` 按身份定位的前提。
    * **list（旧 workspace，只读兼容）** —— 尚未跑迁移脚本的历史数据仍能被切分。

    ⛔ 本文件此前每个消费点都是 `if not isinstance(items, list): continue`（切分）或
    `list(items) if isinstance(items, list) else []`（合成）。后者尤其危险：dict 进来会
    产出**空数组**，于是 `criteria_judge_{TRACK}.json` 的整轨条件静默清零，而 `judge_pack`
    既是消费者也是生产者，形态会一路传导到 `uncertain_recheck` / `exclusion_direction_check`
    / `evidence_bundle` / `check_reason_alignment` / `rollup`。容器形态是否合法由
    `check_track_structure.py` 闸13 阻断，这里只做归一。
    """
    if isinstance(items, dict):
        return [it for it in items.values() if isinstance(it, dict)]
    if isinstance(items, list):
        return [it for it in items if isinstance(it, dict)]
    return []


def _keyed_by_condition_id(entries: list[dict]) -> dict[str, dict]:
    """把条目列表落成以 `条件ID` 为键的 dict —— 本脚本所有产物的落盘形态。

    顺序按 `条件ID` 自然序（`_sort_key`），因为 dict 的书写顺序不承载语义：
    JSON 对象在格式层面无序，往里加条目也只会落到末尾。展示顺序由消费方排序决定。
    """
    keyed: dict[str, dict] = {}
    for item in entries:
        cid = str(item.get("条件ID") or "").strip()
        if not cid:
            continue
        keyed[cid] = item
    return {cid: keyed[cid] for cid in sorted(keyed, key=_sort_key)}

# 每条条件保留的判定必需字段（顺序即输出顺序）
KEEP_FIELDS = (
    "条件ID",
    "来源标准",
    "原文",
    "子条件",
    "逻辑关系",
    "可从病例获取",
    # OR 分支拆成并行原子子条件后，同组关系与汇总方向靠这两个键传递。漏掉它们，
    # 判定子代理就把同组分支当成彼此独立的条件：IN 轨会按约束 18「全部入选'符合'」
    # 把"满足其一即可"读成"必须全部满足"，患者被错误淘汰。
    "或组",
    "或组语义",
    "转化条件",
    "日期维度",
    "适用范围",
    "备注",
)

CONCLUSIONS = ("符合", "不符合", "存疑", "无法判断")

# `四分类` 期望的四个类目键（缺失即视为结构错误）
EXPECTED_CATEGORIES = (
    "入选_可从病例获取",
    "入选_不可从病例获取",
    "排除_可从病例获取",
    "排除_不可从病例获取",
)

# 条件ID 前缀 → 该条目必须落在含此关键词的类目内
PREFIX_POLARITY = {"IN": "入选", "EX": "排除"}

# `assemble` 合成全量包时使用的 `解析说明` 兜底文案（与
# criteria-parser/references/schema_example.json 一致）。双轨解析时该块是常量样板，
# 两轨都不该各写一份，故由本脚本注入；`--meta` 里若带 `解析说明` 则以它为准。
DEFAULT_PARSE_NOTES: dict = {
    "四分类": list(EXPECTED_CATEGORIES),
    "拆分原则": "最小子颗粒度；AND关系拆分为独立子条件；OR/和或/任一的**异质替代分支**拆分为并行原子子条件并以`或组`+`或组语义`标记同组（入选=任一满足即整条满足，排除=任一触发即整条触发）；同一事实的等价表述（单位换算/参考范围变体/同类列举）与除非例外的豁免逻辑仍整体保留，不拆分",
    "日期处理": "参考日期照标准原文标出参考事件（知情同意书签署/筛选/首次给药/检测…，⛔ 不得统一压成筛选），可从病例提取则提取，取不到才默认判定当天；日期判断新增子维度：事件、发生日期、参考事件、参考日期、时间窗",
    "条件转化": "可从病例获取的条件转化为病例可匹配的结构化条件(匹配字段/运算符/阈值)，同时保留原标准原文用于展示",
    "可获取性判定": "客观诊断/分期/检验/影像/用药史/手术史=可从病例获取；知情同意、受试者承诺、预后预估、未来计划、研究者主观评估=不可从病例获取",
}


class SplitBlocked(Exception):
    """切分闸门未通过 —— 必须先修复上游产物，不允许带病推进判定阶段。"""


# 「达轮次上限带建议放行」在 criteria QC 上已是非法状态（达上限仍有阻断必须暂停等人工裁决），
# 出现这些标记说明阻断项被降级为建议后自我放行，未经 QC 子代理复核。
_SELF_RELEASE_MARKERS = ("带建议放行", "轮次上限", "round_limit")


def _self_release_evidence(qc: dict) -> list[str]:
    """找出「阻断降级为建议后自我放行」的痕迹。"""
    hits: list[str] = []
    if qc.get("round_limit_released"):
        hits.append("round_limit_released=true")
    for field in ("note", "status", "conclusion"):
        text = str(qc.get(field) or "")
        if any(m in text for m in _SELF_RELEASE_MARKERS):
            hits.append(f"{field}: {text[:80]}")
    for item in qc.get("residual_issues") or []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("status") or "")
        if any(m in text for m in _SELF_RELEASE_MARKERS):
            hits.append(f"residual_issues[{item.get('id') or item.get('condition_id') or '?'}].status: {text[:80]}")
    return hits


def _open_blocking(qc: dict) -> list[dict]:
    """尚未修复的阻断项。

    QC 改为**全量报告**后（会话 `c80c47d9` 的不收敛治理），已修的问题带 `status: "fixed"`
    **留在** `blocking_issues` 里以便追溯，不再从报告中消失。所以「非空即未收敛」不再成立，
    判据改为「有没有 `status` 不是 `fixed`/`upstream` 的条目」。

    ⚠️ 缺 `status` 一律按 `open` 处理：旧报告没有这个字段，默认放行会让历史故障态
    （`blocking_issues` 非空 + `passed` 被改成 true）重新溜过闸。
    """
    out: list[dict] = []
    for item in qc.get("blocking_issues") or []:
        if not isinstance(item, dict):
            out.append({"id": "?"})
            continue
        if str(item.get("status") or "open").strip().lower() not in ("fixed", "upstream"):
            out.append(item)
    return out


def _coverage_shortfall(qc: dict) -> str | None:
    """本轮是否漏看了条目。返回描述串，None 表示无问题。

    `coverage.reviewed < coverage.total_entities` 说明这一轮没有逐条看全，它报告的
    「没有 open 项」不可信 —— 这正是 `c80c47d9` 分轮发现新问题的形态（两轮 blocking_ids
    零交集）。`coverage` 缺失时不判问题：旧 workspace 没有这个字段，否则会全部卡死。
    """
    cov = qc.get("coverage")
    if not isinstance(cov, dict):
        return None
    total, reviewed = cov.get("total_entities"), cov.get("reviewed")
    if not isinstance(total, int) or not isinstance(reviewed, int):
        return None
    if reviewed < total:
        return f"本轮只复核了 {reviewed}/{total} 条（coverage.reviewed < total_entities）"
    return None


def _qc_passed(qc: dict) -> bool:
    """criteria QC 是否真正收敛。

    `passed`、未修复的 `blocking_issues`、本轮覆盖率都要看，并排除自我放行痕迹：
    历史故障是主代理把 `blocking_issues` 整批挪进 `residual_issues`（标注
    「已达QC轮次上限，带建议放行」）、清空 `blocking_issues`、再把 `passed` 改成 true，
    且不跑 QC 子代理复核。
    """
    if qc.get("passed") is not True:
        return False
    if _open_blocking(qc):
        return False
    if _coverage_shortfall(qc):
        return False
    return not _self_release_evidence(qc)


def check_qc_gate(qc: dict, *, track: str | None = None, blocked_action: str = "切分判定输入包") -> None:
    """QC 闸：criteria QC 未收敛时禁止推进（切分或合成）。"""
    if _qc_passed(qc):
        return

    # 只报**未修复**的阻断项：全量报告里 `status=fixed` 的条目留在 blocking_issues 供追溯，
    # 把它们一起报出来会让 agent 去改已经改好的条目。
    blocking = _open_blocking(qc)
    rnd = qc.get("round")
    ids = [str(b.get("condition_id") or b.get("id") or "?") for b in blocking if isinstance(b, dict)]
    detail = f"，残留阻断项 {len(blocking)} 条：{', '.join(ids)}" if ids else ""
    shortfall = _coverage_shortfall(qc)
    if shortfall:
        detail += f"，{shortfall} → 本轮结论不可信，需重跑全量复核"
    released = _self_release_evidence(qc)
    if released:
        detail += "，检出阻断降级自我放行痕迹：" + "; ".join(released)
    who = f"{track} 轨 " if track else ""
    criteria_file = f"criteria_parsed_{track}.json" if track else "criteria_parsed.json"
    qc_file = f"criteria_qc_{track}.json" if track else "criteria_qc.json"
    raise SplitBlocked(
        f"{who}criteria QC 未收敛（passed={qc.get('passed')!r}, round={rnd}{detail}），禁止{blocked_action}。\n"
        f"→ 先修复 {criteria_file} 的阻断项，再由 `task(quality-control)` 复核改写 {qc_file}；主代理不得自行改写 QC 结论。\n"
        "→ 若已达 QC 轮次上限仍有阻断项，必须暂停整个流程、向用户报告两轨残留阻断项并等待裁决，不得自行放行。\n"
        "→ 人工确认带病放行时，显式加 --force-qc-unconverged 重跑本命令。"
    )


def check_criteria_structure(criteria: dict) -> None:
    """结构闸：`四分类` 类目完整、条件ID 前缀与类目极性一致、汇总统计不失真。

    历史故障：EX-1..EX-16 被写进「入选_不可从病例获取」数组，导致
    「排除_可从病例获取」为空，切分出 IN 46 条 / EX 1 条的残缺判定包。
    这类错误是确定性的，必须在切分前机械拦住。
    """
    four = criteria.get("四分类")
    if not isinstance(four, dict):
        raise SplitBlocked("criteria_parsed.json 缺少 `四分类`，无法切分")

    problems: list[str] = []

    missing = [c for c in EXPECTED_CATEGORIES if c not in four]
    if missing:
        problems.append(f"`四分类` 缺少类目：{', '.join(missing)}")

    for category, items in four.items():
        if not isinstance(items, (dict, list)):
            problems.append(f"类目 `{category}` 既不是对象也不是数组（{type(items).__name__}）—— 应是以 `条件ID` 为键的对象")
            continue
        for item in category_entries(items):
            cid = str(item.get("条件ID") or "")
            m = re.match(r"^([A-Za-z]+)", cid)
            keyword = PREFIX_POLARITY.get(m.group(1).upper()) if m else None
            if keyword and keyword not in str(category):
                problems.append(f"条件 `{cid}` 被放在类目 `{category}`，应属「{keyword}」类目（分类结构错误）")

    stats = criteria.get("汇总统计")
    if isinstance(stats, dict):
        for category in EXPECTED_CATEGORIES:
            if category not in stats or not isinstance(four.get(category), (dict, list)):
                continue
            declared, actual = stats[category], len(four[category])
            if isinstance(declared, int) and declared != actual:
                problems.append(f"汇总统计 `{category}`={declared} 与实际条目数 {actual} 不一致（统计失真）")

    if problems:
        raise SplitBlocked("合成后的 criteria_parsed.json 结构校验未通过，禁止推进：\n" + "\n".join(f"  - {p}" for p in problems))


def check_shards_non_empty(packs: dict[str, dict]) -> None:
    """产出闸：任一分片为空说明上游结构仍有问题，宁可阻断也不交残缺判定包。"""
    empty = [shard for shard, pack in packs.items() if not pack.get("条件数")]
    if empty:
        raise SplitBlocked(f"分片 {', '.join(empty)} 条件数为 0，判定包残缺，禁止推进判定阶段；请先修复 criteria_parsed.json 的四分类归属")


def _condition_ids(four: dict) -> list[tuple[str, str]]:
    """返回 [(条件ID, 所在类目)]。"""
    out: list[tuple[str, str]] = []
    for category, items in four.items():
        for item in category_entries(items):
            out.append((str(item.get("条件ID") or ""), str(category)))
    return out


def check_track_structure(criteria: dict, track: str) -> None:
    """单轨结构闸：本轨文件只允许本轨的类目与本轨前缀的条目。

    双轨解析下每轨各写一份 `criteria_parsed_{IN|EX}.json`。若解析子代理越界把
    对侧条目写进来（历史故障 thread `5a1c8d95` 的同类错误：EX-* 被写进「入选_不可
    从病例获取」），必须在切分前机械拦住，否则两轨会重复/漏掉同一批条件。
    """
    keyword = SHARDS.get(track)
    if keyword is None:
        raise SplitBlocked(f"未知轨道 {track!r}，合法值：{', '.join(TRACKS)}")

    four = criteria.get("四分类")
    if not isinstance(four, dict):
        raise SplitBlocked(f"{track} 轨文件缺少 `四分类`，无法切分")

    problems: list[str] = []

    own_categories = [c for c in EXPECTED_CATEGORIES if keyword in c]
    missing = [c for c in own_categories if c not in four]
    if missing:
        problems.append(f"{track} 轨缺少本轨类目：{', '.join(missing)}")

    for category, items in four.items():
        if keyword in str(category):
            continue
        foreign = category_entries(items)
        if foreign:
            problems.append(f"{track} 轨文件含对侧类目 `{category}`（{len(foreign)} 条）——该轨只应写「{keyword}」类目")

    expected_prefix = next(p for p, kw in PREFIX_POLARITY.items() if kw == keyword)
    for cid, category in _condition_ids(four):
        if keyword not in category:
            continue
        m = re.match(r"^([A-Za-z]+)", cid)
        prefix = m.group(1).upper() if m else ""
        if prefix and prefix != expected_prefix:
            problems.append(f"{track} 轨的条件 `{cid}` 前缀应为 {expected_prefix}-（分类结构错误）")

    if problems:
        raise SplitBlocked(f"{track} 轨 criteria 结构校验未通过，禁止推进：\n" + "\n".join(f"  - {p}" for p in problems))


def slim_track(criteria: dict, track: str) -> dict:
    """把单轨 criteria 精简成判定输入包（保留 `四分类` 外层结构）。"""
    keyword = SHARDS[track]
    four = criteria.get("四分类") or {}
    categories: dict[str, dict] = {}
    for category, items in four.items():
        if keyword not in str(category):
            continue
        categories[category] = _keyed_by_condition_id([_slim_item(i) for i in category_entries(items)])
    return {
        "分片": keyword,
        "条件数": sum(len(v) for v in categories.values()),
        "四分类": categories,
    }


def check_cross_track(tracks: dict[str, dict]) -> None:
    """跨轨一致性闸（assemble 专用）。

    两轨独立 QC 后没人看「跨轨」问题，这里用确定性检查兜底：
    同一条件ID 不得在两轨都出现；`描述索引` 的键不得跨轨冲突。
    """
    problems: list[str] = []

    ids_by_track: dict[str, set[str]] = {}
    for track, criteria in tracks.items():
        four = criteria.get("四分类") or {}
        ids_by_track[track] = {cid for cid, _ in _condition_ids(four) if cid}

    duplicated = sorted(ids_by_track.get("IN", set()) & ids_by_track.get("EX", set()))
    if duplicated:
        problems.append(f"条件ID 跨轨重复（同一条被两轨都解析了）：{', '.join(duplicated)}")

    index_by_track = {track: (criteria.get("描述索引") or {}) for track, criteria in tracks.items()}
    index_dup = sorted(set(index_by_track.get("IN", {})) & set(index_by_track.get("EX", {})))
    if index_dup:
        problems.append(f"描述索引 键跨轨冲突：{', '.join(index_dup)}")

    if problems:
        raise SplitBlocked("跨轨一致性校验未通过，禁止合成全量包：\n" + "\n".join(f"  - {p}" for p in problems))


def assemble_tracks(tracks: dict[str, dict], meta: dict) -> dict:
    """把两轨 criteria 合成全量 `criteria_parsed.json`。

    - `方案元数据` 全篇级，由 `--meta` 注入（两轨都不产出，避免互相冲突；
      历史缺陷 thread `5a1c8d95`：该块全空且无人认领）。
    - `解析说明` 为常量样板，`--meta` 未给则用 DEFAULT_PARSE_NOTES。
    - `四分类` 按类目取自对应轨，顺序固定为 EXPECTED_CATEGORIES。
    - `汇总统计` **重算**（不信任任何一轨自报的数字）。
    - `描述索引` 取两轨并集（冲突已由 check_cross_track 拦住），按条件ID 自然排序。
    """
    protocol_meta = meta.get("方案元数据") if isinstance(meta.get("方案元数据"), dict) else meta
    if not isinstance(protocol_meta, dict) or not any(str(v).strip() for v in protocol_meta.values() if v is not None):
        raise SplitBlocked("`方案元数据` 为空：全量包缺少方案编号/标题等信息，报告无法追溯来源。请在提取阶段落盘 criteria_meta.json 后重试")

    four: dict[str, dict] = {}
    for category in EXPECTED_CATEGORIES:
        keyword = "入选" if "入选" in category else "排除"
        track = next(t for t, kw in SHARDS.items() if kw == keyword)
        four[category] = _keyed_by_condition_id(category_entries((tracks[track].get("四分类") or {}).get(category)))

    merged_index: dict[str, str] = {}
    for track in TRACKS:
        for key, value in (tracks[track].get("描述索引") or {}).items():
            merged_index[str(key)] = value

    stats = {category: len(items) for category, items in four.items()}
    stats["子条件总数"] = sum(stats.values())

    return {
        "方案元数据": protocol_meta,
        "解析说明": meta.get("解析说明") if isinstance(meta.get("解析说明"), dict) else DEFAULT_PARSE_NOTES,
        "四分类": four,
        "汇总统计": stats,
        "描述索引": {cid: merged_index[cid] for cid in sorted(merged_index, key=_sort_key)},
    }


def _slim_item(item: dict) -> dict:
    out: dict = {}
    for field in KEEP_FIELDS:
        if field not in item:
            continue
        value = item[field]
        if field == "备注" and value in (None, "", []):
            continue  # 空备注不占 token
        out[field] = value
    return out


def _sort_key(cid: str) -> tuple:
    """条件ID 自然排序：先按前缀（IN 优先于 EX），再按各数字段。"""
    prefix_order = {"IN": 0, "EX": 1}
    m = re.match(r"^([A-Za-z]+)", cid)
    prefix = m.group(1).upper() if m else "ZZ"
    nums = tuple(int(n) for n in re.findall(r"\d+", cid))
    return (prefix_order.get(prefix, 9), prefix, nums, cid)


def _load(paths: list[str]) -> list[dict]:
    return [json.loads(Path(p).read_text(encoding="utf-8")) for p in paths]


def _write(path: str | Path, payload: dict) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return out




def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="解析收尾：切判定输入包 / 双轨合成全量包")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sl = sub.add_parser("slim", help="把单轨 criteria 精简成该轨的判定输入包")
    sl.add_argument("--criteria", required=True, help="criteria_parsed_{IN|EX}.json")
    sl.add_argument("--qc", required=True, help="该轨的 criteria_qc_{IN|EX}.json；QC 未收敛时阻断")
    sl.add_argument("--track", required=True, choices=list(TRACKS))
    sl.add_argument("--out", required=True, help="criteria_judge_{IN|EX}.json")
    sl.add_argument("--force-qc-unconverged", action="store_true", help="人工确认后带病放行 QC 闸（结构闸不可绕过）")

    asm = sub.add_parser("assemble", help="把 IN/EX 两轨合成全量 criteria_parsed.json")
    asm.add_argument("--in-criteria", required=True)
    asm.add_argument("--in-qc", required=True)
    asm.add_argument("--ex-criteria", required=True)
    asm.add_argument("--ex-qc", required=True)
    asm.add_argument("--meta", required=True, help="criteria_meta.json（全篇级 `方案元数据`，可含 `解析说明`）")
    asm.add_argument("--out", required=True, help="criteria_parsed.json")
    asm.add_argument("--force-qc-unconverged", action="store_true", help="人工确认后带病放行两轨 QC 闸（结构/跨轨闸不可绕过）")

    dep = sub.add_parser("split", help="[已退役] 改用 slim（按轨）+ assemble（合成）")
    dep.add_argument("--criteria")
    dep.add_argument("--qc")
    dep.add_argument("--out-dir")
    dep.add_argument("--track")
    dep.add_argument("--force-qc-unconverged", action="store_true")

    args = ap.parse_args(argv)

    if args.cmd == "split":
        print(
            "⛔ `split` 已退役（单轨切分不再存在）。\n"
            "  双轨流程请改用：\n"
            "    parse_pack.py slim --criteria criteria_parsed_IN.json --qc criteria_qc_IN.json --track IN --out criteria_judge_IN.json\n"
            "    parse_pack.py slim --criteria criteria_parsed_EX.json --qc criteria_qc_EX.json --track EX --out criteria_judge_EX.json\n"
            "    parse_pack.py assemble --in-criteria ... --in-qc ... --ex-criteria ... --ex-qc ... --meta criteria_meta.json --out criteria_parsed.json",
            file=sys.stderr,
        )
        return 2

    if args.cmd == "slim":
        criteria = json.loads(Path(args.criteria).read_text(encoding="utf-8"))
        try:
            if args.force_qc_unconverged:
                print(f"⚠ 已人工放行 {args.track} 轨 QC 闸（--force-qc-unconverged）：判定包可能基于未收敛的标准", file=sys.stderr)
            else:
                check_qc_gate(json.loads(Path(args.qc).read_text(encoding="utf-8")), track=args.track, blocked_action=f"切分 criteria_judge_{args.track}.json")
            # 结构闸永不绕过：分类归属/前缀错配属确定性错误，带病切分必出残缺判定包。
            check_track_structure(criteria, args.track)
            pack = slim_track(criteria, args.track)
            check_shards_non_empty({args.track: pack})
        except SplitBlocked as exc:
            print(f"⛔ 切分闸门未通过：{exc}", file=sys.stderr)
            return 2
        out = _write(args.out, pack)
        print(f"{args.track}: 条件 {pack['条件数']} 条 → {out} ({out.stat().st_size} bytes)")
        return 0

    if args.cmd == "assemble":
        tracks = {
            "IN": json.loads(Path(args.in_criteria).read_text(encoding="utf-8")),
            "EX": json.loads(Path(args.ex_criteria).read_text(encoding="utf-8")),
        }
        qc_paths = {"IN": args.in_qc, "EX": args.ex_qc}
        try:
            for track in TRACKS:
                if args.force_qc_unconverged:
                    print(f"⚠ 已人工放行 {track} 轨 QC 闸（--force-qc-unconverged）", file=sys.stderr)
                else:
                    check_qc_gate(json.loads(Path(qc_paths[track]).read_text(encoding="utf-8")), track=track, blocked_action="合成全量 criteria_parsed.json")
                check_track_structure(tracks[track], track)
            check_cross_track(tracks)
            meta = json.loads(Path(args.meta).read_text(encoding="utf-8"))
            merged = assemble_tracks(tracks, meta)
            # 合成结果再过一次全量结构闸（四类目齐备、前缀↔类目一致、统计与实际一致）。
            check_criteria_structure(merged)
        except SplitBlocked as exc:
            print(f"⛔ 合成闸门未通过：{exc}", file=sys.stderr)
            return 2
        out = _write(args.out, merged)
        stats = merged["汇总统计"]
        print(f"合成全量包 → {out} ({out.stat().st_size} bytes)；汇总统计={stats}；描述索引 {len(merged['描述索引'])} 条")
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
