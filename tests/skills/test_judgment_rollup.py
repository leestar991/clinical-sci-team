"""主入排条件组级汇总（`rollup.py`）真值表测试。

子条件判定回答的是「`IN-10-3` 这一小条达标吗」，读者真正要的是「入选标准第 10 条整体达标吗」。
`rollup_document()` 把子条件结论按**结论空间**折叠到主条件（`IN-10` / `EX-1`）。本文件用真值表
锁死折叠口径，防止日后「顺手改优先级」：

- AND 侧（同一主条件的并列子条件）：`不符合 > 存疑 > 无法判断 > 符合`
- OR 侧（IN 轨 `或组`，任一满足即整条满足）：`符合 > 存疑 > 无法判断 > 不符合`
- EX 轨 `或组`（任一触发即整条触发）在结论空间等价于 AND，用 AND 优先级

最关键的一行是 `test_in_or_group_one_met_wins`：`IN-5`（PSA 进展 **或** 软组织进展 **或**
骨病灶进展）患者只满足 PSA 一支、另两支因无相应检查而「无法判断」。若按 AND 汇总，整条会被
判成不达标 —— **等于错误淘汰患者**，正是 eligibility-judgment SKILL.md 反复警告的那条 ⛔。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "skills" / "custom" / "eligibility-judgment" / "scripts" / "rollup.py"

if not SCRIPT_PATH.exists():  # skills/custom 为 gitignore 目录，清空检出时跳过
    pytest.skip("eligibility-judgment 技能未安装", allow_module_level=True)


def _load_module():
    spec = importlib.util.spec_from_file_location("judgment_rollup", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


rollup = _load_module()


def _j(conclusion: str, **extra) -> dict:
    """最小判定条目（只有汇总用得到的字段）。"""
    return {"conclusion": conclusion, "reason": "…", "evidence": [], **extra}


def _in_group(conclusion: str, group: str = "IN-5-OR") -> dict:
    return _j(conclusion, **{"或组": group, "或组语义": "任一满足即整条满足"})


def _ex_group(conclusion: str, group: str = "EX-1-OR") -> dict:
    entry = _j(conclusion, **{"或组": group, "或组语义": "任一触发即整条触发"})
    if conclusion in ("符合", "不符合"):
        entry["exclusion_triggered"] = conclusion == "不符合"
    return entry


def _ex_direction(conclusion: str) -> dict:
    """排除项条目：conclusion 与 exclusion_triggered 按闸4 口径配对。"""
    entry = _j(conclusion)
    if conclusion in ("符合", "不符合"):
        entry["exclusion_triggered"] = conclusion == "不符合"
    return entry


# --------------------------------------------------------------------------- #
# 主条件 ID 推导
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "cid,expected",
    [
        ("IN-2-1", "IN-2"),
        ("IN-10-12", "IN-10"),
        ("EX-1-3", "EX-1"),
        ("IN-1", "IN-1"),  # 无 -N 后缀：主条件即自身
        ("EX-6", "EX-6"),
        ("EX-20", "EX-20"),
    ],
)
def test_parent_id(cid: str, expected: str):
    assert rollup.parent_id(cid) == expected


@pytest.mark.parametrize("cid", ["", "X-1", "IN", "备注", "IN-A-1"])
def test_parent_id_rejects_non_conforming_ids(cid: str):
    assert rollup.parent_id(cid) is None


@pytest.mark.parametrize("cid,track", [("IN-2-1", "IN"), ("EX-6", "EX"), ("in-2-1", "IN")])
def test_track_of(cid: str, track: str):
    assert rollup.track_of(cid) == track


# --------------------------------------------------------------------------- #
# AND 折叠真值表（同一主条件的并列子条件；用户决策：不符合 > 存疑 > 无法判断 > 符合）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "subs,expected",
    [
        (["符合", "符合", "符合"], "符合"),
        (["符合", "不符合"], "不符合"),
        (["不符合", "存疑", "无法判断"], "不符合"),  # 不符合最高优先
        (["符合", "存疑"], "存疑"),
        (["符合", "存疑", "无法判断"], "存疑"),  # 存疑压过无法判断
        (["符合", "无法判断"], "无法判断"),
        (["无法判断", "无法判断"], "无法判断"),
    ],
)
def test_and_rollup_truth_table(subs: list[str], expected: str):
    judgments = {f"IN-10-{i + 1}": _j(c) for i, c in enumerate(subs)}
    table, _summary, warnings = rollup.rollup_document(judgments)
    assert table["IN-10"]["conclusion"] == expected
    assert warnings == []


def test_and_rollup_records_members_counts_and_rule():
    judgments = {
        "IN-10-1": _j("符合"),
        "IN-10-2": _j("存疑"),
        "IN-10-3": _j("无法判断"),
    }
    table, summary, _ = rollup.rollup_document(judgments)
    entry = table["IN-10"]
    assert entry["conclusion"] == "存疑"
    assert entry["track"] == "IN"
    assert entry["rule"] == "AND"
    assert entry["members"] == ["IN-10-1", "IN-10-2", "IN-10-3"]
    assert entry["decided_by"] == ["IN-10-2"]  # 只列决定该结论的子条件
    assert entry["counts"] == {"符合": 1, "不符合": 0, "存疑": 1, "无法判断": 1}
    assert "or_groups" not in entry  # 无或组时不出现该键
    assert summary == {"符合": 0, "不符合": 0, "存疑": 1, "无法判断": 0}


def test_all_met_lists_every_member_as_decided_by():
    judgments = {"IN-3-1": _j("符合"), "IN-3-2": _j("符合")}
    table, _summary, _ = rollup.rollup_document(judgments)
    assert table["IN-3"]["decided_by"] == ["IN-3-1", "IN-3-2"]


def test_single_sub_condition_parent_takes_its_own_conclusion():
    judgments = {"EX-6": _ex_direction("不符合"), "IN-1": _j("符合")}
    table, summary, warnings = rollup.rollup_document(judgments)
    assert table["EX-6"]["conclusion"] == "不符合"
    assert table["EX-6"]["rule"] == "单条"
    assert table["EX-6"]["members"] == ["EX-6"]
    assert table["IN-1"]["conclusion"] == "符合"
    assert table["IN-1"]["rule"] == "单条"
    assert summary == {"符合": 1, "不符合": 1, "存疑": 0, "无法判断": 0}
    assert warnings == []


# --------------------------------------------------------------------------- #
# IN 轨 或组：任一满足即整条满足（防错误淘汰患者）
# --------------------------------------------------------------------------- #
def test_in_or_group_one_met_wins():
    """IN-5：PSA 进展一支符合，另两支无相应检查判无法判断 → 整条**符合**。

    ⛔ 按 AND 汇总会得到「无法判断」，等于错误淘汰患者。
    """
    judgments = {
        "IN-5-1": _in_group("符合"),
        "IN-5-2": _in_group("无法判断"),
        "IN-5-3": _in_group("无法判断"),
    }
    table, summary, warnings = rollup.rollup_document(judgments)
    entry = table["IN-5"]
    assert entry["conclusion"] == "符合"
    assert entry["rule"] == "OR组"
    assert entry["decided_by"] == ["IN-5-1"]  # 组级 decided_by 展开成子条件ID
    assert entry["counts"] == {"符合": 1, "不符合": 0, "存疑": 0, "无法判断": 2}
    assert entry["or_groups"]["IN-5-OR"] == {
        "conclusion": "符合",
        "semantics": "任一满足即整条满足",
        "members": ["IN-5-1", "IN-5-2", "IN-5-3"],
        "decided_by": ["IN-5-1"],
    }
    assert summary["符合"] == 1
    assert warnings == []


@pytest.mark.parametrize(
    "subs,expected",
    [
        (["符合", "不符合"], "符合"),  # 任一符合即满足，其余分支不构成障碍
        (["不符合", "存疑"], "存疑"),
        (["不符合", "无法判断"], "无法判断"),
        (["存疑", "无法判断"], "存疑"),  # 存疑压过无法判断
        (["不符合", "不符合"], "不符合"),  # 全部分支都不满足才算整组不满足
    ],
)
def test_in_or_group_truth_table(subs: list[str], expected: str):
    judgments = {f"IN-5-{i + 1}": _in_group(c) for i, c in enumerate(subs)}
    table, _summary, _ = rollup.rollup_document(judgments)
    assert table["IN-5"]["conclusion"] == expected


def test_in_parent_mixes_or_group_and_standalone_sub_conditions():
    """同一主条件既有或组分支又有并列子条件：组内 OR 折叠后再与并列项 AND 折叠。"""
    judgments = {
        "IN-5-1": _in_group("符合"),
        "IN-5-2": _in_group("不符合"),
        "IN-5-3": _j("存疑"),  # 并列子条件，不属于或组
    }
    table, _summary, _ = rollup.rollup_document(judgments)
    entry = table["IN-5"]
    assert entry["rule"] == "AND+OR组"
    assert entry["conclusion"] == "存疑"  # 组=符合，与并列的存疑 AND 折叠
    assert entry["decided_by"] == ["IN-5-3"]


# --------------------------------------------------------------------------- #
# EX 轨 或组：任一触发即整条触发（结论空间等价 AND）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "subs,expected",
    [
        (["符合", "符合", "符合", "符合"], "符合"),  # 四支都未触发
        (["符合", "不符合"], "不符合"),  # 任一支触发 → 整条触发
        (["符合", "存疑"], "存疑"),
        (["符合", "无法判断"], "无法判断"),
        (["不符合", "存疑"], "不符合"),  # 已触发即定论
    ],
)
def test_ex_or_group_truth_table(subs: list[str], expected: str):
    judgments = {f"EX-1-{i + 1}": _ex_group(c) for i, c in enumerate(subs)}
    table, _summary, warnings = rollup.rollup_document(judgments)
    assert table["EX-1"]["conclusion"] == expected
    assert table["EX-1"]["track"] == "EX"
    assert warnings == []


def test_ex_or_group_semantics_recorded():
    judgments = {"EX-1-1": _ex_group("符合"), "EX-1-2": _ex_group("不符合")}
    table, _summary, _ = rollup.rollup_document(judgments)
    group = table["EX-1"]["or_groups"]["EX-1-OR"]
    assert group["semantics"] == "任一触发即整条触发"
    assert group["conclusion"] == "不符合"
    assert group["decided_by"] == ["EX-1-2"]


# --------------------------------------------------------------------------- #
# 排序：主条件 IN 先 EX 后、数字段升序
# --------------------------------------------------------------------------- #
def test_parents_are_naturally_ordered():
    judgments = {
        "EX-2": _ex_direction("符合"),
        "IN-10-1": _j("符合"),
        "EX-10-1": _ex_direction("符合"),
        "IN-2-1": _j("符合"),
    }
    table, _summary, _ = rollup.rollup_document(judgments)
    assert list(table) == ["IN-2", "IN-10", "EX-2", "EX-10"]


def test_members_are_naturally_ordered_not_lexicographically():
    judgments = {f"IN-10-{n}": _j("符合") for n in (10, 2, 1)}
    table, _summary, _ = rollup.rollup_document(judgments)
    assert table["IN-10"]["members"] == ["IN-10-1", "IN-10-2", "IN-10-10"]


# --------------------------------------------------------------------------- #
# 告警（不阻断）
# --------------------------------------------------------------------------- #
def test_missing_or_semantics_is_inferred_from_track_with_warning():
    """`或组语义` 缺失（切包丢字段）→ 按轨前缀推断，并出声。"""
    judgments = {
        "IN-5-1": _j("符合", **{"或组": "IN-5-OR"}),
        "IN-5-2": _j("无法判断", **{"或组": "IN-5-OR"}),
    }
    table, _summary, warnings = rollup.rollup_document(judgments)
    assert table["IN-5"]["conclusion"] == "符合"  # 仍按 IN 轨 OR 语义折叠
    assert table["IN-5"]["or_groups"]["IN-5-OR"]["semantics"] == "任一满足即整条满足"
    assert any("或组语义" in w and "IN-5-OR" in w for w in warnings)


def test_wrong_track_semantics_is_overridden_by_track_prefix_with_warning():
    """IN 轨条目却写了 EX 的语义 → 以轨前缀为准（否则会错误淘汰患者），并出声。"""
    judgments = {
        "IN-5-1": _j("符合", **{"或组": "IN-5-OR", "或组语义": "任一触发即整条触发"}),
        "IN-5-2": _j("无法判断", **{"或组": "IN-5-OR", "或组语义": "任一触发即整条触发"}),
    }
    table, _summary, warnings = rollup.rollup_document(judgments)
    assert table["IN-5"]["conclusion"] == "符合"
    assert any("不符" in w or "不一致" in w for w in warnings)


def test_or_group_spanning_multiple_parents_warns_and_scopes_per_parent():
    judgments = {
        "IN-5-1": _in_group("符合", group="IN-X-OR"),
        "IN-6-1": _in_group("无法判断", group="IN-X-OR"),
    }
    table, _summary, warnings = rollup.rollup_document(judgments)
    assert table["IN-5"]["conclusion"] == "符合"
    assert table["IN-6"]["conclusion"] == "无法判断"  # 未被跨主条件的兄弟带偏
    assert any("IN-X-OR" in w and "跨主条件" in w for w in warnings)


def test_unknown_conclusion_degrades_to_uncertain_with_warning():
    judgments = {"IN-2-1": _j("疑似符合"), "IN-2-2": _j("符合")}
    table, _summary, warnings = rollup.rollup_document(judgments)
    assert table["IN-2"]["conclusion"] == "无法判断"
    assert any("IN-2-1" in w for w in warnings)


def test_non_conforming_condition_id_is_skipped_with_warning():
    judgments = {"IN-2-1": _j("符合"), "备注": _j("符合")}
    table, _summary, warnings = rollup.rollup_document(judgments)
    assert list(table) == ["IN-2"]
    assert any("备注" in w for w in warnings)


def test_non_dict_entries_are_skipped_without_crashing():
    judgments = {"IN-2-1": _j("符合"), "_示例说明": "这不是判定条目"}
    table, _summary, warnings = rollup.rollup_document(judgments)
    assert list(table) == ["IN-2"]
    assert warnings == []  # 下划线开头的注释键静默跳过，不刷噪声


def test_empty_judgments_yields_empty_table():
    table, summary, warnings = rollup.rollup_document({})
    assert table == {}
    assert summary == {"符合": 0, "不符合": 0, "存疑": 0, "无法判断": 0}
    assert warnings == []


# --------------------------------------------------------------------------- #
# 中文键兼容（判定产物允许 结论/conclusion 两种写法）
# --------------------------------------------------------------------------- #
def test_chinese_key_aliases_are_accepted():
    judgments = {
        "IN-5-1": {"结论": "符合", "或组": "IN-5-OR", "或组语义": "任一满足即整条满足"},
        "IN-5-2": {"结论": "无法判断", "或组": "IN-5-OR", "或组语义": "任一满足即整条满足"},
    }
    table, _summary, warnings = rollup.rollup_document(judgments)
    assert table["IN-5"]["conclusion"] == "符合"
    assert warnings == []


# --------------------------------------------------------------------------- #
# references/schema_example.json 的 criteria_rollup 样例必须自洽
# --------------------------------------------------------------------------- #
#
# 样例文件是判定子代理**直接对照抄写**的形态参照物（criteria-parser 的 schema_example.json 曾
# 长期是非法 JSON 而无人发现）。criteria_rollup 虽由脚本产出、无需子代理手写，但样例若自相矛盾
# 就会教出错误的心智模型，因此在这里机械校验。

EXAMPLE_PATH = SCRIPT_PATH.parent.parent / "references" / "schema_example.json"


def _example_rollups() -> list[tuple[str, str, dict, dict]]:
    """[(doc_key, pid, entry, doc)]，跳过 `_示例说明` 这类注释键。

    统一证据源形态（顶层 `criteria_rollup`，无 documents 维度）与历史多 documents
    形态都支持；顶层形态的 doc_key 用 `patient_id`。
    """
    import json

    data = json.loads(EXAMPLE_PATH.read_text(encoding="utf-8"))
    out = []
    if isinstance(data.get("criteria_rollup"), dict):
        doc = {
            "criteria_rollup": data["criteria_rollup"],
            "rollup_summary": data.get("rollup_summary"),
        }
        for pid, entry in data["criteria_rollup"].items():
            if pid.startswith("_") or not isinstance(entry, dict):
                continue
            out.append((str(data.get("patient_id", "doc")), pid, entry, doc))
    for doc_key, doc in (data.get("documents") or {}).items():
        for pid, entry in (doc.get("criteria_rollup") or {}).items():
            if pid.startswith("_") or not isinstance(entry, dict):
                continue
            out.append((doc_key, pid, entry, doc))
    return out


@pytest.mark.skipif(not EXAMPLE_PATH.exists(), reason="schema_example.json 未安装")
def test_schema_example_declares_criteria_rollup():
    assert _example_rollups(), "schema_example.json 未给出 criteria_rollup 样例"


@pytest.mark.skipif(not EXAMPLE_PATH.exists(), reason="schema_example.json 未安装")
def test_schema_example_rollup_entries_are_self_consistent():
    offenders: list[str] = []
    for doc_key, pid, entry, _doc in _example_rollups():
        where = f"{doc_key}/{pid}"
        if entry.get("conclusion") not in rollup.CONCLUSIONS:
            offenders.append(f"{where}: conclusion={entry.get('conclusion')!r} 不在四类枚举内")
        if entry.get("track") != rollup.track_of(pid):
            offenders.append(f"{where}: track={entry.get('track')!r} 与主条件前缀不一致")
        if rollup.parent_id(pid) != pid:
            offenders.append(f"{where}: 键不是主条件ID（应形如 IN-2 / EX-1）")
        members = entry.get("members") or []
        if not members:
            offenders.append(f"{where}: members 为空")
        if any(rollup.parent_id(cid) != pid for cid in members):
            offenders.append(f"{where}: members 含不属于该主条件的子条件")
        if list(members) != sorted(members, key=rollup.sort_key):
            offenders.append(f"{where}: members 未按自然序排列")
        counts = entry.get("counts") or {}
        if set(counts) != set(rollup.CONCLUSIONS):
            offenders.append(f"{where}: counts 的键不是四类枚举")
        elif sum(counts.values()) != len(members):
            offenders.append(f"{where}: counts 合计 {sum(counts.values())} ≠ members {len(members)} 条")
        decided = entry.get("decided_by") or []
        if not decided or any(cid not in members for cid in decided):
            offenders.append(f"{where}: decided_by 必须非空且是 members 的子集")
        if entry.get("rule") not in ("单条", "AND", "OR组", "AND+OR组"):
            offenders.append(f"{where}: rule={entry.get('rule')!r} 非法")
        if entry.get("rule") == "单条" and len(members) != 1:
            offenders.append(f"{where}: rule=单条 但 members 有 {len(members)} 条")
        for gid, group in (entry.get("or_groups") or {}).items():
            expected = rollup.TRACK_SEMANTICS[entry.get("track", "IN")]
            if group.get("semantics") != expected:
                offenders.append(f"{where}/{gid}: semantics 应为「{expected}」")
            if any(cid not in members for cid in group.get("members") or []):
                offenders.append(f"{where}/{gid}: 组成员不在主条件 members 内")
    assert not offenders, "schema_example.json 的 criteria_rollup 样例不自洽：\n" + "\n".join(offenders)


@pytest.mark.skipif(not EXAMPLE_PATH.exists(), reason="schema_example.json 未安装")
def test_schema_example_rollup_summary_matches_its_table():
    for doc_key, _pid, _entry, doc in _example_rollups():
        table = {pid: e for pid, e in doc["criteria_rollup"].items() if not pid.startswith("_") and isinstance(e, dict)}
        expected = {c: 0 for c in rollup.CONCLUSIONS}
        for e in table.values():
            if e.get("conclusion") in expected:
                expected[e["conclusion"]] += 1
        actual = {k: v for k, v in (doc.get("rollup_summary") or {}).items() if not k.startswith("_")}
        assert actual == expected, f"{doc_key}: rollup_summary {actual} 与 criteria_rollup 重算 {expected} 不一致"
        break  # 每个 document 只需校验一次


# --------------------------------------------------------------------------- #
# `或组` 的权威来源是标准包，不是判定条目
#
# 会话 `d1883294` 的真实故障：判定子代理落盘的条目只有
# `conclusion / reason / evidence / matching`，**没有 `或组`** —— 而当时
# `rollup_document()` 只从判定条目读该字段。于是 13 个或组全部退化成未分组子条件，
# 每个主条件都被算成 `AND`：IN-7（RECIST 可测量病灶 **或** PCWG3 骨转移）的
# IN-7-1=无法判断 / IN-7-2=符合 被 AND 折叠成「无法判断」，正确答案是「符合」。
# 全程零告警 —— 旧告警只在 `或组` **存在**但语义不符/跨主条件时才响。
#
# 教训：`或组` 是**结构事实**，权威出处是 `criteria_parsed_*.json`（`merge-judgments`
# 在磁盘上就能读到）。让 LLM 把结构字段原样转抄一遍再依赖它，与 `81562273` 的
# 「张冠李戴」是同一类设计缺陷。
#
# ⚠️ 本文件上方 `_in_group()` / `_ex_group()` 夹具主动往判定条目里塞 `或组`，
# 恰好补上了真实数据缺的那一块 —— 算法真值表覆盖得很好，边界契约却零覆盖。
# 以下用例专门补这个缺口，⛔ 不要用那两个夹具写它们。
# --------------------------------------------------------------------------- #
def _bare(conclusion: str) -> dict:
    """真实判定条目的形态：没有 `或组`，只有结论与理由。"""
    return {"conclusion": conclusion, "reason": "…", "evidence": [], "matching": {}}


# 标准包侧的权威或组映射（`criteria_parsed_*.json` 的 条件ID → 或组字段）
IN7_GROUPS = {
    "IN-7-1": {"或组": "IN-7-OR", "或组语义": "任一满足即整条满足"},
    "IN-7-2": {"或组": "IN-7-OR", "或组语义": "任一满足即整条满足"},
}


def test_groups_from_criteria_pack_drive_the_rollup():
    """判定条目不带 `或组`，但标准包带 —— 必须按 OR 折叠。这是 d1883294 的正解。"""
    judgments = {"IN-7-1": _bare("无法判断"), "IN-7-2": _bare("符合")}
    table, summary, _warnings = rollup.rollup_document(judgments, groups=IN7_GROUPS)
    entry = table["IN-7"]
    assert entry["conclusion"] == "符合", "IN 轨或组任一支符合即整条符合"
    assert entry["rule"] == "OR组"
    assert entry["or_groups"]["IN-7-OR"]["decided_by"] == ["IN-7-2"]
    assert summary["符合"] == 1


def test_without_groups_the_same_data_collapses_wrongly():
    """锁死故障形态本身：不传 groups 时就是 d1883294 的错误结果。

    这条不是在为错误行为背书 —— 它保证「传了 groups」与「没传」两条路径的差异
    不会在日后重构中被悄悄抹平，从而让人误以为无 groups 也安全。
    """
    judgments = {"IN-7-1": _bare("无法判断"), "IN-7-2": _bare("符合")}
    table, _summary, _warnings = rollup.rollup_document(judgments)
    assert table["IN-7"]["conclusion"] == "无法判断"
    assert table["IN-7"]["rule"] == "AND"


def test_pack_groups_win_over_conflicting_entry_groups_with_warning():
    """条目与包冲突时以**包**为准并出声。条目里的值是 LLM 转抄的，不可信。"""
    judgments = {
        "IN-7-1": _bare("无法判断"),
        # 子代理把或组名抄错了（或抄成了别条的组）
        "IN-7-2": {**_bare("符合"), "或组": "IN-7-WRONG", "或组语义": "任一满足即整条满足"},
    }
    table, _summary, warnings = rollup.rollup_document(judgments, groups=IN7_GROUPS)
    assert table["IN-7"]["conclusion"] == "符合"
    assert "IN-7-OR" in table["IN-7"]["or_groups"]
    assert "IN-7-WRONG" not in table["IN-7"]["or_groups"]
    assert any("IN-7-2" in w and "IN-7-WRONG" in w for w in warnings), warnings


def test_entry_groups_are_used_when_pack_omits_that_condition():
    """包里没登记该条时回退到条目值 —— 向后兼容老产物，不要直接丢掉。"""
    judgments = {
        "IN-5-1": _in_group("符合"),
        "IN-5-2": _in_group("无法判断"),
    }
    table, _summary, _warnings = rollup.rollup_document(judgments, groups={})
    assert table["IN-5"]["conclusion"] == "符合"
    assert table["IN-5"]["rule"] == "OR组"


def test_pack_declares_groups_but_none_materialised_is_blocking():
    """包声明了或组、汇总却一个组都没产出 → 阻断级，不是告警。

    这是 d1883294 的静默点：默认落到 AND，而 AND 恰好是 IN 轨最危险的方向
    （把「满足其一即可」读成「必须全部满足」，错误淘汰患者）。
    """
    judgments = {"IN-7-1": _bare("无法判断"), "IN-7-2": _bare("符合")}
    # groups 声明了 IN-7-OR，但键与判定条目对不上（如条件ID 拼写漂移）
    stale = {"IN-7-01": {"或组": "IN-7-OR", "或组语义": "任一满足即整条满足"}}
    with pytest.raises(rollup.RollupBlocked) as exc:
        rollup.rollup_document(judgments, groups=stale)
    assert "IN-7-OR" in str(exc.value)


def test_partial_group_materialisation_is_blocking():
    """包声明 2 组、只落地 1 组也阻断 —— 部分丢失同样会静默翻转方向。"""
    judgments = {
        "IN-5-1": _bare("符合"),
        "IN-5-2": _bare("无法判断"),
        "IN-7-1": _bare("无法判断"),
        "IN-7-2": _bare("符合"),
    }
    groups = {
        "IN-5-1": {"或组": "IN-5-OR", "或组语义": "任一满足即整条满足"},
        "IN-5-2": {"或组": "IN-5-OR", "或组语义": "任一满足即整条满足"},
        "IN-7-1": {"或组": "IN-7-OR", "或组语义": "任一满足即整条满足"},
        "IN-7-9": {"或组": "IN-7-OR", "或组语义": "任一满足即整条满足"},  # 键漂移
    }
    # IN-7-OR 仍会因 IN-7-1 落地而成组，故这里改成整组都对不上
    del groups["IN-7-1"]
    with pytest.raises(rollup.RollupBlocked) as exc:
        rollup.rollup_document(judgments, groups=groups)
    msg = str(exc.value)
    assert "IN-7-OR" in msg and "IN-5-OR" not in msg, "只点名真正丢失的组"


def test_groups_none_keeps_backward_compatibility():
    """`groups=None`（老调用方）不触发阻断校验 —— 无从知道包里该有几组。"""
    judgments = {"IN-1": _bare("符合")}
    table, _summary, warnings = rollup.rollup_document(judgments, groups=None)
    assert table["IN-1"]["rule"] == "单条"
    assert not [w for w in warnings if "或组" in w]


def test_extract_or_groups_reads_criteria_pack_shape():
    """从 `criteria_parsed_*.json` / `criteria_judge_*.json` 的 `四分类` 结构提取或组映射。"""
    pack = {
        "四分类": {
            "入选_可从病例获取": [
                {"条件ID": "IN-7-1", "或组": "IN-7-OR", "或组语义": "任一满足即整条满足"},
                {"条件ID": "IN-7-2", "或组": "IN-7-OR", "或组语义": "任一满足即整条满足"},
                {"条件ID": "IN-9", "或组": None},
            ],
            "入选_不可从病例获取": [{"条件ID": "IN-1"}],
        }
    }
    groups = rollup.extract_or_groups(pack)
    assert set(groups) == {"IN-7-1", "IN-7-2"}, "只登记真正带或组的条目"
    assert groups["IN-7-1"]["或组"] == "IN-7-OR"
    assert groups["IN-7-1"]["或组语义"] == "任一满足即整条满足"


def test_extract_or_groups_merges_multiple_packs():
    """两轨包分别传入时合并成一张表。"""
    in_pack = {"四分类": {"入选_可从病例获取": {"IN-7-1": {"条件ID": "IN-7-1", "或组": "IN-7-OR"}}}}
    ex_pack = {"四分类": {"排除_可从病例获取": {"EX-1-1": {"条件ID": "EX-1-1", "或组": "EX-1-OR"}}}}
    groups = rollup.extract_or_groups(in_pack, ex_pack)
    assert set(groups) == {"IN-7-1", "EX-1-1"}


def test_extract_or_groups_tolerates_missing_structure():
    """包结构异常时返回空表而不抛 —— 阻断由调用方按「声明了几组」判断。"""
    assert rollup.extract_or_groups({}) == {}
    assert rollup.extract_or_groups({"四分类": None}) == {}
    assert rollup.extract_or_groups({"四分类": {"入选_可从病例获取": "not-a-list"}}) == {}
