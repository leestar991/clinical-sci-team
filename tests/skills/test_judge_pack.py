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
        "排除_可从病例获取": [{"条件ID": "EX-15", "原文": "活动性乙肝/丙肝/梅毒/HIV", "可从病例获取": True, "备注": "见附录"}],
        "排除_不可从病例获取": [{"条件ID": "EX-17", "原文": "研究者认为不适合入组", "可从病例获取": False}],
    },
}


def _track_criteria(track: str, *, source: dict | None = None) -> dict:
    """从全量样例裁出单轨文件，模拟双轨解析各自的产出。"""
    src = source or CRITERIA
    keyword = pack.SHARDS[track]
    four = {c: items for c, items in src["四分类"].items() if keyword in c}
    index = {cid: text for cid, text in (src.get("描述索引") or {}).items() if cid.startswith(track)}
    out: dict = {"四分类": four}
    if index:
        out["描述索引"] = index
    return out


IN_CRITERIA = _track_criteria("IN")
EX_CRITERIA = _track_criteria("EX")
META = {"方案元数据": {"方案编号": "TEST-101", "方案标题": "测试方案", "来源": "试验方案.pdf 第4章"}}


# --- slim（按轨精简） --------------------------------------------------------


def test_slim_scopes_pack_to_its_own_track():
    in_pack = pack.slim_track(IN_CRITERIA, "IN")
    ex_pack = pack.slim_track(EX_CRITERIA, "EX")
    assert set(in_pack["四分类"]) == {"入选_可从病例获取", "入选_不可从病例获取"}
    assert set(ex_pack["四分类"]) == {"排除_可从病例获取", "排除_不可从病例获取"}
    assert (in_pack["条件数"], ex_pack["条件数"]) == (2, 2)
    assert (in_pack["分片"], ex_pack["分片"]) == ("入选", "排除")


def test_slim_keeps_four_category_shape_for_downstream_gates():
    """精简包必须仍带 `四分类`，否则 uncertain_recheck / exclusion_direction_check 无法吃。"""
    for track, criteria in (("IN", IN_CRITERIA), ("EX", EX_CRITERIA)):
        slim = pack.slim_track(criteria, track)
        assert "四分类" in slim
        for items in slim["四分类"].values():
            assert isinstance(items, list)


def test_slim_strips_non_judgment_fields_and_empty_notes():
    in_pack = pack.slim_track(IN_CRITERIA, "IN")
    item = in_pack["四分类"]["入选_可从病例获取"][0]
    assert "内部调试字段" not in item
    assert "备注" not in item  # None 备注被剔除
    # 判定必需字段全部保留（含同义词/证据位置所在的转化条件）
    for field in ("条件ID", "原文", "子条件", "逻辑关系", "可从病例获取", "转化条件", "日期维度"):
        assert field in item
    assert item["转化条件"]["同义词"] == ["年龄"]
    # 非空备注保留
    assert pack.slim_track(EX_CRITERIA, "EX")["四分类"]["排除_可从病例获取"][0]["备注"] == "见附录"


def test_slim_drops_top_level_noise():
    noisy_track = dict(IN_CRITERIA, 方案元数据={"方案编号": "X"}, 解析说明={"a": 1}, 汇总统计={"n": 2}, 描述索引={"IN-1": "知情同意"})
    slim = pack.slim_track(noisy_track, "IN")
    for noisy in ("方案元数据", "解析说明", "汇总统计", "描述索引"):
        assert noisy not in slim


def test_slim_pack_is_smaller_than_full_criteria():
    full = len(json.dumps(CRITERIA, ensure_ascii=False))
    for track, criteria in (("IN", IN_CRITERIA), ("EX", EX_CRITERIA)):
        assert len(json.dumps(pack.slim_track(criteria, track), ensure_ascii=False)) < full


def test_track_structure_gate_rejects_missing_four_categories():
    with pytest.raises(pack.SplitBlocked):
        pack.check_track_structure({"foo": 1}, "IN")


def test_track_structure_gate_rejects_unknown_track():
    with pytest.raises(pack.SplitBlocked):
        pack.check_track_structure(IN_CRITERIA, "XX")


def test_track_structure_gate_rejects_opposite_track_items():
    """IN 轨文件混入 EX-* 条目 → 阻断（否则两轨会重复/漏掉同一批条件）。"""
    polluted = {
        "四分类": {
            "入选_可从病例获取": [{"条件ID": "IN-2-1", "原文": "年龄≥18"}],
            "入选_不可从病例获取": [{"条件ID": "EX-15", "原文": "活动性乙肝"}],
        }
    }
    with pytest.raises(pack.SplitBlocked, match="前缀应为 IN-"):
        pack.check_track_structure(polluted, "IN")


def test_track_structure_gate_rejects_opposite_track_categories():
    polluted = {
        "四分类": {
            "入选_可从病例获取": [{"条件ID": "IN-2-1", "原文": "年龄≥18"}],
            "入选_不可从病例获取": [],
            "排除_可从病例获取": [{"条件ID": "EX-15", "原文": "活动性乙肝"}],
        }
    }
    with pytest.raises(pack.SplitBlocked, match="含对侧类目"):
        pack.check_track_structure(polluted, "IN")


def test_track_structure_gate_accepts_clean_track():
    pack.check_track_structure(IN_CRITERIA, "IN")
    pack.check_track_structure(EX_CRITERIA, "EX")


# --- assemble（双轨合成全量包） ----------------------------------------------


def test_assemble_produces_full_schema_shape():
    merged = pack.assemble_tracks({"IN": IN_CRITERIA, "EX": EX_CRITERIA}, META)
    assert list(merged) == ["方案元数据", "解析说明", "四分类", "汇总统计", "描述索引"]
    assert set(merged["四分类"]) == set(pack.EXPECTED_CATEGORIES)
    assert merged["方案元数据"]["方案编号"] == "TEST-101"


def test_assemble_recomputes_summary_stats():
    """`汇总统计` 必须重算，不信任任一轨自报的数字（本例样例里写的是 总条数=4）。"""
    merged = pack.assemble_tracks({"IN": IN_CRITERIA, "EX": EX_CRITERIA}, META)
    assert merged["汇总统计"] == {
        "入选_可从病例获取": 1,
        "入选_不可从病例获取": 1,
        "排除_可从病例获取": 1,
        "排除_不可从病例获取": 1,
        "子条件总数": 4,
    }


def test_assemble_injects_default_parse_notes_when_meta_omits_them():
    merged = pack.assemble_tracks({"IN": IN_CRITERIA, "EX": EX_CRITERIA}, META)
    assert merged["解析说明"] == pack.DEFAULT_PARSE_NOTES


def test_assemble_prefers_parse_notes_from_meta():
    meta = dict(META, 解析说明={"拆分原则": "自定义"})
    merged = pack.assemble_tracks({"IN": IN_CRITERIA, "EX": EX_CRITERIA}, meta)
    assert merged["解析说明"] == {"拆分原则": "自定义"}


def test_assemble_blocks_on_empty_protocol_meta():
    """`方案元数据` 全空 → 阻断（历史缺陷 thread 5a1c8d95：该块无人认领、全空放行）。"""
    with pytest.raises(pack.SplitBlocked, match="方案元数据"):
        pack.assemble_tracks({"IN": IN_CRITERIA, "EX": EX_CRITERIA}, {"方案元数据": {"方案编号": "", "方案标题": None}})


def test_assemble_merges_description_index_sorted():
    in_c = dict(IN_CRITERIA, 描述索引={"IN-2": "年龄", "IN-1": "知情同意"})
    ex_c = dict(EX_CRITERIA, 描述索引={"EX-15": "感染"})
    merged = pack.assemble_tracks({"IN": in_c, "EX": ex_c}, META)
    assert list(merged["描述索引"]) == ["IN-1", "IN-2", "EX-15"]


def test_cross_track_gate_rejects_duplicate_condition_ids():
    in_c = {"四分类": {"入选_可从病例获取": [{"条件ID": "IN-1"}], "入选_不可从病例获取": []}}
    ex_c = {"四分类": {"排除_可从病例获取": [{"条件ID": "IN-1"}], "排除_不可从病例获取": []}}
    with pytest.raises(pack.SplitBlocked, match="跨轨重复"):
        pack.check_cross_track({"IN": in_c, "EX": ex_c})


def test_cross_track_gate_rejects_description_index_collision():
    in_c = dict(IN_CRITERIA, 描述索引={"IN-1": "知情同意"})
    ex_c = dict(EX_CRITERIA, 描述索引={"IN-1": "被排除轨也写了"})
    with pytest.raises(pack.SplitBlocked, match="描述索引"):
        pack.check_cross_track({"IN": in_c, "EX": ex_c})


def test_cross_track_gate_accepts_disjoint_tracks():
    pack.check_cross_track({"IN": IN_CRITERIA, "EX": EX_CRITERIA})


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


def test_shard_documents_consistent_accepts_identical_keys():
    pack.check_shard_documents_consistent([IN_SHARD, EX_SHARD])  # 不抛即通过


def test_shard_documents_blocks_self_invented_keys():
    """thread 345f2bf4：IN 轨写 combined_ocr、EX 轨写 screening_bundle。

    merge_judgments 按 doc_key 用 setdefault 合并，键不同会静默并列成两个假文档——
    交付物看似 60 条实则从未合并。必须在合并前阻断。
    """
    a = {"patient_id": "P", "documents": {"combined_ocr": {"judgments": {"IN-1": {"conclusion": "符合"}}}}}
    b = {"patient_id": "P", "documents": {"screening_bundle": {"judgments": {"EX-1": {"conclusion": "符合"}}}}}
    with pytest.raises(pack.SplitBlocked) as exc:
        pack.check_shard_documents_consistent([a, b])
    msg = str(exc.value)
    assert "combined_ocr" in msg and "screening_bundle" in msg
    assert "假文档" in msg


def test_shard_documents_blocks_partial_overlap():
    """一轨多一个键也算不一致（漏判了一份来源）。"""
    a = {"documents": {"病历": {"judgments": {}}, "检查": {"judgments": {}}}}
    b = {"documents": {"病历": {"judgments": {}}}}
    with pytest.raises(pack.SplitBlocked):
        pack.check_shard_documents_consistent([a, b])


def test_shard_documents_ignores_empty_shard():
    """某轨完全没有 documents 时不误报（由其他闸负责）。"""
    a = {"documents": {"病历": {"judgments": {}}}}
    pack.check_shard_documents_consistent([a, {"documents": {}}])
    pack.check_shard_documents_consistent([a, {}])


def test_merge_judgments_cli_exit_2_on_inconsistent_keys(tmp_path):
    a = tmp_path / "a.json"
    b = tmp_path / "b.json"
    a.write_text(json.dumps({"documents": {"combined_ocr": {"judgments": {"IN-1": {"conclusion": "符合"}}}}}), encoding="utf-8")
    b.write_text(json.dumps({"documents": {"screening_bundle": {"judgments": {"EX-1": {"conclusion": "符合"}}}}}), encoding="utf-8")
    rc = pack.main(["merge-judgments", "--shards", str(a), str(b), "--out", str(tmp_path / "m.json")])
    assert rc == 2
    assert not (tmp_path / "m.json").exists()


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


QC_PASSED = {"passed": True, "round": 2, "blocking_issues": [], "residual_issues": [{"condition_id": "IN-1", "action": "建议补录"}]}


def _write_json(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_cli_slim_and_merge_roundtrip(tmp_path: Path):
    criteria_path = _write_json(tmp_path / "criteria_parsed_IN.json", IN_CRITERIA)
    qc_path = _write_json(tmp_path / "criteria_qc_IN.json", QC_PASSED)
    out_pack = tmp_path / "criteria_judge_IN.json"

    assert pack.main(["slim", "--criteria", str(criteria_path), "--qc", str(qc_path), "--track", "IN", "--out", str(out_pack)]) == 0
    in_pack = json.loads(out_pack.read_text(encoding="utf-8"))
    assert in_pack["分片"] == "入选"

    a, b = tmp_path / "a.json", tmp_path / "b.json"
    a.write_text(json.dumps(IN_SHARD, ensure_ascii=False), encoding="utf-8")
    b.write_text(json.dumps(EX_SHARD, ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "nested" / "judgments_draft.json"
    assert pack.main(["merge-judgments", "--shards", str(a), str(b), "--out", str(out)]) == 0
    merged = json.loads(out.read_text(encoding="utf-8"))
    assert len(merged["documents"]["doc"]["judgments"]) == 5


# --- split 闸门 -------------------------------------------------------------
#
# 回归 thread 5a1c8d95 的两个故障：
#   1. 切分与 QC 第 2 轮同轮发出 → 在 QC 未收敛、EX-* 还错放在「入选」类目时切出
#      IN 46 条 / EX 1 条的残缺判定包；
#   2. QC 2 轮仍有阻断项时，主代理把 blocking_issues 挪进 residual_issues 标注
#      「已达QC轮次上限，带建议放行」、置 passed=true，未经 QC 复核即推进判定。


# 故障态（双轨版）：EX-* 被写进 IN 轨的「入选_不可从病例获取」
MISCLASSIFIED_IN_TRACK = {
    "四分类": {
        "入选_可从病例获取": [{"条件ID": "IN-2-1", "原文": "年龄≥18"}],
        "入选_不可从病例获取": [{"条件ID": "IN-1", "原文": "知情同意"}, {"条件ID": "EX-15", "原文": "活动性乙肝"}],
    }
}


def _slim_cli(tmp_path: Path, track: str, criteria: dict, qc: dict, *extra: str) -> int:
    return pack.main(
        [
            "slim",
            "--criteria",
            str(_write_json(tmp_path / f"criteria_parsed_{track}.json", criteria)),
            "--qc",
            str(_write_json(tmp_path / f"criteria_qc_{track}.json", qc)),
            "--track",
            track,
            "--out",
            str(tmp_path / f"criteria_judge_{track}.json"),
            *extra,
        ]
    )


def _no_pack_written(tmp_path: Path, track: str) -> bool:
    return not (tmp_path / f"criteria_judge_{track}.json").exists()


def test_slim_blocked_when_qc_not_passed(tmp_path: Path):
    qc = {"passed": False, "round": 2, "blocking_issues": [{"condition_id": "IN-3"}, {"id": "CQC-R2-003"}]}
    assert _slim_cli(tmp_path, "IN", IN_CRITERIA, qc) == 2
    assert _no_pack_written(tmp_path, "IN")


def test_slim_blocked_when_passed_true_but_blocking_remains(tmp_path: Path):
    qc = {"passed": True, "round": 2, "blocking_issues": [{"condition_id": "IN-3"}]}
    assert _slim_cli(tmp_path, "IN", IN_CRITERIA, qc) == 2
    assert _no_pack_written(tmp_path, "IN")


def test_slim_blocked_when_round_limit_self_released(tmp_path: Path):
    """阻断项被降级为建议后自我放行 —— blocking_issues 已空、passed=true 也必须拦住。"""
    qc = {
        "passed": True,
        "round": 2,
        "blocking_issues": [],
        "residual_issues": [
            {"id": "CQC-R2-001", "condition_id": "IN-3", "status": "已手工修订；因已达QC轮次上限（2轮），带建议放行"},
        ],
        "note": "已达 criteria QC 轮次上限（2轮），阻断问题已手工修订后带建议放行。",
    }
    assert _slim_cli(tmp_path, "IN", IN_CRITERIA, qc) == 2
    assert _no_pack_written(tmp_path, "IN")


def test_slim_blocked_when_condition_id_prefix_contradicts_track(tmp_path: Path):
    """回放故障态：EX-* 被写进「入选_不可从病例获取」。"""
    assert _slim_cli(tmp_path, "IN", MISCLASSIFIED_IN_TRACK, QC_PASSED) == 2
    assert _no_pack_written(tmp_path, "IN")


def test_structure_gate_is_not_bypassable_by_force_flag(tmp_path: Path):
    """QC 闸可人工放行，结构闸不可 —— 分类归属错误是确定性错误。"""
    qc = {"passed": False, "round": 2, "blocking_issues": [{"condition_id": "IN-3"}]}
    assert _slim_cli(tmp_path, "IN", MISCLASSIFIED_IN_TRACK, qc, "--force-qc-unconverged") == 2
    assert _no_pack_written(tmp_path, "IN")


def test_force_flag_allows_slim_when_only_qc_gate_fails(tmp_path: Path):
    qc = {"passed": False, "round": 2, "blocking_issues": [{"condition_id": "IN-3"}]}
    assert _slim_cli(tmp_path, "IN", IN_CRITERIA, qc, "--force-qc-unconverged") == 0
    assert (tmp_path / "criteria_judge_IN.json").exists()


def test_slim_blocked_when_track_pack_would_be_empty(tmp_path: Path):
    empty_track = {"四分类": {"入选_可从病例获取": [], "入选_不可从病例获取": []}}
    assert _slim_cli(tmp_path, "IN", empty_track, QC_PASSED) == 2
    assert _no_pack_written(tmp_path, "IN")


def test_slim_allowed_when_qc_converged_and_structure_clean(tmp_path: Path):
    assert _slim_cli(tmp_path, "EX", EX_CRITERIA, QC_PASSED) == 0
    ex_pack = json.loads((tmp_path / "criteria_judge_EX.json").read_text(encoding="utf-8"))
    assert ex_pack["条件数"] == 2


def _assemble_cli(tmp_path: Path, *, in_qc: dict, ex_qc: dict, meta: dict | None = None, extra: tuple[str, ...] = ()) -> int:
    return pack.main(
        [
            "assemble",
            "--in-criteria",
            str(_write_json(tmp_path / "criteria_parsed_IN.json", IN_CRITERIA)),
            "--in-qc",
            str(_write_json(tmp_path / "criteria_qc_IN.json", in_qc)),
            "--ex-criteria",
            str(_write_json(tmp_path / "criteria_parsed_EX.json", EX_CRITERIA)),
            "--ex-qc",
            str(_write_json(tmp_path / "criteria_qc_EX.json", ex_qc)),
            "--meta",
            str(_write_json(tmp_path / "criteria_meta.json", META if meta is None else meta)),
            "--out",
            str(tmp_path / "criteria_parsed.json"),
            *extra,
        ]
    )


def test_assemble_cli_writes_full_pack_when_both_tracks_converged(tmp_path: Path):
    assert _assemble_cli(tmp_path, in_qc=QC_PASSED, ex_qc=QC_PASSED) == 0
    merged = json.loads((tmp_path / "criteria_parsed.json").read_text(encoding="utf-8"))
    assert merged["汇总统计"]["子条件总数"] == 4
    assert set(merged["四分类"]) == set(pack.EXPECTED_CATEGORIES)


def test_assemble_cli_blocked_when_one_track_qc_unconverged(tmp_path: Path):
    """一轨未收敛即阻断整个合成（全局暂停语义的机械落点）。"""
    unconverged = {"passed": False, "round": 2, "blocking_issues": [{"condition_id": "EX-16"}]}
    assert _assemble_cli(tmp_path, in_qc=QC_PASSED, ex_qc=unconverged) == 2
    assert not (tmp_path / "criteria_parsed.json").exists()


def test_assemble_cli_blocked_when_meta_is_empty(tmp_path: Path):
    assert _assemble_cli(tmp_path, in_qc=QC_PASSED, ex_qc=QC_PASSED, meta={"方案元数据": {"方案编号": ""}}) == 2
    assert not (tmp_path / "criteria_parsed.json").exists()


def test_split_subcommand_is_retired(tmp_path: Path, capsys):
    assert pack.main(["split", "--criteria", "x.json", "--qc", "y.json", "--out-dir", str(tmp_path)]) == 2
    assert "已退役" in capsys.readouterr().err


def test_slim_pack_is_accepted_by_direction_and_recheck_gates(tmp_path: Path):
    """精简包必须能被两个机械闸直接消费（回归：切分破坏 四分类 结构会让闸静默失效）。"""
    direction_path = SCRIPT_PATH.parent / "exclusion_direction_check.py"
    spec = importlib.util.spec_from_file_location("edc", direction_path)
    edc = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = edc
    spec.loader.exec_module(edc)

    ex_pack = pack.slim_track(EX_CRITERIA, "EX")
    judgments = {
        "patient_id": "P1",
        "documents": {"doc": {"judgments": {"EX-15": {"conclusion": "不符合", "reason": "HBsAg 阴性，未见活动性肝炎。"}}}},
    }
    result = edc.check(judgments, ex_pack)
    assert result["conflicts"] == ["EX-15"]

    # uncertain_recheck 同样直接吃精简包：IN 轨「无法判断」条目 + OCR 命中 → suspected_missed
    recheck_path = SCRIPT_PATH.parent / "uncertain_recheck.py"
    spec = importlib.util.spec_from_file_location("ucr", recheck_path)
    ucr = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = ucr
    spec.loader.exec_module(ucr)

    in_pack = pack.slim_track(IN_CRITERIA, "IN")
    ocr = tmp_path / "ocr_records.md"
    ocr.write_text("来源图片：/x/筛选期病历_page_001.jpg\n患者年龄 72 岁，男性。\n", encoding="utf-8")
    in_judgments = {
        "patient_id": "P1",
        "documents": {"doc": {"judgments": {"IN-2-1": {"conclusion": "无法判断", "reason": "未提及年龄。"}}}},
    }
    recheck = ucr.recheck(in_pack, in_judgments, [ocr])
    assert recheck["suspected_missed"] == ["IN-2-1"], "精简包应保留 同义词，使兜底闸能命中 OCR 原文"
