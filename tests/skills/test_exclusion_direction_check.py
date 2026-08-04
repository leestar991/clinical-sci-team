"""exclusion_direction_check 回归测试。

历史故障（患者 M016_ZALO）：EX-10/EX-12/EX-15/EX-16 的 reason 语义均为"排除条件
未触发"，conclusion 却写成 `不符合`（按技能定义=被触发/应排除），方向整体反转，
且 QC 未拦住。本测试锁定该机械校验闸的行为。
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "skills" / "custom" / "eligibility-judgment" / "scripts" / "exclusion_direction_check.py"

if not SCRIPT_PATH.exists():  # skills/custom 为 gitignore 目录，清空检出时跳过
    pytest.skip("eligibility-judgment 技能未安装", allow_module_level=True)


def _load_module():
    spec = importlib.util.spec_from_file_location("exclusion_direction_check", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


checker = _load_module()


def _judgments(judgments: dict, patient_id: str = "M016_ZALO") -> dict:
    return {
        "patient_id": patient_id,
        "documents": {"doc": {"doc_label": "病历", "judgments": judgments}},
    }


def _entry(result: dict, cid: str) -> dict:
    return next(e for e in result["entries"] if e["条件ID"] == cid)


# --- M016 真实故障用例：四条方向反转 ---------------------------------------

M016_INVERTED = {
    "EX-10": {
        "conclusion": "不符合",
        "reason": "现有资料未见活动性感染诊断，感染筛查亦未提示HIV/HCV/HBV等活动性病毒感染证据，病程摘要未提及活动性结核或需全身治疗的细菌/真菌感染。",
    },
    "EX-12": {
        "conclusion": "不符合",
        "reason": "当前超声提示膀胱充盈良好、壁光滑，病历未描述难以控制的膀胱流出道梗阻或尿失禁。",
    },
    "EX-15": {
        "conclusion": "不符合",
        "reason": "梅毒、HIV、HCV抗体及HBsAg筛查均为阴性，当前无活动性乙肝/丙肝/梅毒/HIV证据。",
    },
    "EX-16": {
        "conclusion": "不符合",
        "reason": "患者已完成18F-PSMA显像，现有资料未提示PETCT/SPECT-CT禁忌或严重图像采集/判读问题。",
    },
}


def test_m016_four_inverted_exclusions_are_blocking():
    result = checker.check(_judgments(M016_INVERTED))
    assert result["checked"] == 4
    assert result["conflicts"] == ["EX-10", "EX-12", "EX-15", "EX-16"]
    for cid in M016_INVERTED:
        entry = _entry(result, cid)
        assert entry["severity"] == "阻断级"
        assert entry["issue"] == "suspected_inversion"
        assert entry["direction_in_reason"] == "未触发"
        assert entry["expected_conclusion"] == "符合"


def test_negated_positive_words_do_not_flip_direction():
    """ "未见活动性感染"含"活动性"，不得被算作肯定证据而抵消否定信号。"""
    direction, basis, hits = checker.detect_direction("现有资料未见活动性感染诊断，HBsAg 阴性。")
    assert direction == "未触发"
    assert basis == "evidence"
    assert hits["positive"] == []


# --- 正确写法不应被误报 -----------------------------------------------------


def test_explicit_not_triggered_with_met_conclusion_passes():
    result = checker.check(
        _judgments(
            {
                "EX-15": {
                    "conclusion": "符合",
                    "reason": "HBsAg、HCV抗体、HIV抗体、梅毒均阴性，未触发该排除条件，患者可入选。",
                }
            }
        )
    )
    assert result["conflicts"] == []
    assert result["advisories"] == []
    assert _entry(result, "EX-15")["issue"] == "ok"


def test_explicit_triggered_with_not_met_conclusion_passes():
    result = checker.check(
        _judgments(
            {
                "EX-5": {
                    "conclusion": "不符合",
                    "reason": "2023年确诊结肠癌并接受根治术，距筛选不足5年，触发排除条件，应排除。",
                }
            }
        )
    )
    assert result["conflicts"] == []
    assert _entry(result, "EX-5")["issue"] == "ok"


def test_not_被触发_is_not_matched_as_被触发():
    direction, basis, _ = checker.detect_direction("检验结果均正常，该排除条件未被触发。")
    assert (direction, basis) == ("未触发", "explicit")


# --- 显式方向短语与 conclusion 冲突 → 阻断级 -------------------------------


def test_explicit_direction_conflict_is_blocking():
    result = checker.check(
        _judgments(
            {
                "EX-9": {
                    "conclusion": "不符合",
                    "reason": "心电图正常，QTcF 414 ms，未触发该排除条件。",
                }
            }
        )
    )
    assert result["conflicts"] == ["EX-9"]
    assert _entry(result, "EX-9")["issue"] == "direction_conflict"


def test_exclusion_triggered_field_conflict_is_blocking():
    result = checker.check(
        _judgments(
            {
                "EX-2": {
                    "conclusion": "不符合",
                    "reason": "既往未接受任何PSMA靶向放射性配体治疗，未触发排除条件。",
                    "exclusion_triggered": False,
                }
            }
        )
    )
    assert result["conflicts"] == ["EX-2"]
    assert _entry(result, "EX-2")["issue"] == "field_conflict"


def test_field_and_conclusion_aligned_passes():
    result = checker.check(
        _judgments(
            {
                "EX-2": {
                    "conclusion": "符合",
                    "reason": "既往未接受任何PSMA靶向放射性配体治疗，未触发排除条件。",
                    "exclusion_triggered": False,
                }
            }
        )
    )
    assert result["conflicts"] == []
    assert _entry(result, "EX-2")["exclusion_triggered"] is False


# --- 建议级 ----------------------------------------------------------------


def test_direction_undeclared_is_advisory():
    result = checker.check(_judgments({"EX-7": {"conclusion": "符合", "reason": "头颅MRI报告已复核。"}}))
    assert result["conflicts"] == []
    assert result["advisories"] == ["EX-7"]
    assert _entry(result, "EX-7")["issue"] == "direction_undeclared"


def test_met_conclusion_with_positive_evidence_only_is_advisory():
    result = checker.check(
        _judgments(
            {
                "EX-13": {
                    "conclusion": "符合",
                    "reason": "既往病史明确记载类风湿关节炎，正在接受甲氨蝶呤治疗。",
                }
            }
        )
    )
    assert result["conflicts"] == []
    assert _entry(result, "EX-13")["issue"] == "suspected_inversion_weak"
    assert _entry(result, "EX-13")["severity"] == "建议级"


# --- 作用域 ----------------------------------------------------------------


def test_inclusion_items_and_nondirectional_conclusions_are_skipped():
    result = checker.check(
        _judgments(
            {
                "IN-2-1": {"conclusion": "不符合", "reason": "年龄16岁，未达18周岁。"},
                "EX-4": {"conclusion": "存疑", "reason": "缺少末次用药日期，无法核实洗脱期。"},
                "EX-1": {"conclusion": "无法判断", "reason": "已查入院记录，缺完整过敏史。"},
            }
        )
    )
    assert result["checked"] == 0
    assert result["conflicts"] == []


def test_criteria_categories_drive_exclusion_scope():
    """条件ID 不以 EX 开头时，按 criteria_parsed.json 的排除类目识别。"""
    criteria = {
        "四分类": {
            "可从病例获取-入选": [{"条件ID": "C-01"}],
            "可从病例获取-排除": [{"条件ID": "C-02"}],
        }
    }
    result = checker.check(
        _judgments(
            {
                "C-01": {"conclusion": "符合", "reason": "年龄72岁，满足≥18周岁。"},
                "C-02": {"conclusion": "不符合", "reason": "HBsAg 阴性，未见活动性肝炎证据。"},
            }
        ),
        criteria,
    )
    assert result["checked"] == 1
    assert result["conflicts"] == ["C-02"]


# --- CLI ------------------------------------------------------------------


def test_cli_writes_report_and_exits_zero(tmp_path: Path):
    judgments_path = tmp_path / "judgments_draft.json"
    judgments_path.write_text(json.dumps(_judgments(M016_INVERTED), ensure_ascii=False), encoding="utf-8")
    out_path = tmp_path / "nested" / "exclusion_direction_check.json"

    code = checker.main(["--judgments", str(judgments_path), "--out", str(out_path)])

    assert code == 0
    report = json.loads(out_path.read_text(encoding="utf-8"))
    assert report["patient_id"] == "M016_ZALO"
    assert report["conflicts"] == ["EX-10", "EX-12", "EX-15", "EX-16"]
