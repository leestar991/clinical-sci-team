"""write_phase2_summary 回归测试:P2 收尾 summary 的机械写盘。

SOUL 曾要求主代理手写 phase2_summary.json 的 20 行字段清单(路径/QC 状态合取/
四分类计数/ocr_results),两类历史故障:先写占位 stub、字段手写错(patient_mode
无法从 ocr_route 推导——模式2/3 都是 B)。字段实证基准:f9231297 的产物。
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "skills" / "custom" / "criteria-parser" / "scripts" / "write_phase2_summary.py"

if not SCRIPT_PATH.exists():  # skills/custom 为 gitignore 目录，清空检出时跳过
    pytest.skip("criteria-parser 技能未安装", allow_module_level=True)


def _load_module():
    spec = importlib.util.spec_from_file_location("write_phase2_summary", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


wps = _load_module()

QC_PASS = {"passed": True, "round": 2, "blocking_issues": [], "residual_issues": []}
QC_BLOCKED = {"passed": False, "round": 3, "blocking_issues": [{"id": "X", "condition_id": "IN-3", "status": "open"}]}


@pytest.fixture()
def ws(tmp_path: Path) -> Path:
    for tr in ("IN", "EX"):
        cats = {"四分类": {f"{('入选' if tr == 'IN' else '排除')}_可从病例获取": {"IN-1": {}}, f"{('入选' if tr == 'IN' else '排除')}_不可从病例获取": {}}, "描述索引": {}}
        (tmp_path / f"criteria_parsed_{tr}.json").write_text(json.dumps(cats, ensure_ascii=False), encoding="utf-8")
        (tmp_path / f"criteria_qc_{tr}.json").write_text(json.dumps(QC_PASS, ensure_ascii=False), encoding="utf-8")
        (tmp_path / f"criteria_judge_{tr}.json").write_text("{}", encoding="utf-8")  # summary 是收尾最后一步,slim 已跑
    (tmp_path / "criteria_parsed.json").write_text("{}", encoding="utf-8")  # assemble 产物
    (tmp_path / "criteria_meta.json").write_text("{}", encoding="utf-8")
    return tmp_path


def _run(ws: Path, *extra: str) -> int:
    return wps.main(["--workspace", str(ws), *extra])


def _summary(ws: Path) -> dict:
    return json.loads((ws / "phase2_summary.json").read_text(encoding="utf-8"))


def test_paths_and_qc_passed_computed_from_artifacts(ws: Path):
    """路径类字段指向 workspace 产物;criteria_qc_passed 是两轨合取,非手填。"""
    assert _run(ws, "--patient-mode", "single_paged") == 0
    s = _summary(ws)
    for key in ("criteria_meta", "criteria_parsed", "criteria_parsed_IN", "criteria_parsed_EX",
                "criteria_judge_IN", "criteria_judge_EX"):
        assert s[key] == f"/mnt/user-data/workspace/{key if key != 'criteria_meta' else 'criteria_meta'}.json" or s[key].endswith(f"{key}.json")
    assert s["criteria_qc"] == {"IN": "/mnt/user-data/workspace/criteria_qc_IN.json",
                                "EX": "/mnt/user-data/workspace/criteria_qc_EX.json"}
    assert s["criteria_qc_passed"] is True
    assert s["patient_mode"] == "single_paged"


def test_criteria_count_from_parsed_files(ws: Path):
    assert _run(ws, "--patient-mode", "single_paged") == 0
    counts = _summary(ws)["criteria_count"]
    assert counts["入选_可从病例获取"] == 1 and counts["排除_可从病例获取"] == 1
    assert counts["入选_不可从病例获取"] == 0


def test_one_track_blocked_blocks_passed_and_status(ws: Path):
    """任一轨 passed=false → 合取 false + 状态 blocked/未通过;⛔ 禁止只看单轨。"""
    (ws / "criteria_qc_EX.json").write_text(json.dumps(QC_BLOCKED, ensure_ascii=False), encoding="utf-8")
    assert _run(ws, "--patient-mode", "single_paged") == 0
    s = _summary(ws)
    assert s["criteria_qc_passed"] is False
    assert s["criteria_qc_status"] == "blocked_round_limit"


def test_round_limit_requires_passed_false_in_qc_report(ws: Path):
    """QC 报告自标 blocked_round_limit 但 passed=true 的自相矛盾形态 → 拒绝写盘。"""
    bad = {**QC_PASS, "criteria_qc_status": "blocked_round_limit"}
    (ws / "criteria_qc_IN.json").write_text(json.dumps(bad, ensure_ascii=False), encoding="utf-8")
    assert _run(ws, "--patient-mode", "single_paged") == 2
    assert not (ws / "phase2_summary.json").exists()


def test_patient_mode_required_and_validated(ws: Path):
    """patient_mode 无法从 ocr_route 机械推导(模式2/3 均 B)→ 必填枚举;落盘键可省参数。"""
    assert _run(ws) == 2
    assert not (ws / "phase2_summary.json").exists()
    assert _run(ws, "--patient-mode", "mixed_paged") == 0
    assert _summary(ws)["patient_mode"] == "mixed_paged"
    assert _run(ws, "--patient-mode", "whatever") == 2


def test_patient_mode_can_come_from_classification(ws: Path):
    """pdf_classification.json 落盘 patient_mode 后参数可省(单一来源优先产物)。"""
    (ws / "pdf_classification.json").write_text(
        json.dumps({"patient_mode": "single_whole"}, ensure_ascii=False), encoding="utf-8")
    assert _run(ws) == 0
    assert _summary(ws)["patient_mode"] == "single_whole"


def test_ocr_results_route_a_and_b(ws: Path):
    (ws / "pdf_classification.json").write_text(json.dumps({
        "patient_mode": "single_whole",
        "files": [
            {"pdf": "方案.pdf", "source_name": "筛选期病历", "ocr_route": "A"},
            {"pdf": "检查.pdf", "source_name": "筛选期检查", "ocr_route": "B"},
        ],
    }, ensure_ascii=False), encoding="utf-8")
    ocr_dir = ws / "ocr" / "筛选期检查"
    ocr_dir.mkdir(parents=True)
    (ocr_dir / "p1.md").write_text("x", encoding="utf-8")
    (ocr_dir / "p2.md").write_text("x", encoding="utf-8")
    (ws / "ocr" / "筛选期病历" / "病历_full.md").parent.mkdir(parents=True, exist_ok=True)
    (ws / "ocr" / "筛选期病历" / "病历_full.md").write_text("x", encoding="utf-8")
    assert _run(ws) == 0
    results = {r["source"]: r for r in _summary(ws)["ocr_results"]}
    assert results["筛选期病历"]["ocr_route"] == "A" and results["筛选期病历"]["ocr_file"].endswith("病历_full.md")
    assert results["筛选期检查"]["ocr_route"] == "B"
    assert results["筛选期检查"]["ocr_md_count"] == 2


def test_missing_required_artifact_exits_2(ws: Path):
    (ws / "criteria_qc_EX.json").unlink()
    assert _run(ws, "--patient-mode", "single_paged") == 2
    assert not (ws / "phase2_summary.json").exists()
