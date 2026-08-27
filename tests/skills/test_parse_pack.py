"""parse_pack 回归测试:解析收尾的切包与合成(slim / assemble + 三道闸)。

2026-08-27 自 test_judge_pack 拆入解析域(脚本同步拆分:slim/assemble 迁
criteria-parser/scripts/parse_pack.py)。保证:QC 闸真正拦住未收敛(QC 闸是
「解析→判定」的强制交接点)、单轨结构闸不可绕过、slim 只裁不丢判定必需字段。
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "skills" / "custom" / "criteria-parser" / "scripts" / "parse_pack.py"

if not SCRIPT_PATH.exists():  # skills/custom 为 gitignore 目录，清空检出时跳过
    pytest.skip("criteria-parser 技能未安装", allow_module_level=True)


def _load_module():
    spec = importlib.util.spec_from_file_location("parse_pack", SCRIPT_PATH)
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
        "入选_可从病例获取": {
            "IN-2-1": {
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
        },
        "入选_不可从病例获取": {"IN-1": {"条件ID": "IN-1", "原文": "签署知情同意书", "可从病例获取": False}},
        "排除_可从病例获取": {"EX-15": {"条件ID": "EX-15", "原文": "活动性乙肝/丙肝/梅毒/HIV", "可从病例获取": True, "备注": "见附录"}},
        "排除_不可从病例获取": {"EX-17": {"条件ID": "EX-17", "原文": "研究者认为不适合入组", "可从病例获取": False}},
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

QC_PASSED = {"passed": True, "round": 2, "blocking_issues": [], "residual_issues": [{"condition_id": "IN-1", "action": "建议补录"}]}


def _write_json(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


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



IN_SHARD = {
    "patient_id": "M016_ZALO",
    "patient_name": "张安龙",
    # 统一证据源判定产物：顶层 judgments，无 documents 维度
    "judgments": {
        "IN-2-1": {"conclusion": "符合", "reason": "年龄72岁"},
        "IN-10-2": {"conclusion": "符合", "reason": "PLT 275"},
        "IN-1": {"conclusion": "无法判断", "reason": "缺知情同意记录"},
    },
    "warnings": ["资料缺少筛选访视表"],
}

EX_SHARD = {
    "patient_id": "M016_ZALO",
    "judgments": {
        "EX-15": {"conclusion": "符合", "exclusion_triggered": False, "reason": "全阴性，未触发排除条件"},
        "EX-4": {"conclusion": "存疑", "reason": "缺末次用药日期"},
    },
    "warnings": ["资料缺少筛选访视表"],
}

# --- slim（按轨精简） --------------------------------------------------------


def test_slim_scopes_pack_to_its_own_track():
    in_pack = pack.slim_track(IN_CRITERIA, "IN")
    ex_pack = pack.slim_track(EX_CRITERIA, "EX")
    assert set(in_pack["四分类"]) == {"入选_可从病例获取", "入选_不可从病例获取"}
    assert set(ex_pack["四分类"]) == {"排除_可从病例获取", "排除_不可从病例获取"}
    assert (in_pack["条件数"], ex_pack["条件数"]) == (2, 2)
    assert (in_pack["分片"], ex_pack["分片"]) == ("入选", "排除")


def test_slim_keeps_four_category_shape_for_downstream_gates():
    """精简包必须仍带 `四分类`，否则 uncertain_recheck / exclusion_direction_check 无法吃。

    形态锁定：每个类目是**以 `条件ID` 为键的对象**。这是 `apply_json_patches` 能按身份定位的
    前提（thread `3a745b38`：数组下标寻址跨调用漂移，24 笔写入全落到前一条上且不报错）。
    """
    for track, criteria in (("IN", IN_CRITERIA), ("EX", EX_CRITERIA)):
        slim = pack.slim_track(criteria, track)
        assert "四分类" in slim
        for items in slim["四分类"].values():
            assert isinstance(items, dict)
            for key, item in items.items():
                assert key == item["条件ID"], f"dict key {key!r} 必须等于条目 条件ID {item.get('条件ID')!r}"


def test_slim_accepts_legacy_list_shape():
    """旧 workspace 的数组形态仍能被切分（只读兼容），但产出一律是 dict。"""
    legacy = {"四分类": {"入选_可从病例获取": [{"条件ID": "IN-3", "原文": "甲"}], "入选_不可从病例获取": []}}
    slim = pack.slim_track(legacy, "IN")
    assert slim["四分类"]["入选_可从病例获取"] == {"IN-3": {"条件ID": "IN-3", "原文": "甲"}}
    assert slim["条件数"] == 1


def test_slim_strips_non_judgment_fields_and_empty_notes():
    in_pack = pack.slim_track(IN_CRITERIA, "IN")
    item = in_pack["四分类"]["入选_可从病例获取"]["IN-2-1"]
    assert "内部调试字段" not in item
    assert "备注" not in item  # None 备注被剔除
    # 判定必需字段全部保留（含同义词/证据位置所在的转化条件）
    for field in ("条件ID", "原文", "子条件", "逻辑关系", "可从病例获取", "转化条件", "日期维度"):
        assert field in item
    assert item["转化条件"]["同义词"] == ["年龄"]
    # 非空备注保留
    assert pack.slim_track(EX_CRITERIA, "EX")["四分类"]["排除_可从病例获取"]["EX-15"]["备注"] == "见附录"


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
    in_c = {"四分类": {"入选_可从病例获取": {"IN-1": {"条件ID": "IN-1"}}, "入选_不可从病例获取": {}}}
    ex_c = {"四分类": {"排除_可从病例获取": {"IN-1": {"条件ID": "IN-1"}}, "排除_不可从病例获取": {}}}
    with pytest.raises(pack.SplitBlocked, match="跨轨重复"):
        pack.check_cross_track({"IN": in_c, "EX": ex_c})


def test_cross_track_gate_rejects_description_index_collision():
    in_c = dict(IN_CRITERIA, 描述索引={"IN-1": "知情同意"})
    ex_c = dict(EX_CRITERIA, 描述索引={"IN-1": "被排除轨也写了"})
    with pytest.raises(pack.SplitBlocked, match="描述索引"):
        pack.check_cross_track({"IN": in_c, "EX": ex_c})


def test_cross_track_gate_accepts_disjoint_tracks():
    pack.check_cross_track({"IN": IN_CRITERIA, "EX": EX_CRITERIA})


# --- 或组字段必须穿过 slim（否则拆分语义在切包时丢失）---------------------
#
# OR 分支已改为拆成并行原子子条件 + `或组`/`或组语义` 标记（criteria-parser 闸 8）。
# `slim` 用 KEEP_FIELDS 白名单裁字段，若这两个键不在白名单里，判定子代理拿到的包里
# 就没有组信息 —— 它会把同组分支当成彼此独立的条件，IN 轨于是把"满足其一即可"
# 按约束 18 读成"必须全部满足"，患者被错误淘汰。


def test_keep_fields_include_or_group():
    assert "或组" in pack.KEEP_FIELDS
    assert "或组语义" in pack.KEEP_FIELDS


def test_slim_preserves_or_group_fields(tmp_path: Path):
    criteria = {
        "四分类": {
            "入选_可从病例获取": {
                "IN-5-1": {
                    "条件ID": "IN-5-1",
                    "原文": "记录证实进展性mCRPC…",
                    "子条件": "PSA 进展",
                    "逻辑关系": "OR分支（同组：IN-5-OR）",
                    "可从病例获取": True,
                    "或组": "IN-5-OR",
                    "或组语义": "任一满足即整条满足",
                    "转化条件": {"匹配字段": "PSA", "同义词": ["PSA"], "证据位置": "检验报告"},
                },
                "IN-5-2": {
                    "条件ID": "IN-5-2",
                    "原文": "记录证实进展性mCRPC…",
                    "子条件": "软组织进展",
                    "逻辑关系": "OR分支（同组：IN-5-OR）",
                    "可从病例获取": True,
                    "或组": "IN-5-OR",
                    "或组语义": "任一满足即整条满足",
                    "转化条件": {"匹配字段": "影像学", "同义词": ["靶病灶"], "证据位置": "影像报告"},
                },
            },
            "入选_不可从病例获取": {},
        }
    }
    out = tmp_path / "criteria_judge_IN.json"
    assert (
        pack.main(
            [
                "slim",
                "--criteria",
                str(_write_json(tmp_path / "criteria_parsed_IN.json", criteria)),
                "--qc",
                str(_write_json(tmp_path / "criteria_qc_IN.json", QC_PASSED)),
                "--track",
                "IN",
                "--out",
                str(out),
            ]
        )
        == 0
    )
    by_id = json.loads(out.read_text(encoding="utf-8"))["四分类"]["入选_可从病例获取"]
    assert by_id["IN-5-1"]["或组"] == "IN-5-OR"
    assert by_id["IN-5-1"]["或组语义"] == "任一满足即整条满足"
    assert by_id["IN-5-2"]["或组"] == "IN-5-OR"


def test_cli_slim_roundtrip(tmp_path: Path):
    """CLI 端到端:QC 闸过 → slim 产出包;合并半边在判定域 test_judge_pack。"""
    criteria_path = _write_json(tmp_path / "criteria_parsed_IN.json", IN_CRITERIA)
    qc_path = _write_json(tmp_path / "criteria_qc_IN.json", QC_PASSED)
    out_pack = tmp_path / "criteria_judge_IN.json"

    assert pack.main(["slim", "--criteria", str(criteria_path), "--qc", str(qc_path), "--track", "IN", "--out", str(out_pack)]) == 0
    in_pack = json.loads(out_pack.read_text(encoding="utf-8"))
    assert in_pack["分片"] == "入选"


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


# ───── 全量报告：`status` 区分 open/fixed（会话 `c80c47d9` 的不收敛）─────
#
# QC 改为**全量报告**后，已修的问题带 `status: "fixed"` **留在** `blocking_issues` 里以便
# 追溯，不再从报告中消失。收敛判据随之改为「无 `status=open` 的 blocking」。
#
# 旧判据（`blocking_issues` 非空即未收敛）会把「全部已修」的报告永久判为未收敛 —— 这是
# 计划里点名的跨文件契约：改了 QC schema 不改这里，闸会卡死整个流程。


def test_slim_allowed_when_all_blocking_are_fixed(tmp_path: Path):
    """全量报告：条目留在 blocking_issues 但全部 status=fixed → 已收敛。"""
    qc = {
        "passed": True,
        "round": 2,
        "coverage": {"total_entities": 2, "reviewed": 2},
        "blocking_issues": [
            {"id": "CQC-R1-001", "condition_id": "IN-3", "status": "fixed", "first_seen_round": 1},
            {"id": "CQC-R1-002", "condition_id": "IN-4", "status": "fixed", "first_seen_round": 1},
        ],
    }
    assert _slim_cli(tmp_path, "IN", IN_CRITERIA, qc) == 0
    assert (tmp_path / "criteria_judge_IN.json").exists()


def test_slim_blocked_when_any_blocking_still_open(tmp_path: Path):
    """一条 open 就必须拦住，即使其余全 fixed 且 passed 被写成 true。"""
    qc = {
        "passed": True,
        "round": 2,
        "coverage": {"total_entities": 2, "reviewed": 2},
        "blocking_issues": [
            {"id": "CQC-R1-001", "condition_id": "IN-3", "status": "fixed", "first_seen_round": 1},
            {"id": "CQC-R2-001", "condition_id": "IN-4", "status": "open", "first_seen_round": 2},
        ],
    }
    assert _slim_cli(tmp_path, "IN", IN_CRITERIA, qc) == 2
    assert _no_pack_written(tmp_path, "IN")


def test_blocking_without_status_is_still_treated_as_open(tmp_path: Path):
    """向后兼容：旧报告没有 `status` 字段，必须仍按未收敛处理（不得因缺字段被放行）。"""
    qc = {"passed": True, "round": 2, "blocking_issues": [{"condition_id": "IN-3"}]}
    assert _slim_cli(tmp_path, "IN", IN_CRITERIA, qc) == 2
    assert _no_pack_written(tmp_path, "IN")


def test_coverage_shortfall_blocks_even_when_nothing_is_open(tmp_path: Path):
    """`reviewed < total_entities` → 本轮没看全，结论不可信，按未收敛拦住。"""
    qc = {
        "passed": True,
        "round": 2,
        "coverage": {"total_entities": 42, "reviewed": 8},
        "blocking_issues": [{"id": "CQC-R1-001", "condition_id": "IN-3", "status": "fixed"}],
    }
    assert _slim_cli(tmp_path, "IN", IN_CRITERIA, qc) == 2
    assert _no_pack_written(tmp_path, "IN")


def test_full_coverage_is_not_required_when_absent(tmp_path: Path):
    """`coverage` 缺失（旧报告）不得因此阻断 —— 否则历史 workspace 全部卡死。"""
    qc = {"passed": True, "round": 1, "blocking_issues": []}
    assert _slim_cli(tmp_path, "IN", IN_CRITERIA, qc) == 0


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
    empty_track = {"四分类": {"入选_可从病例获取": {}, "入选_不可从病例获取": {}}}
    assert _slim_cli(tmp_path, "IN", empty_track, QC_PASSED) == 2
    assert _no_pack_written(tmp_path, "IN")


def test_slim_allowed_when_qc_converged_and_structure_clean(tmp_path: Path):
    assert _slim_cli(tmp_path, "EX", EX_CRITERIA, QC_PASSED) == 0
    ex_pack = json.loads((tmp_path / "criteria_judge_EX.json").read_text(encoding="utf-8"))
    assert ex_pack["条件数"] == 2


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


def test_cli_slim_and_merge_roundtrip(tmp_path: Path):
    criteria_path = _write_json(tmp_path / "criteria_parsed_IN.json", IN_CRITERIA)
    qc_path = _write_json(tmp_path / "criteria_qc_IN.json", QC_PASSED)
    out_pack = tmp_path / "criteria_judge_IN.json"

    assert pack.main(["slim", "--criteria", str(criteria_path), "--qc", str(qc_path), "--track", "IN", "--out", str(out_pack)]) == 0
    in_pack = json.loads(out_pack.read_text(encoding="utf-8"))
    assert in_pack["分片"] == "入选"

    in_pack = json.loads(out_pack.read_text(encoding="utf-8"))
    assert in_pack["分片"] == "入选"


def test_slim_pack_is_accepted_by_direction_and_recheck_gates(tmp_path: Path):
    """精简包必须能被两个机械闸直接消费（回归：切分破坏 四分类 结构会让闸静默失效）。"""
    direction_path = REPO_ROOT / "skills" / "custom" / "eligibility-judgment" / "scripts" / "exclusion_direction_check.py"
    spec = importlib.util.spec_from_file_location("edc", direction_path)
    edc = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = edc
    spec.loader.exec_module(edc)

    ex_pack = pack.slim_track(EX_CRITERIA, "EX")
    judgments = {
        "patient_id": "P1",
        "judgments": {"EX-15": {"conclusion": "不符合", "reason": "HBsAg 阴性，未见活动性肝炎。"}},
    }
    result = edc.check(judgments, ex_pack)
    assert result["conflicts"] == ["EX-15"]

    # uncertain_recheck 同样直接吃精简包：IN 轨「无法判断」条目 + OCR 命中 → suspected_missed
    recheck_path = REPO_ROOT / "skills" / "custom" / "eligibility-judgment" / "scripts" / "uncertain_recheck.py"
    spec = importlib.util.spec_from_file_location("ucr", recheck_path)
    ucr = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = ucr
    spec.loader.exec_module(ucr)

    in_pack = pack.slim_track(IN_CRITERIA, "IN")
    ocr = tmp_path / "ocr_records.md"
    ocr.write_text("来源图片：/x/筛选期病历_page_001.jpg\n患者年龄 72 岁，男性。\n", encoding="utf-8")
    in_judgments = {
        "patient_id": "P1",
        "judgments": {"IN-2-1": {"conclusion": "无法判断", "reason": "未提及年龄。"}},
    }
    recheck = ucr.recheck(in_pack, in_judgments, [ocr])
    assert recheck["suspected_missed"] == ["IN-2-1"], "精简包应保留 同义词，使兜底闸能命中 OCR 原文"


# --- merge-judgments --criteria：或组的权威来源是标准包 ----------------------
#
# 会话 `d1883294` 的真实故障：判定子代理落盘的条目只有
# `conclusion / reason / evidence / matching`，**没有 `或组`**。当时 merge_judgments
# 只把 judgments 传给 rollup，13 个或组全部退化成 `AND`，IN-7（IN-7-1 无法判断 /
# IN-7-2 符合）被折叠成「无法判断」，正确答案是「符合」—— 且全程零告警。
#
# ⚠️ 上方 ROLLUP_IN_SHARD / ROLLUP_EX_SHARD 夹具在 judgments 条目里塞了 `或组`，
# 恰好补上了真实数据缺的那一块。那批用例验证的是「没传包时读条目」的兼容路径，
# ⛔ 不要用它们验证本节的权威来源行为。

# 真实形态的判定分片：条目里没有任何结构字段
