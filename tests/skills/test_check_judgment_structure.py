"""check_judgment_structure 回归测试：单患者单轨判定结构闸（QC 前置 + 改判后守恒）。

保证判定/改判的静默事故被机械拦住，而不是靠模型自觉。核心回归：
- 患者 M016_ZALO 排除项方向反转（EX-10/12/15/16 reason 说"未见/阴性"却判 `不符合`）→ 闸4/闸6
- 改判两类静默事故：**无操作改判**（点名却没改）与**连带误伤**（没点名却改了）→ 闸8
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "skills" / "custom" / "eligibility-judgment" / "scripts" / "check_judgment_structure.py"

if not SCRIPT_PATH.exists():  # skills/custom 为 gitignore 目录，清空检出时跳过
    pytest.skip("eligibility-judgment 技能未安装", allow_module_level=True)


def _load_module():
    spec = importlib.util.spec_from_file_location("check_judgment_structure", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


cjs = _load_module()

PATIENT = "M016_ZALO"


# ────────────────────────────── 夹具 ──────────────────────────────


def pdir(ws: Path) -> Path:
    d = ws / "patients" / PATIENT
    d.mkdir(parents=True, exist_ok=True)
    return d


def dump(path: Path, payload) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def write_pack(ws: Path, track: str, cids: list[str]):
    cat = "排除_可从病例获取" if track == "EX" else "入选_可从病例获取"
    dump(ws / f"criteria_judge_{track}.json", {"条件数": len(cids), "四分类": {cat: [{"条件ID": c} for c in cids]}})


def ex_entry(concl: str, reason: str = "r", *, trig: bool | None = None):
    entry: dict = {"conclusion": concl, "reason": reason, "evidence": [{"page": 1, "quote": "q"}]}
    if trig is None and concl in ("符合", "不符合"):
        trig = concl == "不符合"
    if trig is not None:
        entry["exclusion_triggered"] = trig
    return entry


def write_judgments(ws: Path, track: str, judgments: dict, *, doc: str = "medical_record", stage: str = "draft"):
    counts = dict.fromkeys(("符合", "不符合", "存疑", "无法判断"), 0)
    for e in judgments.values():
        if isinstance(e, dict) and e.get("conclusion") in counts:
            counts[e["conclusion"]] += 1
    stem = "judgments_draft" if stage == "draft" else "judgments"
    return dump(
        pdir(ws) / f"{stem}_{PATIENT}_{track}.json",
        {"patient_id": PATIENT, "documents": {doc: {"judgments": judgments, "summary": counts}}},
    )


def write_gates(ws: Path, track: str, *, missed: list[str] | None = None, conflicts: list[str] | None = None):
    dump(pdir(ws) / f"uncertain_recheck_{PATIENT}_{track}.json", {"suspected_missed": missed or []})
    if track == "EX":
        dump(
            pdir(ws) / f"exclusion_direction_check_{PATIENT}_EX.json",
            {"conflicts": conflicts or [], "advisories": []},
        )


def write_phase2(ws: Path, sources: list[str]):
    return dump(ws / "phase2_summary.json", {"ocr_results": [{"source": s} for s in sources]})


def write_qc(ws: Path, track: str, cids: list[str]) -> Path:
    return dump(
        ws / "outputs" / f"qc_report_{PATIENT}_{track}.json",
        {
            "patient_id": PATIENT,
            "track": track,
            "passed": not cids,
            "blocking_issues": [{"id": f"QC-{i:03d}", "condition_id": c} for i, c in enumerate(cids)],
        },
    )


def probs(ws: Path, track: str, *, qc: Path | None = None, snapshot: bool = False, stage: str = "draft") -> list[str]:
    return cjs.check(ws, PATIENT, track, stage, qc, snapshot)["problems"]


def clean_ex(ws: Path, cids: list[str]) -> dict:
    """一份全过闸的 EX 轨判定。"""
    write_pack(ws, "EX", cids)
    judgments = {c: ex_entry("符合", "未触发该排除条件") for c in cids}
    write_judgments(ws, "EX", judgments)
    write_gates(ws, "EX")
    return judgments


# ─────────────────── 闸 1：顶层结构 ───────────────────


def test_gate1_missing_file(tmp_path):
    assert any("闸1" in p and "文件不存在" in p for p in probs(tmp_path, "EX"))


def test_gate1_invalid_json(tmp_path):
    (pdir(tmp_path) / f"judgments_draft_{PATIENT}_EX.json").write_text("{坏", encoding="utf-8")
    assert any("闸1" in p and "JSON 不合法" in p for p in probs(tmp_path, "EX"))


def test_gate1_missing_documents(tmp_path):
    dump(pdir(tmp_path) / f"judgments_draft_{PATIENT}_EX.json", {"patient_id": PATIENT})
    assert any("缺少非空 `documents`" in p for p in probs(tmp_path, "EX"))


def test_gate1_all_judgments_empty(tmp_path):
    dump(
        pdir(tmp_path) / f"judgments_draft_{PATIENT}_EX.json",
        {"patient_id": PATIENT, "documents": {"a": {"judgments": {}, "summary": {}}}},
    )
    assert any("判定未产出" in p for p in probs(tmp_path, "EX"))


# ─────────────────── 闸 2：条件ID 覆盖恒等于标准包 ───────────────────


def test_gate2_detects_missing_condition(tmp_path):
    """改判不得删条目——判定条目数恒等于标准包条件数。"""
    write_pack(tmp_path, "EX", ["EX-1", "EX-2", "EX-3"])
    write_judgments(tmp_path, "EX", {c: ex_entry("符合") for c in ("EX-1", "EX-2")})
    write_gates(tmp_path, "EX")
    assert any("闸2" in p and "缺失条件ID：['EX-3']" in p for p in probs(tmp_path, "EX"))


def test_gate2_detects_extra_condition(tmp_path):
    write_pack(tmp_path, "EX", ["EX-1"])
    write_judgments(tmp_path, "EX", {"EX-1": ex_entry("符合"), "IN-5": ex_entry("符合")})
    write_gates(tmp_path, "EX")
    assert any("标准包外条件ID：['IN-5']" in p and "跨轨污染" in p for p in probs(tmp_path, "EX"))


def test_gate2_skipped_without_pack(tmp_path):
    write_judgments(tmp_path, "EX", {"EX-1": ex_entry("符合")})
    write_gates(tmp_path, "EX")
    r = cjs.check(tmp_path, PATIENT, "EX", "draft", None, False)
    assert r["problems"] == []
    assert any("闸2 跳过" in n for n in r["notes"])


def test_gate2_notes_declared_count_mismatch(tmp_path):
    dump(tmp_path / "criteria_judge_EX.json", {"条件数": 99, "四分类": {"排除_可从病例获取": [{"条件ID": "EX-1"}]}})
    write_judgments(tmp_path, "EX", {"EX-1": ex_entry("符合")})
    write_gates(tmp_path, "EX")
    r = cjs.check(tmp_path, PATIENT, "EX", "draft", None, False)
    assert any("`条件数`=99" in n for n in r["notes"])
    assert r["problems"] == []  # 以实际为准，不阻断


# ─────────────────── 闸 3：结论枚举 ───────────────────


@pytest.mark.parametrize("bad", ["部分符合", "需补充信息", "", None, "PASS"])
def test_gate3_rejects_illegal_conclusion(tmp_path, bad):
    write_pack(tmp_path, "IN", ["IN-1"])
    write_judgments(tmp_path, "IN", {"IN-1": {"conclusion": bad, "reason": "r"}})
    write_gates(tmp_path, "IN")
    assert any("闸3" in p and "conclusion 非法" in p for p in probs(tmp_path, "IN"))


def test_gate3_rejects_non_object_entry(tmp_path):
    write_pack(tmp_path, "IN", ["IN-1"])
    write_judgments(tmp_path, "IN", {"IN-1": "符合"})
    write_gates(tmp_path, "IN")
    assert any("不是对象" in p for p in probs(tmp_path, "IN"))


# ─────────────────── 闸 4：EX 轨方向字段一致 ───────────────────


def test_gate4_detects_direction_contradiction(tmp_path):
    """符合 ⇔ exclusion_triggered=false；写反即方向自相矛盾。"""
    write_pack(tmp_path, "EX", ["EX-1"])
    write_judgments(tmp_path, "EX", {"EX-1": ex_entry("符合", trig=True)})
    write_gates(tmp_path, "EX")
    assert any("闸4" in p and "方向自相矛盾" in p for p in probs(tmp_path, "EX"))


def test_gate4_detects_missing_flag(tmp_path):
    write_pack(tmp_path, "EX", ["EX-1"])
    write_judgments(tmp_path, "EX", {"EX-1": {"conclusion": "不符合", "reason": "r"}})
    write_gates(tmp_path, "EX")
    assert any("缺 `exclusion_triggered`" in p for p in probs(tmp_path, "EX"))


@pytest.mark.parametrize("concl", ["存疑", "无法判断"])
def test_gate4_flag_not_required_for_uncertain(tmp_path, concl):
    """存疑/无法判断不要求方向字段（约束：证据不足时不得用不符合表达）。"""
    write_pack(tmp_path, "EX", ["EX-1"])
    write_judgments(tmp_path, "EX", {"EX-1": {"conclusion": concl, "reason": "r"}})
    write_gates(tmp_path, "EX")
    assert probs(tmp_path, "EX") == []


def test_gate4_not_applied_to_in_track(tmp_path):
    """IN 轨没有方向字段概念，不得误报。"""
    write_pack(tmp_path, "IN", ["IN-1"])
    write_judgments(tmp_path, "IN", {"IN-1": {"conclusion": "不符合", "reason": "r"}})
    write_gates(tmp_path, "IN")
    assert probs(tmp_path, "IN") == []


# ─────────────────── 闸 5：summary 自洽 ───────────────────


def test_gate5_detects_summary_mismatch(tmp_path):
    write_pack(tmp_path, "IN", ["IN-1"])
    dump(
        pdir(tmp_path) / f"judgments_draft_{PATIENT}_IN.json",
        {
            "patient_id": PATIENT,
            "documents": {
                "rec": {
                    "judgments": {"IN-1": {"conclusion": "符合", "reason": "r"}},
                    "summary": {"符合": 9, "不符合": 9, "存疑": 0, "无法判断": 0},
                }
            },
        },
    )
    write_gates(tmp_path, "IN")
    assert any("闸5" in p and "summary 与实际不符" in p for p in probs(tmp_path, "IN"))


# ─────────────────── 闸 6：机械闸产物已清空 ───────────────────


def test_gate6_detects_unresolved_missed(tmp_path):
    clean_ex(tmp_path, ["EX-1"])
    write_gates(tmp_path, "EX", missed=["EX-1"])
    assert any("闸6 疑似漏判未清空：['EX-1']" in p for p in probs(tmp_path, "EX"))


def test_gate6_detects_unresolved_direction_conflicts(tmp_path):
    """真实故障 M016_ZALO：EX-10/12/15/16 方向反转。"""
    cids = [f"EX-{n}" for n in range(1, 17)]
    clean_ex(tmp_path, cids)
    write_gates(tmp_path, "EX", conflicts=["EX-10", "EX-12", "EX-15", "EX-16"])
    hits = [p for p in probs(tmp_path, "EX") if "闸6 排除项方向冲突未清空" in p]
    assert hits and "EX-16" in hits[0]


def test_gate6_requires_recheck_artifact(tmp_path):
    write_pack(tmp_path, "IN", ["IN-1"])
    write_judgments(tmp_path, "IN", {"IN-1": {"conclusion": "符合", "reason": "r"}})
    assert any("闸6 漏判反查产物缺失" in p for p in probs(tmp_path, "IN"))


def test_gate6_requires_direction_artifact_for_ex_only(tmp_path):
    write_pack(tmp_path, "EX", ["EX-1"])
    write_judgments(tmp_path, "EX", {"EX-1": ex_entry("符合")})
    dump(pdir(tmp_path) / f"uncertain_recheck_{PATIENT}_EX.json", {"suspected_missed": []})
    assert any("闸6 方向校验产物缺失" in p for p in probs(tmp_path, "EX"))


# ─────────────────── 闸 7：QC 目标条目存在 ───────────────────


def test_gate7_detects_dropped_qc_target(tmp_path):
    write_pack(tmp_path, "EX", ["EX-1", "EX-2"])
    write_judgments(tmp_path, "EX", {c: ex_entry("符合") for c in ("EX-1", "EX-2")})
    write_gates(tmp_path, "EX")
    qc = write_qc(tmp_path, "EX", ["EX-9"])  # QC 点名了一个不存在的条目
    assert any("闸7" in p and "改判后不存在：['EX-9']" in p for p in probs(tmp_path, "EX", qc=qc))


def test_gate7_flags_missing_qc_report(tmp_path):
    clean_ex(tmp_path, ["EX-1"])
    assert any("闸7" in p and "文件不存在" in p for p in probs(tmp_path, "EX", qc=tmp_path / "nope.json"))


# ─────────────────── 闸 8：改判守恒 ───────────────────


def test_gate8_detects_noop_repair(tmp_path):
    """QC 点名 EX-2 却三字段全未动 → 无操作改判。"""
    clean_ex(tmp_path, ["EX-1", "EX-2"])
    cjs.check(tmp_path, PATIENT, "EX", "draft", None, True)  # 基线
    qc = write_qc(tmp_path, "EX", ["EX-2"])
    hits = [p for p in probs(tmp_path, "EX", qc=qc) if "闸8" in p and "无操作改判" in p]
    assert hits and "EX-2" in hits[0]


def test_gate8_detects_collateral_damage(tmp_path):
    """QC 只点名 EX-1，EX-2 的结论却被连带改掉 → 误伤。"""
    clean_ex(tmp_path, ["EX-1", "EX-2"])
    cjs.check(tmp_path, PATIENT, "EX", "draft", None, True)
    write_judgments(
        tmp_path,
        "EX",
        {"EX-1": ex_entry("不符合", "触发该排除条件"), "EX-2": ex_entry("不符合", "触发该排除条件")},
    )
    write_gates(tmp_path, "EX")
    qc = write_qc(tmp_path, "EX", ["EX-1"])
    hits = [p for p in probs(tmp_path, "EX", qc=qc) if "闸8" in p and "连带误伤" in p]
    assert hits and "EX-2" in hits[0]


def test_gate8_passes_on_correct_repair(tmp_path):
    """只改 QC 点名的那条，其余不动 → 全过。"""
    clean_ex(tmp_path, ["EX-1", "EX-2"])
    cjs.check(tmp_path, PATIENT, "EX", "draft", None, True)
    write_judgments(
        tmp_path,
        "EX",
        {"EX-1": ex_entry("不符合", "触发该排除条件"), "EX-2": ex_entry("符合", "未触发该排除条件")},
    )
    write_gates(tmp_path, "EX")
    qc = write_qc(tmp_path, "EX", ["EX-1"])
    assert probs(tmp_path, "EX", qc=qc) == []


def test_gate8_reason_only_change_counts_as_repaired(tmp_path):
    """方向校验误报的正确解法是只补 reason 措辞、不改结论——不得被判为无操作。"""
    clean_ex(tmp_path, ["EX-1"])
    cjs.check(tmp_path, PATIENT, "EX", "draft", None, True)
    write_judgments(tmp_path, "EX", {"EX-1": ex_entry("符合", "未见心律失常，未触发该排除条件")})
    write_gates(tmp_path, "EX")
    qc = write_qc(tmp_path, "EX", ["EX-1"])
    assert probs(tmp_path, "EX", qc=qc) == []


def test_gate8_skipped_without_baseline(tmp_path):
    clean_ex(tmp_path, ["EX-1"])
    qc = write_qc(tmp_path, "EX", ["EX-1"])
    r = cjs.check(tmp_path, PATIENT, "EX", "draft", qc, False)
    assert any("闸8 跳过" in n and "改判前应先 --snapshot" in n for n in r["notes"])
    assert r["problems"] == []


def test_snapshot_excludes_evidence_body(tmp_path):
    """基线只记方向三要素，不含证据正文（避免快照膨胀）。"""
    clean_ex(tmp_path, ["EX-1"])
    cjs.check(tmp_path, PATIENT, "EX", "draft", None, True)
    base = json.loads((pdir(tmp_path) / f"judgment_baseline_{PATIENT}_EX.json").read_text(encoding="utf-8"))
    assert set(next(iter(base.values()))) == {"conclusion", "exclusion_triggered", "reason"}


# ─────────────────── 闸 9：document 键等于真实 OCR 来源 ───────────────────


def test_gate9_detects_self_invented_document_key(tmp_path):
    """thread 345f2bf4：IN 轨写 combined_ocr、EX 轨写 screening_bundle，合并成两个假文档。"""
    write_phase2(tmp_path, ["筛选期病历", "筛选期检查"])
    write_pack(tmp_path, "IN", ["IN-1"])
    write_judgments(tmp_path, "IN", {"IN-1": {"conclusion": "符合", "reason": "r"}}, doc="combined_ocr")
    write_gates(tmp_path, "IN")
    hits = [p for p in probs(tmp_path, "IN") if "闸9" in p]
    assert hits and "combined_ocr" in hits[0] and "筛选期病历" in hits[0]


def test_gate9_passes_when_keys_match_sources(tmp_path):
    write_phase2(tmp_path, ["筛选期病历", "筛选期检查"])
    write_pack(tmp_path, "IN", ["IN-1"])
    entry = {"IN-1": {"conclusion": "符合", "reason": "r"}}
    dump(
        pdir(tmp_path) / f"judgments_draft_{PATIENT}_IN.json",
        {
            "patient_id": PATIENT,
            "documents": {d: {"judgments": entry, "summary": {"符合": 1, "不符合": 0, "存疑": 0, "无法判断": 0}} for d in ("筛选期病历", "筛选期检查")},
        },
    )
    write_gates(tmp_path, "IN")
    assert probs(tmp_path, "IN") == []


def test_gate9_detects_partial_key_set(tmp_path):
    """只判了一份来源、漏了另一份，也必须拦住。"""
    write_phase2(tmp_path, ["筛选期病历", "筛选期检查"])
    write_pack(tmp_path, "IN", ["IN-1"])
    write_judgments(tmp_path, "IN", {"IN-1": {"conclusion": "符合", "reason": "r"}}, doc="筛选期病历")
    write_gates(tmp_path, "IN")
    assert any("闸9" in p for p in probs(tmp_path, "IN"))


def test_gate9_skipped_without_phase2_summary(tmp_path):
    write_pack(tmp_path, "IN", ["IN-1"])
    write_judgments(tmp_path, "IN", {"IN-1": {"conclusion": "符合", "reason": "r"}}, doc="whatever")
    write_gates(tmp_path, "IN")
    r = cjs.check(tmp_path, PATIENT, "IN", "draft", None, False)
    assert r["problems"] == []
    assert any("闸9 跳过" in n for n in r["notes"])


def test_gate9_skipped_when_ocr_results_empty(tmp_path):
    write_phase2(tmp_path, [])
    write_pack(tmp_path, "IN", ["IN-1"])
    write_judgments(tmp_path, "IN", {"IN-1": {"conclusion": "符合", "reason": "r"}}, doc="x")
    write_gates(tmp_path, "IN")
    r = cjs.check(tmp_path, PATIENT, "IN", "draft", None, False)
    assert r["problems"] == []
    assert any("闸9 跳过" in n for n in r["notes"])


# ─────────────────── 闸产物（QC / 理由子代理前置自检）───────────────────


def test_gate_artifact_written(tmp_path):
    clean_ex(tmp_path, ["EX-1"])
    assert cjs.main(["--workspace", str(tmp_path), "--patient", PATIENT, "--track", "EX"]) == 0
    art = json.loads((pdir(tmp_path) / f"judgment_structure_gate_{PATIENT}_EX.json").read_text(encoding="utf-8"))
    assert art["exit_code"] == 0
    assert art["checked_file"] == f"judgments_draft_{PATIENT}_EX.json"
    assert len(art["content_sha256_16"]) == 16


def test_gate_artifact_records_failure_and_digest_change(tmp_path):
    clean_ex(tmp_path, ["EX-1"])
    write_gates(tmp_path, "EX", conflicts=["EX-1"])
    cjs.main(["--workspace", str(tmp_path), "--patient", PATIENT, "--track", "EX"])
    first = json.loads((pdir(tmp_path) / f"judgment_structure_gate_{PATIENT}_EX.json").read_text(encoding="utf-8"))
    assert first["exit_code"] == 2 and any("闸6" in p for p in first["problems"])

    clean_ex(tmp_path, ["EX-1", "EX-2"])
    cjs.main(["--workspace", str(tmp_path), "--patient", PATIENT, "--track", "EX"])
    second = json.loads((pdir(tmp_path) / f"judgment_structure_gate_{PATIENT}_EX.json").read_text(encoding="utf-8"))
    assert first["content_sha256_16"] != second["content_sha256_16"]


# ─────────────────── CLI / 退出码 / stage ───────────────────


def test_cli_exit_2_then_0(tmp_path):
    clean_ex(tmp_path, ["EX-1"])
    write_gates(tmp_path, "EX", conflicts=["EX-1"])
    args = ["--workspace", str(tmp_path), "--patient", PATIENT, "--track", "EX"]
    assert cjs.main(args) == 2
    write_gates(tmp_path, "EX")
    assert cjs.main(args) == 0


def test_cli_final_stage_reads_merged_file(tmp_path):
    write_pack(tmp_path, "EX", ["EX-1"])
    write_judgments(tmp_path, "EX", {"EX-1": ex_entry("符合")}, stage="final")
    write_gates(tmp_path, "EX")
    assert probs(tmp_path, "EX", stage="final") == []
    assert any("闸1" in p for p in probs(tmp_path, "EX", stage="draft"))  # draft 不存在


def test_cli_writes_json_report(tmp_path):
    clean_ex(tmp_path, ["EX-1"])
    out = tmp_path / "r.json"
    rc = cjs.main(
        ["--workspace", str(tmp_path), "--patient", PATIENT, "--track", "EX", "--json", str(out)],
    )
    assert rc == 0
    assert json.loads(out.read_text(encoding="utf-8"))["track"] == "EX"


def test_summary_blocks_downstream(tmp_path):
    """未过闸时摘要必须显式禁止派 QC 与进入合并 —— 脚本存在的意义。"""
    clean_ex(tmp_path, ["EX-1"])
    write_gates(tmp_path, "EX", missed=["EX-1"])
    text = cjs.summarize([cjs.check(tmp_path, PATIENT, "EX", "draft", None, False)])
    assert "禁止发 task(quality-control)" in text
    assert "禁止进入合并汇总" in text
