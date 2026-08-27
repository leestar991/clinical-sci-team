#!/usr/bin/env python3
"""判定域分片工具：批次切分(plan-batches) + 分片产物合并(merge-judgments/merge-recheck)。

2026-08-27 拆分：解析收尾职责(slim/assemble——切判定输入包、合成全量 criteria_parsed.json)
已迁往 `/criteria-parser/scripts/parse_pack.py`(脚本随域走:吃解析产物、闸是解析侧闸、
产出是解析交付物)。本文件只保留判定域职责。

背景：判定子代理若要吃下"全量 criteria_parsed.json + 该患者全部 OCR"，输入大、轮次长、
时延高；而入选项与排除项的解析、QC、判定彼此独立，可全程双轨并行。

本脚本提供的确定性子命令：

1. `plan-batches` —— 把**单轨**判定输入包按固定批量（默认 12 条）切成若干**批次清单**，
   供主代理逐批派判定子任务。⛔ 它**不切数据文件**：每批仍读同一份
   `criteria_judge_{TRACK}.json`，批次只是「本批负责哪些条件ID」的清单。

2. `merge-judgments` —— 把分片 `judgments_draft_*.json` 机械合并为完整 `judgments_draft.json`
   （统一证据源判定：两轨**顶层 judgments** 直接合并、重算顶层 summary、条件ID 自然排序、
   合并 warnings），并**重算主条件组级汇总**顶层 `criteria_rollup` + `rollup_summary`
   （算法见同目录 `rollup.py`）。汇总每次合并无条件覆盖;⛔ 汇总与 `judgments` **平级**。

3. `merge-recheck` —— 把分片 `uncertain_recheck_*.json` 合并为一份。

判定输入包 `criteria_judge_{IN|EX}.json` 由解析收尾产出：
`/criteria-parser/scripts/parse_pack.py slim`(QC 闸不过时拿不到——强制先收敛)。

## 为什么按批派、而不再整轨一次派（会话 `09eeaffb`）

整轨一次派的失败形态：IN 轨 28 条 / EX 轨 45 条 + 两份 OCR 由单个子代理一次判完，
99 个 AI 回合里 0 次 `write_file`，撞满 `recursion_limit=420`，两轨合计 10.02M token /
42 分钟、产物为零。批次把「不可能完成的任务」换成「若干个能完成的任务」：每批 12 条、
各自落盘，任一批撞限只损失那一批。⛔ 批次不跨轨（IN/EX 是判定的语义边界，两轨的委派
模板与闸命令都不同）;`四分类` 类目不参与切分（类目边界不是工作量边界）。

`_sort_key`(条件ID 自然序)与 `SplitBlocked` 在解析域 `parse_pack.py` 各有一份内联副本
(注释互指);算法改动须两边同步。

用法：
    python3 judge_pack.py plan-batches --criteria .../criteria_judge_IN.json --track IN \
                                       [--batch-size 12] [--patient P001] [--out .../judge_batches_P001_IN.json]
    python3 judge_pack.py merge-judgments --shards A.json B.json [--criteria ...] --out .../judgments_draft.json
    python3 judge_pack.py merge-recheck  --shards A.json B.json --out .../uncertain_recheck.json

其余子命令始终 exit 0（除参数/IO 错误）；结果以产物文件与 stdout 摘要为准。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path

def _load_sibling(module_name: str, filename: str):
    """按路径加载同目录模块。

    本脚本既被 `python3 judge_pack.py` 直接执行，也被测试用
    `importlib.spec_from_file_location` 动态加载——后者不会把脚本目录放进 `sys.path`，
    因此 `import rollup` 在测试里会 ImportError。按 `__file__` 解析绝对路径两种方式都成立，
    且不污染 `sys.path`、不与同名第三方模块冲突。
    """
    path = Path(__file__).resolve().parent / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - 文件缺失即技能安装不完整
        raise ImportError(f"无法加载 {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(module_name, module)
    spec.loader.exec_module(module)
    return module


# 主条件组级汇总算法的唯一真相源（报告侧只渲染、不重算）
rollup = _load_sibling("eligibility_judgment_rollup", "rollup.py")

# 分片定义：分片名 → 该分片包含的 `四分类` 类目关键词
SHARDS: dict[str, str] = {"IN": "入选", "EX": "排除"}
TRACKS = tuple(SHARDS)  # ("IN", "EX")


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


# 判定批次的默认批量。12 条来自会话 `09eeaffb` 的实测：整轨 28/45 条一次判完撞
# recursion_limit=420（99 个真实回合、0 次落盘）；按 ~5 个 superstep/条件的观测密度，
# 12 条 + 建索引 + 四条闸 + 落盘约 30-40 个真实回合，在 420 的额度内留足余量，
# 且批内条目数不足以把上下文推过压缩触发线（60k）。
DEFAULT_BATCH_SIZE = 12


def plan_batches(pack: dict, *, batch_size: int = DEFAULT_BATCH_SIZE) -> list[dict]:
    """把单轨判定输入包的条件ID 按自然序切成固定批量的批次清单。

    ⛔ **只产清单、不切数据**：每批的子代理仍读整份 `criteria_judge_{TRACK}.json`，
    prompt 里用 `condition_ids` 圈定"本批只判这些"。不切包有三个硬理由：

    1. **闸脚本按整轨口径校验**。`check_judgment_structure.py` 闸 2 要求判定条目集合
       **恒等于**标准包条件ID 集合；`uncertain_recheck.py` / `check_reason_alignment.py` /
       `exclusion_direction_check.py` 的 `--criteria` 也都是整轨包。若按批切出
       `criteria_judge_IN_b1.json`，闸 2 会对每个批次文件各自成立，
       却再也没人检查"所有批次并起来是否等于整轨"——漏一整批不会有任何闸报错。
    2. **或组会被切断**。`或组` 的分支可能落在不同批次（EX-12-3/4/5/6 连号但跨批边界），
       `merge-judgments --criteria` 用整轨包重算组级汇总，包一切分组就找不齐成员，
       `RollupBlocked` 或静默退化成 AND（IN 轨最危险的方向）。
    3. **切包 = 多一层可漂移的产物**。清单是纯派发信息，错了只是派错；切出来的包是判定的
       唯一标准来源，错了会按「条件 × 患者」放大到每条判定。

    返回 `[{batch, condition_ids, count, draft_file_suffix}]`，`batch` 从 1 起。
    条件ID 用 `_sort_key` 自然序（IN 先 EX 后、数字段升序），**跨 `四分类` 类目连续切分**：
    类目边界不是工作量边界，按类目切会让 IN 轨「不可从病例获取」的 4 条单独成批，
    而那 4 条同样要全量核查病历，等于多派一次任务、多付一份 OCR 读取。
    """
    if batch_size < 1:
        raise SplitBlocked(f"--batch-size 必须 ≥ 1，收到 {batch_size}")

    ids: list[str] = []
    for items in (pack.get("四分类") or {}).values():
        for entry in category_entries(items):
            cid = str(entry.get("条件ID") or "").strip()
            if cid:
                ids.append(cid)
    ids = sorted(set(ids), key=_sort_key)
    if not ids:
        raise SplitBlocked("标准包 `四分类` 里没有任何条件ID，无法规划批次；请先跑 `slim` 产出本轨包")

    batches: list[dict] = []
    for i in range(0, len(ids), batch_size):
        chunk = ids[i : i + batch_size]
        batches.append({"batch": len(batches) + 1, "condition_ids": chunk, "count": len(chunk)})
    return batches


def batch_plan(pack: dict, track: str, *, batch_size: int = DEFAULT_BATCH_SIZE, patient: str = "") -> dict:
    """`plan-batches` 的完整产物：批次清单 + 派发/合并所需的路径与总数。

    `draft_file` 带 `_b{N}` 后缀，因为每批**各自落盘**——这正是分批的收益所在：
    某批撞 `recursion_limit` 只损失那一批，其余批次的判定已在磁盘上，
    主代理据 `merge_shards` 补派缺的那批即可，不必整轨重判。
    """
    batches = plan_batches(pack, batch_size=batch_size)
    pid = patient or "{id}"
    for b in batches:
        b["draft_file"] = f"judgments_draft_{pid}_{track}_b{b['batch']}.json"
        b["recheck_file"] = f"uncertain_recheck_{pid}_{track}_b{b['batch']}.json"
    return {
        "patient_id": patient or None,
        "track": track,
        "batch_size": batch_size,
        "total_conditions": sum(b["count"] for b in batches),
        "batch_count": len(batches),
        "batches": batches,
        # 整轨合并的输入清单：每批 draft 合成本轨 draft，之后才进入原有的两轨合并。
        "merge_shards": [b["draft_file"] for b in batches],
        "track_draft_file": f"judgments_draft_{pid}_{track}.json",
        "track_recheck_file": f"uncertain_recheck_{pid}_{track}.json",
    }


def _sort_key(cid: str) -> tuple:
    """条件ID 自然排序：先按前缀（IN 优先于 EX），再按各数字段。"""
    prefix_order = {"IN": 0, "EX": 1}
    m = re.match(r"^([A-Za-z]+)", cid)
    prefix = m.group(1).upper() if m else "ZZ"
    nums = tuple(int(n) for n in re.findall(r"\d+", cid))
    return (prefix_order.get(prefix, 9), prefix, nums, cid)


class SplitBlocked(Exception):
    """切分闸门未通过 —— 必须先修复上游产物，不允许带病推进判定阶段。"""


# 主条件组级汇总算法的唯一真相源（报告侧只渲染、不重算）
rollup = _load_sibling("eligibility_judgment_rollup", "rollup.py")


def merge_judgments(shards: list[dict], groups: dict[str, dict] | None = None) -> dict:
    """合并各分片顶层 judgments，重算 summary 与主条件组级汇总。

    统一证据源判定后产物无 documents 维度：两轨的顶层 `judgments` 直接合并（IN/EX 条件ID
    前缀不同，天然不冲突；同名条件ID 属异常，保留先到者并出声）。历史多 documents 产物
    兼容：有 `documents` 时按 document 合并后**摊平为顶层 judgments**（升级为统一形态）。

    `groups`：从标准包提取的**权威**或组映射（`rollup.extract_or_groups()`）。判定条目里的
    `或组` 是判定子代理转抄的，不作为数据源 —— 真实故障 `d1883294`：条目根本没抄，13 个或组
    全部退化成 `AND`，IN-7 被折叠成「无法判断」而正确答案是「符合」，且零告警。
    传 `None` 表示调用方没给标准包，退回读条目（老流程兼容）。
    """
    merged: dict = {}
    judgments: dict[str, dict] = {}
    warnings: list = []
    cross_doc_warnings: list = []
    rollup_warnings: list[str] = []

    for shard in shards:
        for key in ("patient_id", "patient_name", "judgment_date", "protocol_id"):
            if shard.get(key) and not merged.get(key):
                merged[key] = shard[key]
        for w in shard.get("warnings") or []:
            if w not in warnings:
                warnings.append(w)
        for w in shard.get("cross_doc_warnings") or []:
            if w not in cross_doc_warnings:
                cross_doc_warnings.append(w)

        for cid, entry in (shard.get("judgments") or {}).items():
            if cid in judgments:
                rollup_warnings.append(f"两轨同名条件ID {cid}：保留先到者，请核对轨归属")
                continue
            judgments[cid] = entry
        # 历史多 documents 产物：摊平为顶层 judgments
        for doc in (shard.get("documents") or {}).values():
            if not isinstance(doc, dict):
                continue
            for cid, entry in (doc.get("judgments") or {}).items():
                if cid in judgments:
                    rollup_warnings.append(f"同名条件ID {cid} 出现于多份物料：保留先到者")
                    continue
                judgments[cid] = entry

    judgments = {cid: judgments[cid] for cid in sorted(judgments, key=_sort_key)}
    summary = {c: 0 for c in CONCLUSIONS}
    for jdg in judgments.values():
        conclusion = jdg.get("conclusion") if isinstance(jdg, dict) else None
        if conclusion in summary:
            summary[conclusion] += 1
    # 主条件组级汇总：与 `judgments` **平级**落盘。塞进 `judgments` 会多出 `IN-2` 这类
    # 不在标准包里的键，check_judgment_structure.py 闸 2（键集合恒等于条件ID 集合）
    # 会直接 exit 2。每次合并无条件重算并覆盖，因此不存在陈旧汇总。
    table, rollup_summary, doc_rollup_warnings = rollup.rollup_document(judgments, groups=groups)
    for w in doc_rollup_warnings:
        if w not in rollup_warnings:
            rollup_warnings.append(w)

    merged["judgments"] = judgments
    merged["summary"] = summary
    merged["criteria_rollup"] = table
    merged["rollup_summary"] = rollup_summary
    if warnings:
        merged["warnings"] = warnings
    if cross_doc_warnings:
        merged["cross_doc_warnings"] = cross_doc_warnings
    if rollup_warnings:
        merged["rollup_warnings"] = rollup_warnings
    return merged


def merge_recheck(shards: list[dict]) -> dict:
    merged: dict = {"patient_id": None, "checked": 0, "suspected_missed": [], "entries": []}
    for shard in shards:
        if shard.get("patient_id") and not merged["patient_id"]:
            merged["patient_id"] = shard["patient_id"]
        merged["checked"] += int(shard.get("checked") or 0)
        for cid in shard.get("suspected_missed") or []:
            if cid not in merged["suspected_missed"]:
                merged["suspected_missed"].append(cid)
        merged["entries"].extend(shard.get("entries") or [])
    merged["suspected_missed"].sort(key=_sort_key)
    merged["entries"].sort(key=lambda e: _sort_key(str(e.get("条件ID", ""))))
    return merged


def _load(paths: list[str]) -> list[dict]:
    return [json.loads(Path(p).read_text(encoding="utf-8")) for p in paths]


def _write(path: str | Path, payload: dict) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return out



class SplitBlocked(Exception):
    """闸门未通过(与解析域 parse_pack.py 的同名类互为副本,措辞改动两边同步)。"""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="判定域：批次切分 / 分片产物合并")
    sub = ap.add_subparsers(dest="cmd", required=True)

    pb = sub.add_parser("plan-batches", help="把单轨判定输入包按固定批量切成批次清单（不切数据文件）")
    pb.add_argument("--criteria", required=True, help="criteria_judge_{IN|EX}.json（`slim` 产物）")
    pb.add_argument("--track", required=True, choices=list(TRACKS))
    pb.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE, help=f"每批条件数（默认 {DEFAULT_BATCH_SIZE}）")
    pb.add_argument("--patient", default="", help="患者ID（填入产物文件名；不给则留 {id} 占位）")
    pb.add_argument("--out", help="可选：把批次清单写到该路径（默认只打印）")

    mj = sub.add_parser("merge-judgments", help="合并分片 judgments")
    mj.add_argument("--shards", required=True, nargs="+")
    mj.add_argument(
        "--criteria",
        nargs="+",
        help="本次判定所用的 criteria_judge_{IN,EX}.json（或合成后的 criteria_parsed.json）。"
        "`或组`/`或组语义` 从这里取 —— 判定条目里的同名字段是子代理转抄的，只作交叉核对。"
        "不传则退化为读条目，或组可能静默丢失（故障 d1883294）。",
    )
    mj.add_argument("--out", required=True)

    mr = sub.add_parser("merge-recheck", help="合并分片 uncertain_recheck")
    mr.add_argument("--shards", required=True, nargs="+")
    mr.add_argument("--out", required=True)

    args = ap.parse_args(argv)

    if args.cmd == "plan-batches":
        pack = json.loads(Path(args.criteria).read_text(encoding="utf-8"))
        try:
            plan = batch_plan(pack, args.track, batch_size=args.batch_size, patient=args.patient)
        except SplitBlocked as exc:
            print(f"⛔ 批次规划未通过：{exc}", file=sys.stderr)
            return 2
        print(f"{args.track} 轨 {plan['total_conditions']} 条 → {plan['batch_count']} 批（每批 ≤ {plan['batch_size']} 条）")
        for b in plan["batches"]:
            print(f"  批 {b['batch']}（{b['count']} 条）→ {b['draft_file']}")
            print(f"    条件ID：{' '.join(b['condition_ids'])}")
        print(f"合并输入（本轨）：{' '.join(plan['merge_shards'])} → {plan['track_draft_file']}")
        if args.out:
            out = _write(args.out, plan)
            print(f"批次清单已写入：{out}")
        return 0

    shards = _load(args.shards)
    if args.cmd == "merge-judgments":
        # `或组` 的权威来源是标准包，不是判定条目（详见 rollup.py 模块 docstring）。
        groups = None
        if args.criteria:
            groups = rollup.extract_or_groups(*_load(args.criteria))
        else:
            print(
                "⚠ 未传 --criteria：`或组` 只能退回读判定条目，而判定子代理常常不转抄该字段。"
                "一旦缺失，入选轨或组会静默退化成 AND（「满足其一即可」被读成「必须全部满足」，"
                "错误淘汰患者）。请补上本次判定所用的 criteria_judge_{IN,EX}.json。",
                file=sys.stderr,
            )
        try:
            merged = merge_judgments(shards, groups=groups)
        except rollup.RollupBlocked as exc:
            print(f"⛔ 组级汇总闸门未通过：{exc}", file=sys.stderr)
            return 2
        out = _write(args.out, merged)
        total = len(merged["judgments"])
        print(f"合并 {len(shards)} 个分片 → {out}；条目 {total} 条；summary={merged['summary']}")
        print(f"主条件组级汇总：主条件数={len(merged['criteria_rollup'])}；rollup_summary={merged['rollup_summary']}")
        if groups is not None:
            declared = {g["或组"] for g in groups.values() if g.get("或组")}
            print(f"或组来源=标准包（{len(args.criteria)} 份）；声明 {len(declared)} 组，全部落地")
        for line in merged.get("rollup_warnings") or []:
            print(f"⚠ 汇总告警：{line}", file=sys.stderr)
        return 0

    merged = merge_recheck(shards)
    out = _write(args.out, merged)
    print(f"合并 {len(shards)} 个反查分片 → {out}；checked={merged['checked']}；suspected_missed={merged['suspected_missed']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
