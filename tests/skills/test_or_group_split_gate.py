"""OR 分支拆分闸(criteria-parser 闸11)。

**规则变更(2026-08-06)**:原规则是「OR 关系整体保留、不拆分」,OR 分支只写进 `或条件` 字段。
改为**把异质 OR 分支拆成并行原子子条件**,并用 `或组` / `或组语义` 标记同组关系。

动机(用户给出的排除标准实例)：
    "对试验药物的任一活性成分或辅料过敏者，或有特异性变态反应病史（哮喘、风湿、湿疹性皮炎）
     仍需全身性药物治疗者或曾发生过其它严重的过敏反应者；"
整体保留成一条时，判定子代理要在一次比对里回答四件互不相干的事，实测倾向只答最容易的那支，
且四支的证据位置各不相同（用药史 / 过敏史 / 既往史+用药史 / 过敏史），共用一套 `同义词` 也不合理。
应拆成 4 条：①活性成分过敏 ②辅料过敏 ③变态反应病史 **且** 仍需全身性药物治疗 ④其它严重过敏反应。

**为什么必须有 `或组`**:汇总语义在两轨相反,拆开后若不标组就会算错——
- EX：组内任一"不符合"(触发) → 整条触发 → 建议排除(与既有约束 17 一致)；
- IN：组内任一"符合" → 该组即满足。**若不标组**，约束 18「全部入选'符合'」会把
  "满足其一即可"读成"必须全部满足"：IN-5(PSA 进展 **或** 软组织进展 **或** 骨病灶进展)
  患者只满足 PSA 一支，另两支为「无法判断」，整体就被错判为不符合入选 —— 错误淘汰患者。

本闸锁定可机械判定的部分；"这几支到底是不是同一件事"需语义判断，只给建议级提示。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "skills" / "custom" / "criteria-parser" / "scripts" / "check_track_structure.py"

if not SCRIPT_PATH.exists():  # skills/custom 为 gitignore 目录
    pytest.skip("criteria-parser 技能未安装", allow_module_level=True)


def _load():
    spec = importlib.util.spec_from_file_location("check_track_structure_or", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


cts = _load()

IN_SEM = "任一满足即整条满足"
EX_SEM = "任一触发即整条触发"


# ─────────────────────────────── 夹具 ───────────────────────────────


def item(cid, *, 或组=None, 或组语义=None, 或条件=None, 匹配字段=None, 逻辑关系="单条件", 逻辑关系备注=None, 子条件="x") -> dict:
    o = {
        "条件ID": cid,
        "来源标准": "标准 第1条",
        "原文": "原文",
        "子条件": 子条件,
        "逻辑关系": 逻辑关系,
        "可从病例获取": True,
    }
    if 逻辑关系备注 is not None:
        o["逻辑关系备注"] = 逻辑关系备注
    t: dict = {}
    if 匹配字段 is not None:
        t["匹配字段"] = 匹配字段
    if 或条件 is not None:
        t["或条件"] = 或条件
    if t:
        t.setdefault("同义词", ["同义词占位"])
        t.setdefault("证据位置", "病历")
        o["转化条件"] = t
    if 或组 is not None:
        o["或组"] = 或组
    if 或组语义 is not None:
        o["或组语义"] = 或组语义
    return o


def write(workspace: Path, track: str, items: list[dict]) -> Path:
    cat = ("入选" if track == "IN" else "排除") + "_可从病例获取"
    payload = {
        "四分类": {cat: {str(i["条件ID"]): i for i in items}},  # 类目规范形态：以 条件ID 为键的 dict
        "描述索引": {i["条件ID"]: "短描述" for i in items},
    }
    p = workspace / f"criteria_parsed_{track}.json"
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return p


def run(workspace: Path, track: str) -> dict:
    return cts.check_track(workspace, track, None, False)


def gate8(report: dict, key: str = "problems") -> list[str]:
    return [m for m in report[key] if "闸11" in m]


# ────────────────────── 正常形态：拆开且标组 ──────────────────────


def test_split_or_group_passes(tmp_path: Path):
    """用户给的 EX-1 拆成 4 条并标组 → 闸8 无问题。"""
    items = [
        item("EX-1-1", 或组="EX-1-OR", 或组语义=EX_SEM, 匹配字段="用药史", 子条件="对试验药物的任一活性成分过敏", 逻辑关系="OR分支"),
        item("EX-1-2", 或组="EX-1-OR", 或组语义=EX_SEM, 匹配字段="过敏史", 子条件="对试验药物的任一辅料过敏", 逻辑关系="OR分支"),
        item("EX-1-3", 或组="EX-1-OR", 或组语义=EX_SEM, 匹配字段=["既往史", "用药史"], 子条件="有特异性变态反应病史（哮喘、风湿、湿疹性皮炎）且仍需全身性药物治疗", 逻辑关系="OR分支", 逻辑关系备注="内部为限定性AND：病史 + 仍需全身性药物治疗"),
        item("EX-1-4", 或组="EX-1-OR", 或组语义=EX_SEM, 匹配字段="过敏史", 子条件="曾发生过其它严重的过敏反应", 逻辑关系="OR分支"),
    ]
    write(tmp_path, "EX", items)
    assert gate8(run(tmp_path, "EX")) == []


def test_in_track_or_group_passes(tmp_path: Path):
    items = [
        item("IN-5-1", 或组="IN-5-OR", 或组语义=IN_SEM, 匹配字段="PSA", 子条件="PSA 进展"),
        item("IN-5-2", 或组="IN-5-OR", 或组语义=IN_SEM, 匹配字段="影像学", 子条件="软组织进展"),
        item("IN-5-3", 或组="IN-5-OR", 或组语义=IN_SEM, 匹配字段="骨扫描", 子条件="骨病灶进展"),
    ]
    write(tmp_path, "IN", items)
    assert gate8(run(tmp_path, "IN")) == []


def test_items_without_or_group_are_untouched(tmp_path: Path):
    write(tmp_path, "IN", [item("IN-2-1", 匹配字段="年龄"), item("IN-2-2", 匹配字段="性别")])
    assert gate8(run(tmp_path, "IN")) == []


# ────────────────────── 阻断级：组语义错误 ──────────────────────


def test_single_member_or_group_is_blocking(tmp_path: Path):
    """一个组只有一个成员 = 要么漏拆了别的分支，要么组标记是多余的。"""
    write(tmp_path, "EX", [item("EX-1-1", 或组="EX-1-OR", 或组语义=EX_SEM, 匹配字段="用药史")])
    msgs = gate8(run(tmp_path, "EX"))
    assert msgs, "单成员或组必须阻断"
    assert "EX-1-OR" in msgs[0]


def test_inconsistent_semantics_within_group_is_blocking(tmp_path: Path):
    write(
        tmp_path,
        "EX",
        [
            item("EX-1-1", 或组="EX-1-OR", 或组语义=EX_SEM, 匹配字段="用药史"),
            item("EX-1-2", 或组="EX-1-OR", 或组语义=IN_SEM, 匹配字段="过敏史"),
        ],
    )
    msgs = gate8(run(tmp_path, "EX"))
    assert msgs
    assert any("语义" in m for m in msgs)


def test_unknown_semantics_value_is_blocking(tmp_path: Path):
    write(
        tmp_path,
        "IN",
        [
            item("IN-5-1", 或组="IN-5-OR", 或组语义="随便写的", 匹配字段="PSA"),
            item("IN-5-2", 或组="IN-5-OR", 或组语义="随便写的", 匹配字段="影像学"),
        ],
    )
    assert gate8(run(tmp_path, "IN"))


def test_track_semantics_mismatch_is_blocking(tmp_path: Path):
    """IN 轨写成 EX 的语义 → 汇总方向会反，必须阻断。"""
    write(
        tmp_path,
        "IN",
        [
            item("IN-5-1", 或组="IN-5-OR", 或组语义=EX_SEM, 匹配字段="PSA"),
            item("IN-5-2", 或组="IN-5-OR", 或组语义=EX_SEM, 匹配字段="影像学"),
        ],
    )
    msgs = gate8(run(tmp_path, "IN"))
    assert msgs
    assert any(IN_SEM in m for m in msgs)


def test_missing_semantics_is_blocking(tmp_path: Path):
    write(
        tmp_path,
        "EX",
        [item("EX-1-1", 或组="EX-1-OR", 匹配字段="用药史"), item("EX-1-2", 或组="EX-1-OR", 匹配字段="过敏史")],
    )
    assert gate8(run(tmp_path, "EX"))


def test_group_spanning_different_source_clauses_is_blocking(tmp_path: Path):
    """同一 OR 组必须来自同一原条号；跨条同组说明组标记串了。"""
    write(
        tmp_path,
        "EX",
        [
            item("EX-1-1", 或组="EX-1-OR", 或组语义=EX_SEM, 匹配字段="用药史"),
            item("EX-2-1", 或组="EX-1-OR", 或组语义=EX_SEM, 匹配字段="过敏史"),
        ],
    )
    msgs = gate8(run(tmp_path, "EX"))
    assert msgs
    assert any("原条号" in m for m in msgs)


# ────────────────── 建议级：疑似 OR 应拆未拆 ──────────────────


def test_multi_branch_or_without_group_is_advisory(tmp_path: Path):
    """`或条件` 有多支却没拆成组 → 建议级提示（可能是漏拆，也可能是同一事实的等价表述）。"""
    write(
        tmp_path,
        "EX",
        [
            item(
                "EX-1",
                匹配字段="过敏史",
                或条件=["对活性成分过敏", "有变态反应病史且仍需全身治疗", "曾发生严重过敏反应"],
                逻辑关系="单条件",
                逻辑关系备注="OR 分支整体保留在 `或条件`（本用例即待检出的漏拆形态）",
            )
        ],
    )
    report = run(tmp_path, "EX")
    assert gate8(report, "problems") == [], "不得阻断——同一事实的单位/参考范围变体是合法的多分支"
    assert gate8(report, "notes"), "但必须给出建议级提示，供 QC 复核是否应拆"


def test_two_branch_or_same_fact_only_advisory_not_blocking(tmp_path: Path):
    """睾酮 <50 ng/dL 或 <1.7 nmol/L 是同一事实的单位变体，合法不拆。"""
    write(
        tmp_path,
        "IN",
        [item("IN-4", 匹配字段="睾酮", 或条件=["睾酮<50 ng/dL", "睾酮<1.7 nmol/L"], 逻辑关系="单条件", 逻辑关系备注="两个单位为等价表述，不拆")],
    )
    report = run(tmp_path, "IN")
    assert gate8(report, "problems") == []


def test_advisory_message_names_the_condition(tmp_path: Path):
    write(
        tmp_path,
        "EX",
        [item("EX-3", 匹配字段="既往治疗史", 或条件=["A 治疗", "B 治疗", "C 治疗"], 逻辑关系="单条件")],
    )
    notes = gate8(run(tmp_path, "EX"), "notes")
    assert notes
    assert "EX-3" in notes[0]


def test_split_group_does_not_trigger_advisory(tmp_path: Path):
    """已经拆成组的条目不该再被提示"应拆未拆"。"""
    items = [
        item("EX-1-1", 或组="EX-1-OR", 或组语义=EX_SEM, 匹配字段="用药史", 或条件=["活性成分A", "活性成分B"]),
        item("EX-1-2", 或组="EX-1-OR", 或组语义=EX_SEM, 匹配字段="过敏史"),
    ]
    write(tmp_path, "EX", items)
    report = run(tmp_path, "EX")
    assert gate8(report, "problems") == []
    assert gate8(report, "notes") == []
