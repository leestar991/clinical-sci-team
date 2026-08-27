#!/usr/bin/env python3
"""主入排条件（`IN-2` / `EX-1`）组级汇总 —— 判定侧唯一算法真相源。

`criteria-parser` 把一条原始入排标准拆成最小子颗粒度（`IN-10` → `IN-10-1..7`），判定按子条件
逐条落盘。但读者要的是「入选标准第 10 条整体达标吗」。本模块把子条件结论按**结论空间**折叠回
主条件，产物由 `judge_pack.py merge-judgments` 写入判定产物的顶层 `criteria_rollup`。

## 为什么算法只在这里实现一份

报告侧（`screening-report-generator`）**不重算**，只渲染 `criteria_rollup`。同一套折叠口径若在
两个技能里各写一份，日后必然漂移出「判定说符合、报告说不符合」的静默分歧。

## 折叠口径（改动前务必读完）

| 场景 | 优先级 | 依据 |
|---|---|---|
| AND：同一主条件的并列子条件 | `不符合 > 存疑 > 无法判断 > 符合` | 约束 17/19（任一挡住即整条挡住；有存疑无不符合 → 需补充信息） |
| OR：IN 轨 `或组`（任一满足即整条满足） | `符合 > 存疑 > 无法判断 > 不符合` | 约束 18 |
| OR：EX 轨 `或组`（任一触发即整条触发） | 同 AND | 「任一触发」在结论空间等价于 AND，见 SKILL.md「排除项的逻辑关系」 |

⛔ **入选轨的 `或组` 绝不能按 AND 汇总**：`IN-5`（PSA 进展 **或** 软组织进展 **或** 骨病灶进展）
患者只满足 PSA 一支、另两支因无相应检查而「无法判断」，按 AND 汇总会把整条判成不达标，
**等于错误淘汰患者**。`或组语义` 缺失或与轨前缀矛盾时，一律**以轨前缀为准**并出声告警——
宁可警告刷屏，也不能让丢字段静默翻转汇总方向。

## ⛔ `或组` 的权威来源是标准包，不是判定条目

`rollup_document(judgments, groups=...)` 的 `groups` 是从 `criteria_parsed_*.json` /
`criteria_judge_*.json` 提取的 `条件ID → {或组, 或组语义}` 映射（见 `extract_or_groups`），
由 `merge-judgments --criteria` 传入。**它优先于判定条目里的同名字段**；条目里的值只用于
交叉核对，冲突时以包为准并告警。

理由：`或组` 是**结构事实**，由 `criteria-parser` 确定性产出，`merge-judgments` 在磁盘上
就能读到。判定条目是 LLM 写的 —— 让它把结构字段原样转抄一遍再依赖那份转抄，与
`81562273` 的「张冠李戴」是同一类设计缺陷。真实故障 `d1883294`：判定条目只有
`conclusion / reason / evidence / matching`，13 个或组全部退化成 `AND`，IN-7
（IN-7-1 无法判断 / IN-7-2 符合）被折叠成「无法判断」，正确答案是「符合」，**全程零告警**。

因此「包声明了或组、汇总却没落地」是 `RollupBlocked` **阻断级**，不是告警：默认落到 AND，
而 AND 恰好是 IN 轨最危险的方向。`groups=None`（未传包）时跳过该校验 —— 无从知道该有几组。

## ⛔ 为什么 `criteria_rollup` 必须与 `judgments` 平级

`check_judgment_structure.py` 闸 2 要求 `judgments` 的键集合**恒等于**本轨标准包的条件ID 集合
（不缺不多）。把主条件汇总塞进 `judgments` 会多出 `IN-2` / `EX-1` 这类不在标准包里的键，闸 2
直接 `exit 2`。因此汇总一律写在 document 下与 `judgments` 并列的 `criteria_rollup`。

汇总由 `merge-judgments` **每次无条件重算并覆盖**，因此不存在「判定改了、汇总还是旧的」这类
陈旧值问题；也正因此本模块是纯函数、不读文件、不写文件。
"""

from __future__ import annotations

import re

CONCLUSIONS: tuple[str, ...] = ("符合", "不符合", "存疑", "无法判断")

# AND 折叠：任一子条件挡住则整条挡住；存疑优先于无法判断暴露出来
AND_PRIORITY: tuple[str, ...] = ("不符合", "存疑", "无法判断", "符合")
# OR 折叠（仅 IN 轨 或组）：任一支满足即整条满足
OR_PRIORITY: tuple[str, ...] = ("符合", "存疑", "无法判断", "不符合")

# 各轨 `或组语义` 的唯一合法取值（与 criteria-parser 的拆分标记一致）
TRACK_SEMANTICS: dict[str, str] = {
    "IN": "任一满足即整条满足",
    "EX": "任一触发即整条触发",
}

_ID_RE = re.compile(r"^(IN|EX)-(\d+)(?:-(\d+))?$", re.IGNORECASE)


class RollupBlocked(Exception):
    """标准包声明了 `或组`，汇总却没落地 —— 阻断级，禁止把畸形汇总写进交付物。

    默认（无或组）落到 AND，而 AND 是 IN 轨最危险的方向：把「满足其一即可」读成
    「必须全部满足」，直接错误淘汰患者。所以这里必须炸，不能只告警。
    """


def extract_or_groups(*packs: dict) -> dict[str, dict]:
    """从标准包的 `四分类` 结构提取 `条件ID → {或组, 或组语义}`（只收真正带或组的条目）。

    接受 `criteria_parsed_{IN|EX}.json`、`criteria_judge_{IN|EX}.json` 与合成后的
    `criteria_parsed.json` —— 四者都保留 `四分类` 外层结构。多个包按传入顺序合并，
    后者覆盖前者。包结构异常时**返回空表而不抛**：该不该阻断由调用方按「声明了几组」判断。
    """
    groups: dict[str, dict] = {}
    all_cids: dict[str, int] = {}     # 父条件ID → 包中该父条件的成员总数（分臂合成完整性校验用）
    arm_entries: dict[str, str] = {}  # 条件ID → 适用臂（仅收集无显式或组的条目）
    for pack in packs:
        if not isinstance(pack, dict):
            continue
        four = pack.get("四分类")
        if not isinstance(four, dict):
            continue
        for items in four.values():
            # 类目形态：dict（key=条件ID，当前形态）或 list（旧 workspace，只读兼容）。
            # 形态合法性由 check_track_structure.py 闸13 上游阻断，这里只归一。
            if isinstance(items, dict):
                entries: list = list(items.values())
            elif isinstance(items, list):
                entries = items
            else:
                continue
            for item in entries:
                if not isinstance(item, dict):
                    continue
                cid = str(item.get("条件ID") or "").strip()
                if not cid:
                    continue
                pid = parent_id(cid)
                if pid:
                    all_cids[pid] = all_cids.get(pid, 0) + 1
                group = item.get("或组")
                if group not in (None, ""):
                    groups[cid] = {
                        "或组": str(group).strip(),
                        "或组语义": (str(item["或组语义"]).strip() if item.get("或组语义") else None),
                    }
                    continue
                tc = item.get("转化条件")
                arm = tc.get("适用臂") if isinstance(tc, dict) else None
                if arm and str(arm).strip():
                    arm_entries[cid] = str(arm).strip()
    # 分臂合成（仅 IN 轨）：同一主条件下的**全部**成员都带 `适用臂` 且 ≥2 支 → 视为按臂 OR 组，
    # 与显式或组同机制、方向按轨前缀。Ib期/II期是互斥分臂，只有患者所在臂适用，另一臂的
    # 「不符合」不得否决整条 —— 按 AND 折叠会系统性错误淘汰初治/经治其中一侧的全部患者。
    # EX 轨暂不合成：分臂互斥时「任一触发即触发」会把患者不在的那一臂的触发算到患者头上，
    # 机械口径无法判定适用臂，宁可保持 AND 让 QC 看到。
    for pid, member_count in all_cids.items():
        arm_members = {cid: arm for cid, arm in arm_entries.items() if parent_id(cid) == pid}
        if len(arm_members) >= 2 and len(arm_members) == member_count:
            track = track_of(pid)
            if track == "IN":
                arm_group = f"{pid}-ARM"
                for cid in arm_members:
                    groups.setdefault(cid, {"或组": arm_group, "或组语义": TRACK_SEMANTICS["IN"]})
    return groups


def parent_id(cid: str) -> str | None:
    """`IN-2-1` → `IN-2`；`EX-6`（无 -N 后缀）→ `EX-6`；不符合编号规范 → `None`。"""
    m = _ID_RE.match(str(cid).strip())
    if not m:
        return None
    return f"{m.group(1).upper()}-{int(m.group(2))}"


def track_of(cid: str) -> str | None:
    """条件ID 的轨（`IN` / `EX`）。轨前缀是 `或组` 汇总方向的**权威来源**。"""
    m = _ID_RE.match(str(cid).strip())
    return m.group(1).upper() if m else None


def sort_key(cid: str) -> tuple:
    """自然序：IN 先 EX 后，主号升序，子号升序（避免 `IN-10-2` 排在 `IN-10-10` 之后）。"""
    m = _ID_RE.match(str(cid).strip())
    if not m:
        return (2, 0, 0, str(cid))
    return (0 if m.group(1).upper() == "IN" else 1, int(m.group(2)), int(m.group(3) or 0), "")


def _conclusion_of(entry: dict) -> str | None:
    """取判定条目的结论（兼容 `conclusion` / `结论`）。不在四类枚举内返回 None。"""
    raw = entry.get("conclusion")
    if raw in (None, ""):
        raw = entry.get("结论")
    return raw if raw in CONCLUSIONS else None


def _collapse(units: list[tuple[str, str]], priority: tuple[str, ...]) -> tuple[str, list[str]]:
    """按优先级折叠 `[(unit_id, conclusion)]`，返回 (结论, 决定该结论的 unit_id 列表)。"""
    if not units:
        return "无法判断", []
    present = {c for _, c in units}
    for conclusion in priority:
        if conclusion in present:
            return conclusion, [uid for uid, c in units if c == conclusion]
    # priority 覆盖全部四类枚举，理论上到不了这里；保守回落
    return "无法判断", [uid for uid, _ in units]


def collapse_or_group(members: list[tuple[str, str]], track: str) -> tuple[str, list[str]]:
    """折叠一个 `或组`。IN 轨按 OR（任一满足即满足），EX 轨按 AND（任一触发即触发）。"""
    priority = OR_PRIORITY if track == "IN" else AND_PRIORITY
    return _collapse(members, priority)


def collapse_and(units: list[tuple[str, str]]) -> tuple[str, list[str]]:
    """折叠同一主条件下的并列单元（子条件或已折叠的或组）。"""
    return _collapse(units, AND_PRIORITY)


def _group_of(entry: dict) -> str | None:
    value = entry.get("或组") or entry.get("or_group")
    return str(value).strip() or None if value not in (None, "") else None


def _semantics_of(entry: dict) -> str | None:
    value = entry.get("或组语义") or entry.get("or_semantics")
    return str(value).strip() or None if value not in (None, "") else None


def _resolve_group(
    cid: str, entry: dict, groups: dict[str, dict] | None, warnings: list[str]
) -> tuple[str | None, str | None]:
    """定出该条目的 `(或组, 或组语义)`：标准包优先，条目仅作交叉核对。

    包里登记了该条件ID → 用包的值；条目值不同就告警（条目是 LLM 转抄的，不可信）。
    包里没登记（或未传包）→ 退回条目值，保持对老产物的兼容。
    """
    entry_group = _group_of(entry)
    entry_semantics = _semantics_of(entry)
    authoritative = (groups or {}).get(str(cid).strip())
    if not authoritative:
        return entry_group, entry_semantics

    group = authoritative.get("或组") or None
    semantics = authoritative.get("或组语义") or entry_semantics
    if entry_group and entry_group != group:
        warnings.append(
            f"{cid}: 判定条目的 `或组`「{entry_group}」与标准包「{group}」不一致，"
            f"已以标准包为准（条目值由判定子代理转抄，不作为数据源）"
        )
    return group, semantics


def rollup_document(judgments: dict, groups: dict[str, dict] | None = None) -> tuple[dict, dict, list[str]]:
    """把子条件判定表折叠成主条件汇总表。

    - `judgments`：`条件ID → 判定条目`；
    - `groups`：**权威**或组映射 `条件ID → {或组, 或组语义}`，由 `extract_or_groups()` 从标准包
      提取（`merge-judgments --criteria`）。优先于判定条目里的同名字段，见模块 docstring。
      传 `None` 表示调用方没有标准包，此时退回读条目并**跳过**「声明了却没落地」的阻断校验。

    返回 `(criteria_rollup, rollup_summary, warnings)`：

    - `criteria_rollup`：主条件ID → 汇总条目（自然序），字段见模块 docstring 与
      `references/judgment-schema.md`；
    - `rollup_summary`：按**主条件**计数的四类（与按子条件计数的 `summary` 是两个口径）；
    - `warnings`：`或组语义` 缺失/与轨矛盾、`或组` 跨主条件、条目与包冲突、结论枚举非法、
      条件ID 不合规。**警告不阻断**——汇总仍会产出，由 QC / 人工按告警回溯。

    抛 `RollupBlocked`：`groups` 声明了某个或组，而该组在汇总里一个成员都没落地。
    """
    warnings: list[str] = []
    # 主条件 → [(cid, conclusion, group)]
    buckets: dict[str, list[tuple[str, str, str | None]]] = {}
    # 或组 → 出现过的主条件（检测跨主条件的异常分组）
    group_parents: dict[str, set[str]] = {}
    # 或组 → 语义（按首次出现记录，用于告警文案）
    group_semantics: dict[str, str | None] = {}

    for cid, entry in judgments.items():
        if not isinstance(entry, dict):
            # `_示例说明` 这类注释键静默跳过；其余非字典值出声
            if not str(cid).startswith("_"):
                warnings.append(f"{cid}: 判定条目不是对象，未参与主条件汇总")
            continue
        if str(cid).startswith("_"):
            continue
        pid = parent_id(cid)
        if pid is None:
            warnings.append(f"{cid}: 条件ID 不符合 `IN-n[-m]` / `EX-n[-m]` 规范，未参与主条件汇总")
            continue
        conclusion = _conclusion_of(entry)
        if conclusion is None:
            raw = entry.get("conclusion", entry.get("结论"))
            warnings.append(f"{cid}: conclusion=「{raw}」不在四类枚举内，已按「无法判断」参与汇总")
            conclusion = "无法判断"
        group, semantics = _resolve_group(cid, entry, groups, warnings)
        if group:
            group_parents.setdefault(group, set()).add(pid)
            group_semantics.setdefault(group, semantics)
        buckets.setdefault(pid, []).append((cid, conclusion, group))

    for group, pids in group_parents.items():
        if len(pids) > 1:
            warnings.append(f"或组 {group} 跨主条件 {sorted(pids, key=sort_key)}，已按主条件分别汇总（同组分支应同属一条原始标准，请回查解析拆分）")

    table: dict[str, dict] = {}
    summary = {c: 0 for c in CONCLUSIONS}

    for pid in sorted(buckets, key=sort_key):
        members = sorted(buckets[pid], key=lambda t: sort_key(t[0]))
        track = track_of(pid) or "IN"
        expected_semantics = TRACK_SEMANTICS[track]

        counts = {c: 0 for c in CONCLUSIONS}
        for _cid, conclusion, _group in members:
            counts[conclusion] += 1

        # 单元 = 未分组子条件 + 每个或组折叠后的一个单元
        units: list[tuple[str, str]] = []
        # 或组 unit_id → 该组的 decided_by（AND 折叠命中该组时展开成子条件ID）
        unit_expansion: dict[str, list[str]] = {}
        grouped: dict[str, list[tuple[str, str]]] = {}
        for cid, conclusion, group in members:
            if group:
                grouped.setdefault(group, []).append((cid, conclusion))
            else:
                units.append((cid, conclusion))

        or_groups: dict[str, dict] = {}
        for group in sorted(grouped, key=lambda g: sort_key(grouped[g][0][0])):
            group_members = grouped[group]
            semantics = group_semantics.get(group)
            if semantics is None:
                warnings.append(f"或组 {group}（{pid}）缺 `或组语义`，已按轨前缀 {track} 推断为「{expected_semantics}」（字段可能在切包时丢失，请回查）")
            elif semantics != expected_semantics:
                warnings.append(f"或组 {group}（{pid}）的 `或组语义`「{semantics}」与 {track} 轨预期「{expected_semantics}」不符，已以轨前缀为准汇总")
            conclusion, decided_by = collapse_or_group(group_members, track)
            or_groups[group] = {
                "conclusion": conclusion,
                "semantics": expected_semantics,
                "members": [cid for cid, _ in group_members],
                "decided_by": decided_by,
            }
            units.append((group, conclusion))
            unit_expansion[group] = decided_by

        conclusion, decided_units = collapse_and(units)
        decided_by: list[str] = []
        for uid in decided_units:
            decided_by.extend(unit_expansion.get(uid, [uid]))
        decided_by.sort(key=sort_key)

        if or_groups:
            rule = "AND+OR组" if len(units) > len(or_groups) else "OR组"
        else:
            rule = "单条" if len(members) == 1 else "AND"

        entry: dict = {
            "conclusion": conclusion,
            "track": track,
            "rule": rule,
            "members": [cid for cid, _c, _g in members],
            "decided_by": decided_by,
            "counts": counts,
        }
        if or_groups:
            entry["or_groups"] = or_groups
        table[pid] = entry
        summary[conclusion] += 1

    if groups is not None:
        declared = {
            str(g.get("或组")).strip()
            for g in groups.values()
            if isinstance(g, dict) and g.get("或组")
        }
        materialised = set(group_parents)
        missing = sorted(declared - materialised)
        if missing:
            raise RollupBlocked(
                f"标准包声明了或组 {missing}，但汇总里这些组一个成员都没落地 —— "
                f"判定条目的条件ID 与标准包对不上（拼写漂移/条目缺失）。"
                f"⛔ 禁止带着退化成 AND 的汇总继续：IN 轨会把「满足其一即可」读成"
                f"「必须全部满足」，直接错误淘汰患者。请核对 `--criteria` 是否为本次判定所用的"
                f"同一份标准包，并回查 judgments 的条件ID 是否完整。"
            )

    return table, summary, warnings
