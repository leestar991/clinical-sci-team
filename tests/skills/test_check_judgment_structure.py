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
    dump(ws / f"criteria_judge_{track}.json", {"条件数": len(cids), "四分类": {cat: {c: {"条件ID": c} for c in cids}}})


def ex_entry(concl: str, reason: str = "r", *, trig: bool | None = None):
    entry: dict = {"conclusion": concl, "reason": reason, "evidence": [{"page": 1, "quote": "q"}]}
    if trig is None and concl in ("符合", "不符合"):
        trig = concl == "不符合"
    if trig is not None:
        entry["exclusion_triggered"] = trig
    return entry


def write_judgments(ws: Path, track: str, judgments: dict, *, doc: str = "medical_record", stage: str = "draft"):
    # 统一证据源判定产物：顶层 `judgments`，无 documents 维度（doc 参数保留仅供旧用例兼容命名）
    counts = dict.fromkeys(("符合", "不符合", "存疑", "无法判断"), 0)
    for e in judgments.values():
        if isinstance(e, dict) and e.get("conclusion") in counts:
            counts[e["conclusion"]] += 1
    stem = "judgments_draft" if stage == "draft" else "judgments"
    return dump(
        pdir(ws) / f"{stem}_{PATIENT}_{track}.json",
        {"patient_id": PATIENT, "judgments": judgments, "summary": counts},
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


def test_gate1_missing_judgments(tmp_path):
    dump(pdir(tmp_path) / f"judgments_draft_{PATIENT}_EX.json", {"patient_id": PATIENT})
    assert any("缺少非空 `judgments`" in p for p in probs(tmp_path, "EX"))


def test_gate1_all_judgments_empty(tmp_path):
    dump(
        pdir(tmp_path) / f"judgments_draft_{PATIENT}_EX.json",
        {"patient_id": PATIENT, "judgments": {}, "summary": {}},
    )
    assert any("判定未产出" in p for p in probs(tmp_path, "EX"))


# ─────────────────── 闸 2：条件ID 覆盖恒等于标准包 ───────────────────


def test_gate2_detects_missing_condition(tmp_path):
    """改判不得删条目——判定条目数恒等于标准包条件数。"""
    write_pack(tmp_path, "EX", ["EX-1", "EX-2", "EX-3"])
    write_judgments(tmp_path, "EX", {c: ex_entry("符合") for c in ("EX-1", "EX-2")})
    write_gates(tmp_path, "EX")
    found = probs(tmp_path, "EX")
    assert any("闸2" in p and "缺失条件ID" in p and "['EX-3']" in p for p in found), found
    # 不带 --batch 时口径必须是整轨（分批口径由 test_batch_scope_* 覆盖）
    assert cjs.check(tmp_path, PATIENT, "EX", "draft", None, False)["闸2口径"] == "整轨标准包"


def test_gate2_detects_extra_condition(tmp_path):
    write_pack(tmp_path, "EX", ["EX-1"])
    write_judgments(tmp_path, "EX", {"EX-1": ex_entry("符合"), "IN-5": ex_entry("符合")})
    write_gates(tmp_path, "EX")
    found = probs(tmp_path, "EX")
    assert any("['IN-5']" in p and "跨轨污染" in p for p in found), found


def test_gate2_skipped_without_pack(tmp_path):
    write_judgments(tmp_path, "EX", {"EX-1": ex_entry("符合")})
    write_gates(tmp_path, "EX")
    r = cjs.check(tmp_path, PATIENT, "EX", "draft", None, False)
    assert r["problems"] == []
    assert any("闸2 跳过" in n for n in r["notes"])


def test_gate2_notes_declared_count_mismatch(tmp_path):
    dump(tmp_path / "criteria_judge_EX.json", {"条件数": 99, "四分类": {"排除_可从病例获取": {"EX-1": {"条件ID": "EX-1"}}}})
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
            "judgments": {"IN-1": {"conclusion": "符合", "reason": "r"}},
            "summary": {"符合": 9, "不符合": 9, "存疑": 0, "无法判断": 0},
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


# ─────────────────── 闸 9：evidence source 必须属于真实 OCR 来源集合 ───────────────────


def test_gate9_rejects_evidence_source_not_in_ocr_sources(tmp_path):
    """evidence[].source 自创（如 combined_ocr）→ 拦。物料维度唯一存活点，必须逐字等于真实来源名。"""
    write_phase2(tmp_path, ["筛选期病历", "筛选期检查"])
    write_pack(tmp_path, "IN", ["IN-1"])
    write_judgments(
        tmp_path,
        "IN",
        {"IN-1": {"conclusion": "符合", "reason": "r", "evidence": [{"source": "combined_ocr", "page": 1, "quote": "q"}]}},
    )
    write_gates(tmp_path, "IN")
    hits = [p for p in probs(tmp_path, "IN") if "闸9" in p]
    assert hits and "combined_ocr" in hits[0]


def test_gate9_passes_when_sources_are_known(tmp_path):
    """evidence 的 source 全部取自真实 OCR 来源集合 → 过。"""
    write_phase2(tmp_path, ["筛选期病历", "筛选期检查"])
    write_pack(tmp_path, "IN", ["IN-1"])
    write_judgments(
        tmp_path,
        "IN",
        {"IN-1": {"conclusion": "符合", "reason": "r", "evidence": [{"source": "筛选期病历", "page": 1, "quote": "q"}]}},
    )
    write_gates(tmp_path, "IN")
    assert probs(tmp_path, "IN") == []


def test_gate9_skipped_without_phase2_summary(tmp_path):
    write_pack(tmp_path, "IN", ["IN-1"])
    write_judgments(tmp_path, "IN", {"IN-1": {"conclusion": "符合", "reason": "r"}})
    write_gates(tmp_path, "IN")
    r = cjs.check(tmp_path, PATIENT, "IN", "draft", None, False)
    assert r["problems"] == []
    assert any("闸9 跳过" in n for n in r["notes"])


def test_gate9_skipped_when_ocr_results_empty(tmp_path):
    write_phase2(tmp_path, [])
    write_pack(tmp_path, "IN", ["IN-1"])
    write_judgments(tmp_path, "IN", {"IN-1": {"conclusion": "符合", "reason": "r"}})
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


# ─────────────── evidence 必须是数组（闸12）───────────────
#
# 真实故障（thread `dfbb4554`，患者 M018）：IN 轨 26 条 evidence **全是对象**
#     {"年龄": {"value": "62岁", "source": ..., "page": 1, "screenshot_ref": ..., "context": ...}}
# 而 EX 轨 37 条全是数组
#     [{"source": ..., "page": 1, "screenshot_ref": ..., "quote": ..., "relevance": ...}]
# 结构闸 exit_code=0 / problems=[] 完全放过——闸 3 只校验条目是 dict 且 conclusion 合法，
# 对 evidence 类型零检查。
#
# 后果是**静默丢证据**：`build_reports.py` 的
#     evidence = pick(item, "证据", "evidence", default=[]) or []
#     "证据": [normalize_evidence(e, ...) for e in evidence if isinstance(e, dict)]
# 对 dict 迭代拿到的是**键名字符串**，`isinstance(e, dict)` 全为 False，列表推导恒得 []，
# 模板 `item.证据||[]` 于是渲染成 "—"。报告不报错、不缺条目，只是证据栏全空，
# 肉眼极难发现——正是本技能反复出事的静默失败模式。


def evidence_entry(concl: str, evidence) -> dict:
    return {"conclusion": concl, "reason": "r", "evidence": evidence}


def test_evidence_as_object_is_blocking(tmp_path):
    """复现 dfbb4554：evidence 写成对象形态 → 必须阻断。"""
    write_pack(tmp_path, "IN", ["IN-2-1"])
    write_gates(tmp_path, "IN")
    write_judgments(
        tmp_path,
        "IN",
        {
            "IN-2-1": evidence_entry(
                "符合",
                {"年龄": {"value": "62岁", "source": "M018", "page": 1, "context": "记载「年龄：62岁」"}},
            )
        },
    )
    msgs = [m for m in probs(tmp_path, "IN") if "闸12" in m]
    assert msgs, "evidence 为对象必须阻断——否则报告静默丢证据"
    assert "IN-2-1" in msgs[0]
    assert "数组" in msgs[0] or "list" in msgs[0]


def test_evidence_as_empty_object_is_blocking(tmp_path):
    """`evidence: {}` 与 `evidence: []` 语义不同：前者是形态错误，不能当成"无证据"放过。"""
    write_pack(tmp_path, "IN", ["IN-1"])
    write_gates(tmp_path, "IN")
    write_judgments(tmp_path, "IN", {"IN-1": evidence_entry("无法判断", {})})
    assert [m for m in probs(tmp_path, "IN") if "闸12" in m]


def test_evidence_as_string_is_blocking(tmp_path):
    write_pack(tmp_path, "IN", ["IN-2-1"])
    write_gates(tmp_path, "IN")
    write_judgments(tmp_path, "IN", {"IN-2-1": evidence_entry("符合", "病历记载年龄62岁")})
    assert [m for m in probs(tmp_path, "IN") if "闸12" in m]


def test_evidence_array_of_objects_passes(tmp_path):
    write_pack(tmp_path, "IN", ["IN-2-1"])
    write_gates(tmp_path, "IN")
    write_judgments(
        tmp_path,
        "IN",
        {"IN-2-1": evidence_entry("符合", [{"source": "M018", "page": 1, "quote": "年龄：62岁"}])},
    )
    assert [m for m in probs(tmp_path, "IN") if "闸12" in m] == []


def test_empty_evidence_array_passes(tmp_path):
    """空数组是合法形态（原则七 B 另有"不得空 evidence"的语义要求，不由本闸管）。"""
    write_pack(tmp_path, "IN", ["IN-1"])
    write_gates(tmp_path, "IN")
    write_judgments(tmp_path, "IN", {"IN-1": evidence_entry("无法判断", [])})
    assert [m for m in probs(tmp_path, "IN") if "闸12" in m] == []


def test_missing_evidence_key_is_not_gate12(tmp_path):
    """缺 evidence 键不是形态错误（由原则七 B 的语义要求管），闸12 不重复报。"""
    write_pack(tmp_path, "IN", ["IN-1"])
    write_gates(tmp_path, "IN")
    write_judgments(tmp_path, "IN", {"IN-1": {"conclusion": "无法判断", "reason": "r"}})
    assert [m for m in probs(tmp_path, "IN") if "闸12" in m] == []


def test_array_containing_non_objects_is_blocking(tmp_path):
    """数组元素必须是对象——字符串元素会被 build_reports 的 isinstance 过滤掉，同样静默丢证据。"""
    write_pack(tmp_path, "IN", ["IN-2-1"])
    write_gates(tmp_path, "IN")
    write_judgments(tmp_path, "IN", {"IN-2-1": evidence_entry("符合", ["年龄62岁", {"page": 1, "quote": "q"}])})
    msgs = [m for m in probs(tmp_path, "IN") if "闸12" in m]
    assert msgs
    assert "IN-2-1" in msgs[0]


def test_ex_track_object_evidence_also_blocking(tmp_path):
    """两轨同一口径——本次是 IN 轨出错，EX 轨同样要拦。"""
    write_pack(tmp_path, "EX", ["EX-1-1"])
    write_gates(tmp_path, "EX")
    write_judgments(tmp_path, "EX", {"EX-1-1": {"conclusion": "符合", "reason": "r", "evidence": {"a": 1}, "exclusion_triggered": False}})
    assert [m for m in probs(tmp_path, "EX") if "闸12" in m]


def test_gate12_names_every_offending_condition(tmp_path):
    """26 条全错时要能一次看清范围，不是逐条刷屏。"""
    write_pack(tmp_path, "IN", ["IN-1", "IN-2-1", "IN-2-2"])
    write_gates(tmp_path, "IN")
    write_judgments(
        tmp_path,
        "IN",
        {
            "IN-1": evidence_entry("无法判断", {}),
            "IN-2-1": evidence_entry("符合", {"年龄": {"value": "62岁"}}),
            "IN-2-2": evidence_entry("符合", [{"page": 1, "quote": "男"}]),
        },
    )
    msgs = [m for m in probs(tmp_path, "IN") if "闸12" in m]
    assert len(msgs) == 1, "应汇总成一条，而不是每条一行"
    assert "IN-1" in msgs[0] and "IN-2-1" in msgs[0]
    assert "IN-2-2" not in msgs[0], "合法条目不得被点名"


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


# ── 闸 2 的批次口径（会话 09eeaffb 分批判定）──────────────────────────────────
#
# 整轨一次派时两个判定子代理各跑 99 个 AI 回合、0 次 write_file，撞满 recursion_limit=420
# （10.02M token、零产物）。判定改为轨内 12 条一批后，批级 draft 只含本批条目，
# 若闸 2 仍按整轨核，每一批都会被报「缺 16 条」——正确的批也过不去，
# 而子代理修这个"缺失"的唯一办法就是去判别批的条目（正是模板第 0 条禁止的）。
#
# 关键性质：收窄口径 ≠ 放宽校验。本批漏一条、或顺手判了别批的条目，同样 exit 2。


def write_batch_plan(ws: Path, track: str, batches: list[list[str]]) -> Path:
    return dump(
        pdir(ws) / f"judge_batches_{PATIENT}_{track}.json",
        {
            "patient_id": PATIENT,
            "track": track,
            "batch_size": 12,
            "batch_count": len(batches),
            "batches": [
                {"batch": i + 1, "condition_ids": ids, "count": len(ids), "draft_file": f"judgments_draft_{PATIENT}_{track}_b{i + 1}.json"}
                for i, ids in enumerate(batches)
            ],
        },
    )


def write_batch_judgments(ws: Path, track: str, batch: int, judgments: dict, *, doc: str = "medical_record"):
    counts = dict.fromkeys(("符合", "不符合", "存疑", "无法判断"), 0)
    for e in judgments.values():
        if isinstance(e, dict) and e.get("conclusion") in counts:
            counts[e["conclusion"]] += 1
    return dump(
        pdir(ws) / f"judgments_draft_{PATIENT}_{track}_b{batch}.json",
        {"patient_id": PATIENT, "judgments": judgments, "summary": counts},
    )


def write_batch_gates(ws: Path, track: str, batch: int, *, missed: list[str] | None = None, conflicts: list[str] | None = None):
    dump(pdir(ws) / f"uncertain_recheck_{PATIENT}_{track}_b{batch}.json", {"suspected_missed": missed or []})
    if track == "EX":
        dump(pdir(ws) / f"exclusion_direction_check_{PATIENT}_EX_b{batch}.json", {"conflicts": conflicts or [], "advisories": []})


IN_TRACK_IDS = [f"IN-{n}" for n in range(1, 25)]
IN_BATCHES = [IN_TRACK_IDS[:12], IN_TRACK_IDS[12:]]


def _setup_batch(ws: Path, batch: int, judged: list[str]):
    write_pack(ws, "IN", IN_TRACK_IDS)
    write_batch_plan(ws, "IN", IN_BATCHES)
    write_batch_judgments(ws, "IN", batch, {c: {"conclusion": "符合", "reason": "r", "evidence": []} for c in judged})
    write_batch_gates(ws, "IN", batch)


def batch_probs(ws: Path, batch: int) -> list[str]:
    return cjs.check(ws, PATIENT, "IN", "draft", None, False, batch=batch)["problems"]


def test_batch_scope_passes_when_batch_is_complete(tmp_path):
    """批级 draft 只含本批 12 条 —— 按批次口径应当全过（整轨口径会误报缺 12 条）。"""
    _setup_batch(tmp_path, 1, IN_BATCHES[0])
    assert batch_probs(tmp_path, 1) == []


def test_batch_scope_still_catches_a_missing_condition_within_the_batch(tmp_path):
    """收窄口径不等于放宽：本批漏一条照样 exit 2。"""
    _setup_batch(tmp_path, 1, IN_BATCHES[0][:-1])
    found = batch_probs(tmp_path, 1)
    assert any("闸2" in p and "缺失条件ID" in p and IN_BATCHES[0][-1] in p for p in found), found


def test_batch_scope_rejects_conditions_from_another_batch(tmp_path):
    """顺手判了别批的条目 → 阻断（两个子代理会同时写同一批条目）。"""
    _setup_batch(tmp_path, 1, [*IN_BATCHES[0], IN_BATCHES[1][0]])
    found = batch_probs(tmp_path, 1)
    assert any("闸2" in p and IN_BATCHES[1][0] in p and "不要顺手判" in p for p in found), found


def test_batch_scope_reads_the_batch_level_draft_file(tmp_path):
    """`--batch N` 必须去看 `_bN` 文件，不是整轨文件。"""
    write_pack(tmp_path, "IN", IN_TRACK_IDS)
    write_batch_plan(tmp_path, "IN", IN_BATCHES)
    # 只有整轨文件存在，批级文件缺失 → 闸1 必须报缺文件
    write_judgments(tmp_path, "IN", {c: {"conclusion": "符合", "reason": "r", "evidence": []} for c in IN_TRACK_IDS})
    found = batch_probs(tmp_path, 1)
    assert any("闸1" in p and f"_IN_b1.json" in p for p in found), found


def test_batch_gate_artifacts_are_per_batch(tmp_path):
    """闸产物也按批：用整轨产物核批级 draft 会把别批的漏判算到本批头上。"""
    _setup_batch(tmp_path, 1, IN_BATCHES[0])
    # 整轨产物报漏判、批级产物干净 → 批级检查应当只看批级产物（全过）
    dump(pdir(tmp_path) / f"uncertain_recheck_{PATIENT}_IN.json", {"suspected_missed": IN_BATCHES[1][:1]})
    assert batch_probs(tmp_path, 1) == []
    # 反之，批级产物报漏判就必须阻断
    write_batch_gates(tmp_path, "IN", 1, missed=[IN_BATCHES[0][0]])
    assert any("闸6" in p for p in batch_probs(tmp_path, 1))


def test_missing_batch_plan_is_blocking_not_silently_full_track(tmp_path):
    """读不到批次清单时必须报错，⛔ 不得静默退回整轨口径（那会误报缺失）。"""
    write_pack(tmp_path, "IN", IN_TRACK_IDS)
    write_batch_judgments(tmp_path, "IN", 1, {c: {"conclusion": "符合", "reason": "r", "evidence": []} for c in IN_BATCHES[0]})
    write_batch_gates(tmp_path, "IN", 1)
    found = batch_probs(tmp_path, 1)
    assert any("闸2" in p and "读不到批次清单" in p and "plan-batches" in p for p in found), found


def test_unknown_batch_number_is_blocking(tmp_path):
    """`--batch` 与清单不是同一次规划 → 报错，不当成空集合（空集合会伪装成一堆越界条目）。"""
    _setup_batch(tmp_path, 1, IN_BATCHES[0])
    dump(pdir(tmp_path) / f"judgments_draft_{PATIENT}_IN_b9.json", json.loads((pdir(tmp_path) / f"judgments_draft_{PATIENT}_IN_b1.json").read_text(encoding="utf-8")))
    dump(pdir(tmp_path) / f"uncertain_recheck_{PATIENT}_IN_b9.json", {"suspected_missed": []})
    found = batch_probs(tmp_path, 9)
    assert any("闸2" in p and "没有第 9 批" in p for p in found), found


def test_batch_plan_with_ids_outside_the_pack_is_blocking(tmp_path):
    """清单与标准包不是同一次产出（包被重新 slim 过）→ 报错。"""
    write_pack(tmp_path, "IN", IN_TRACK_IDS)
    write_batch_plan(tmp_path, "IN", [[*IN_BATCHES[0][:11], "IN-999"]])
    write_batch_judgments(tmp_path, "IN", 1, {c: {"conclusion": "符合", "reason": "r", "evidence": []} for c in IN_BATCHES[0][:11]})
    write_batch_gates(tmp_path, "IN", 1)
    found = batch_probs(tmp_path, 1)
    assert any("闸2" in p and "IN-999" in p and "不是同一次产出" in p for p in found), found


def test_full_track_scope_catches_a_whole_missing_batch(tmp_path):
    """⛔ 整轨口径不可省：只有它会因为**少了一整批**而报错。

    批级闸各自只保证「本批完整」，没有任何一道闸会发现漏派了一整批 —— 除了合并后
    以整轨口径重跑的这一次。
    """
    write_pack(tmp_path, "IN", IN_TRACK_IDS)
    # 合并结果只含批 1（漏派了批 2）
    write_judgments(tmp_path, "IN", {c: {"conclusion": "符合", "reason": "r", "evidence": []} for c in IN_BATCHES[0]})
    write_gates(tmp_path, "IN")
    found = probs(tmp_path, "IN")
    assert any("闸2" in p and "缺失条件ID" in p for p in found), found
    missing_reported = [p for p in found if "缺失条件ID" in p][0]
    for cid in IN_BATCHES[1][:3]:
        assert cid in missing_reported


def test_batch_gate_artifact_does_not_overwrite_the_track_one(tmp_path):
    """QC 前置读的是整轨那份闸产物，批级的不能覆盖它。"""
    _setup_batch(tmp_path, 1, IN_BATCHES[0])
    cjs.check(tmp_path, PATIENT, "IN", "draft", None, False, batch=1)
    cjs.write_gate_artifact(tmp_path, cjs.check(tmp_path, PATIENT, "IN", "draft", None, False, batch=1))
    batch_artifact = pdir(tmp_path) / f"judgment_structure_gate_{PATIENT}_IN_b1.json"
    track_artifact = pdir(tmp_path) / f"judgment_structure_gate_{PATIENT}_IN.json"
    assert batch_artifact.exists()
    assert not track_artifact.exists(), "批级闸产物覆盖了整轨那份 —— QC 会把「某批过了」读成「整轨过了」"
    assert json.loads(batch_artifact.read_text(encoding="utf-8"))["batch"] == 1


def test_cli_rejects_batch_below_one(tmp_path):
    _setup_batch(tmp_path, 1, IN_BATCHES[0])
    with pytest.raises(SystemExit) as exc:
        cjs.main(["--workspace", str(tmp_path), "--patient", PATIENT, "--track", "IN", "--batch", "0"])
    assert exc.value.code == 2


def test_cli_batch_flow_exit_codes(tmp_path):
    _setup_batch(tmp_path, 1, IN_BATCHES[0])
    argv = ["--workspace", str(tmp_path), "--patient", PATIENT, "--track", "IN", "--batch", "1"]
    assert cjs.main(argv) == 0
    write_batch_gates(tmp_path, "IN", 1, missed=[IN_BATCHES[0][0]])
    assert cjs.main(argv) == 2


def test_cli_accepts_explicit_batch_plan_path(tmp_path):
    _setup_batch(tmp_path, 1, IN_BATCHES[0])
    moved = tmp_path / "elsewhere" / "plan.json"
    moved.parent.mkdir(parents=True)
    moved.write_text((pdir(tmp_path) / f"judge_batches_{PATIENT}_IN.json").read_text(encoding="utf-8"), encoding="utf-8")
    (pdir(tmp_path) / f"judge_batches_{PATIENT}_IN.json").unlink()
    assert cjs.main(["--workspace", str(tmp_path), "--patient", PATIENT, "--track", "IN", "--batch", "1", "--batch-plan", str(moved)]) == 0
