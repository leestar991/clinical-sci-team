"""judge_pack 回归测试：入选/排除分片包切分 + 分片产物机械合并。

保证 Phase 3 并行分片不会丢条目、不改结论、summary 重算正确。
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "skills" / "custom" / "eligibility-judgment" / "scripts" / "judge_pack.py"

if not SCRIPT_PATH.exists():  # skills/custom 为 gitignore 目录，清空检出时跳过
    pytest.skip("eligibility-judgment 技能未安装", allow_module_level=True)


def _load_module():
    spec = importlib.util.spec_from_file_location("judge_pack", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


pack = _load_module()

CRITERIA = {
    "方案元数据": {"方案编号": "TEST-101"},
    "解析说明": {"拆分原则": "AND 拆分"},
    "汇总统计": {"总条数": 4},
    "描述索引": {"IN-1": "知情同意"},
    "四分类": {
        "入选_可从病例获取": [
            {
                "条件ID": "IN-2-1",
                "来源标准": "入选标准 第2条",
                "原文": "筛选时年龄≥18周岁的男性患者。",
                "子条件": "年龄≥18周岁",
                "逻辑关系": "AND",
                "可从病例获取": True,
                "转化条件": {"匹配字段": "年龄", "运算符": "≥", "阈值": 18, "同义词": ["年龄"]},
                "日期维度": {"事件": "出生"},
                "备注": None,
                "内部调试字段": "应被剔除",
            }
        ],
        "入选_不可从病例获取": [{"条件ID": "IN-1", "原文": "签署知情同意书", "可从病例获取": False}],
        "排除_可从病例获取": [
            {"条件ID": "EX-15", "原文": "活动性乙肝/丙肝/梅毒/HIV", "可从病例获取": True, "备注": "见附录"}
        ],
        "排除_不可从病例获取": [{"条件ID": "EX-17", "原文": "研究者认为不适合入组", "可从病例获取": False}],
    },
}


# --- split -----------------------------------------------------------------


def test_split_produces_two_packs_scoped_by_category():
    packs = pack.split_criteria(CRITERIA)
    assert set(packs) == {"IN", "EX"}
    assert set(packs["IN"]["四分类"]) == {"入选_可从病例获取", "入选_不可从病例获取"}
    assert set(packs["EX"]["四分类"]) == {"排除_可从病例获取", "排除_不可从病例获取"}
    assert packs["IN"]["条件数"] == 2
    assert packs["EX"]["条件数"] == 2


def test_split_keeps_four_category_shape_for_downstream_gates():
    """分片包必须仍带 `四分类`，否则 uncertain_recheck / exclusion_direction_check 无法吃。"""
    packs = pack.split_criteria(CRITERIA)
    for shard in packs.values():
        assert "四分类" in shard
        for items in shard["四分类"].values():
            assert isinstance(items, list)


def test_split_strips_non_judgment_fields_and_empty_notes():
    packs = pack.split_criteria(CRITERIA)
    item = packs["IN"]["四分类"]["入选_可从病例获取"][0]
    assert "内部调试字段" not in item
    assert "备注" not in item  # None 备注被剔除
    # 判定必需字段全部保留（含同义词/证据位置所在的转化条件）
    for field in ("条件ID", "原文", "子条件", "逻辑关系", "可从病例获取", "转化条件", "日期维度"):
        assert field in item
    assert item["转化条件"]["同义词"] == ["年龄"]
    # 非空备注保留
    assert packs["EX"]["四分类"]["排除_可从病例获取"][0]["备注"] == "见附录"


def test_split_drops_top_level_noise():
    packs = pack.split_criteria(CRITERIA)
    for shard in packs.values():
        for noisy in ("方案元数据", "解析说明", "汇总统计", "描述索引"):
            assert noisy not in shard


def test_split_pack_is_smaller_than_full_criteria():
    packs = pack.split_criteria(CRITERIA)
    full = len(json.dumps(CRITERIA, ensure_ascii=False))
    for shard in packs.values():
        assert len(json.dumps(shard, ensure_ascii=False)) < full


def test_split_raises_without_four_categories():
    with pytest.raises(ValueError):
        pack.split_criteria({"foo": 1})


# --- merge-judgments -------------------------------------------------------


IN_SHARD = {
    "patient_id": "M016_ZALO",
    "patient_name": "张安龙",
    "documents": {
        "doc": {
            "doc_label": "张安龙",
            "source_file": "M016.pdf",
            "judgments": {
                "IN-2-1": {"conclusion": "符合", "reason": "年龄72岁"},
                "IN-10-2": {"conclusion": "符合", "reason": "PLT 275"},
                "IN-1": {"conclusion": "无法判断", "reason": "缺知情同意记录"},
            },
        }
    },
    "warnings": ["资料缺少筛选访视表"],
}

EX_SHARD = {
    "patient_id": "M016_ZALO",
    "documents": {
        "doc": {
            "judgments": {
                "EX-15": {"conclusion": "符合", "exclusion_triggered": False, "reason": "全阴性，未触发排除条件"},
                "EX-4": {"conclusion": "存疑", "reason": "缺末次用药日期"},
            }
        }
    },
    "warnings": ["资料缺少筛选访视表"],
}


def test_merge_judgments_keeps_all_entries_and_metadata():
    merged = pack.merge_judgments([IN_SHARD, EX_SHARD])
    judgments = merged["documents"]["doc"]["judgments"]
    assert set(judgments) == {"IN-1", "IN-2-1", "IN-10-2", "EX-4", "EX-15"}
    assert merged["patient_id"] == "M016_ZALO"
    assert merged["patient_name"] == "张安龙"
    assert merged["documents"]["doc"]["doc_label"] == "张安龙"
    assert merged["documents"]["doc"]["source_file"] == "M016.pdf"
    assert merged["warnings"] == ["资料缺少筛选访视表"]  # 去重


def test_merge_judgments_preserves_conclusions_verbatim():
    merged = pack.merge_judgments([IN_SHARD, EX_SHARD])
    ex15 = merged["documents"]["doc"]["judgments"]["EX-15"]
    assert ex15 == {"conclusion": "符合", "exclusion_triggered": False, "reason": "全阴性，未触发排除条件"}


def test_merge_judgments_recomputes_summary():
    merged = pack.merge_judgments([IN_SHARD, EX_SHARD])
    assert merged["documents"]["doc"]["summary"] == {"符合": 3, "不符合": 0, "存疑": 1, "无法判断": 1}


def test_merge_judgments_sorts_in_before_ex_and_numerically():
    merged = pack.merge_judgments([EX_SHARD, IN_SHARD])
    assert list(merged["documents"]["doc"]["judgments"]) == ["IN-1", "IN-2-1", "IN-10-2", "EX-4", "EX-15"]


# --- merge-recheck ---------------------------------------------------------


def test_merge_recheck_accumulates_and_unions():
    merged = pack.merge_recheck(
        [
            {"patient_id": "P1", "checked": 3, "suspected_missed": ["IN-9"], "entries": [{"条件ID": "IN-9"}]},
            {"patient_id": "P1", "checked": 2, "suspected_missed": ["EX-1"], "entries": [{"条件ID": "EX-1"}]},
        ]
    )
    assert merged["checked"] == 5
    assert merged["suspected_missed"] == ["IN-9", "EX-1"]
    assert [e["条件ID"] for e in merged["entries"]] == ["IN-9", "EX-1"]


# --- merge-reasons ---------------------------------------------------------


def test_merge_reasons_backfills_only_reason_field():
    judgments = pack.merge_judgments([IN_SHARD, EX_SHARD])
    judgments["documents"]["doc"]["judgments"]["EX-15"]["evidence"] = [{"quote": "全阴性"}]
    reasons = {"reasons": {"IN-2-1": "年龄72岁 ≥ 18周岁。", "EX-15": "全阴性，未触发该排除条件。"}}

    merged, stats = pack.merge_reasons(judgments, reasons)
    ex15 = merged["documents"]["doc"]["judgments"]["EX-15"]

    assert ex15["reason"] == "全阴性，未触发该排除条件。"
    assert ex15["conclusion"] == "符合"  # 结论未被改动
    assert ex15["exclusion_triggered"] is False
    assert ex15["evidence"] == [{"quote": "全阴性"}]
    assert stats["applied"] == ["IN-2-1", "EX-15"]
    assert "EX-4" in stats["missing_in_reasons"]
    assert stats["unknown_in_reasons"] == []


def test_merge_reasons_reports_unknown_ids():
    judgments = pack.merge_judgments([IN_SHARD])
    _, stats = pack.merge_reasons(judgments, {"reasons": {"IN-2-1": "x", "EX-99": "不存在的条目"}})
    assert stats["unknown_in_reasons"] == ["EX-99"]


def test_merge_reasons_accepts_flat_mapping():
    judgments = pack.merge_judgments([IN_SHARD])
    merged, stats = pack.merge_reasons(judgments, {"IN-1": "缺知情同意签署记录。"})
    assert merged["documents"]["doc"]["judgments"]["IN-1"]["reason"] == "缺知情同意签署记录。"
    assert stats["applied"] == ["IN-1"]


def test_merge_reasons_ignores_blank_text():
    judgments = pack.merge_judgments([IN_SHARD])
    original = judgments["documents"]["doc"]["judgments"]["IN-2-1"]["reason"]
    merged, stats = pack.merge_reasons(judgments, {"reasons": {"IN-2-1": "   "}})
    assert merged["documents"]["doc"]["judgments"]["IN-2-1"]["reason"] == original
    assert "IN-2-1" in stats["missing_in_reasons"]


def test_merge_reasons_accepts_object_entries_and_flags_conclusion_mismatch():
    """真实 report-writer 产物为 {cid: {conclusion, reason}} 形态，必须支持并交叉核对结论。"""
    judgments = pack.merge_judgments([IN_SHARD, EX_SHARD])
    reasons = {
        "reasons": {
            "IN-2-1": {"conclusion": "符合", "reason": "年龄72岁 ≥ 18周岁。"},
            "EX-15": {"conclusion": "不符合", "reason": "全阴性。"},  # 与判定 符合 不一致
        }
    }
    merged, stats = pack.merge_reasons(judgments, reasons)

    assert merged["documents"]["doc"]["judgments"]["IN-2-1"]["reason"] == "年龄72岁 ≥ 18周岁。"
    assert stats["conclusion_mismatch"] == ["EX-15"]
    # 结论本身不被 reasons 覆盖
    assert merged["documents"]["doc"]["judgments"]["EX-15"]["conclusion"] == "符合"


# --- CLI ------------------------------------------------------------------


def test_cli_split_and_merge_roundtrip(tmp_path: Path):
    criteria_path = tmp_path / "criteria_parsed.json"
    criteria_path.write_text(json.dumps(CRITERIA, ensure_ascii=False), encoding="utf-8")

    assert pack.main(["split", "--criteria", str(criteria_path), "--out-dir", str(tmp_path)]) == 0
    in_pack = json.loads((tmp_path / "criteria_judge_IN.json").read_text(encoding="utf-8"))
    assert in_pack["分片"] == "入选"

    a, b = tmp_path / "a.json", tmp_path / "b.json"
    a.write_text(json.dumps(IN_SHARD, ensure_ascii=False), encoding="utf-8")
    b.write_text(json.dumps(EX_SHARD, ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "nested" / "judgments_draft.json"
    assert pack.main(["merge-judgments", "--shards", str(a), str(b), "--out", str(out)]) == 0
    merged = json.loads(out.read_text(encoding="utf-8"))
    assert len(merged["documents"]["doc"]["judgments"]) == 5


def test_split_pack_is_accepted_by_direction_and_recheck_gates(tmp_path: Path):
    """分片包必须能被两个机械闸直接消费（回归：切分破坏 四分类 结构会让闸静默失效）。"""
    direction_path = SCRIPT_PATH.parent / "exclusion_direction_check.py"
    spec = importlib.util.spec_from_file_location("edc", direction_path)
    edc = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = edc
    spec.loader.exec_module(edc)

    ex_pack = pack.split_criteria(CRITERIA)["EX"]
    judgments = {
        "patient_id": "P1",
        "documents": {"doc": {"judgments": {"EX-15": {"conclusion": "不符合", "reason": "HBsAg 阴性，未见活动性肝炎。"}}}},
    }
    result = edc.check(judgments, ex_pack)
    assert result["conflicts"] == ["EX-15"]
