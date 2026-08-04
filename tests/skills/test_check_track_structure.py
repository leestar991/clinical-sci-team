"""check_track_structure 回归测试：单轨标准结构闸（QC 前置 + 修订后守恒）。

保证 QC 前的结构问题与修订后的丢条都被机械拦住，而不是靠模型自觉。
核心回归：thread `5d987e97` —— 全量 write_file 修订让 `EX-7` 实体条目静默消失，
总条数「符合预期地下降了」（合并本身会减少条数），必须由闸 5/6/7 各自独立抓到。
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "skills" / "custom" / "criteria-parser" / "scripts" / "check_track_structure.py"

if not SCRIPT_PATH.exists():  # skills/custom 为 gitignore 目录，清空检出时跳过
    pytest.skip("criteria-parser 技能未安装", allow_module_level=True)


def _load_module():
    spec = importlib.util.spec_from_file_location("check_track_structure", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


cts = _load_module()


# ────────────────────────────── 夹具 ──────────────────────────────


def write_track(workspace: Path, track: str, entities: dict[str, list[str]], index: list[str] | None = None, **extra):
    """entities: {类目名: [条件ID...]}；index 省略时按 entities 自动一一对应。"""
    ids = [cid for cids in entities.values() for cid in cids]
    payload = {
        "四分类": {cat: [{"条件ID": cid} for cid in cids] for cat, cids in entities.items()},
        "描述索引": {cid: "短描述" for cid in (index if index is not None else ids)},
    }
    payload.update(extra)
    path = workspace / f"criteria_parsed_{track}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def write_meta(workspace: Path, 入选: int | None = None, 排除: int | None = None):
    last = {}
    if 入选 is not None:
        last["入选"] = 入选
    if 排除 is not None:
        last["排除"] = 排除
    (workspace / "criteria_meta.json").write_text(json.dumps({"末条号": last}, ensure_ascii=False), encoding="utf-8")


def write_qc(workspace: Path, track: str, condition_ids: list[str], rnd: int = 1):
    path = workspace / f"criteria_qc_{track}.json"
    path.write_text(
        json.dumps(
            {
                "track": track,
                "passed": not condition_ids,
                "round": rnd,
                "blocking_issues": [{"id": f"CQC-{i}", "condition_id": cid} for i, cid in enumerate(condition_ids)],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def run(workspace: Path, *args) -> int:
    return cts.main(["--workspace", str(workspace), *args])


def problems(workspace: Path, track: str, **kwargs) -> list[str]:
    report = cts.check_track(workspace, track, kwargs.get("qc"), kwargs.get("snapshot", False))
    return report["problems"]


# ────────────────────────── parse_cid ──────────────────────────


@pytest.mark.parametrize(
    ("cid", "expected"),
    [
        ("IN-2", ("IN", 2, None)),
        ("EX-4-2", ("EX", 4, 2)),
        ("EX-13-10", ("EX", 13, 10)),
        (" IN-1-1 ", ("IN", 1, 1)),
        ("IN-2-a", None),
        ("IN-2.1", None),
        ("EX-5-E1", None),  # 禁用的自创豁免编号
        ("IN2", None),
        ("", None),
    ],
)
def test_parse_cid(cid, expected):
    assert cts.parse_cid(cid) == expected


# ─────────────────── 闸 1：顶层结构 ───────────────────


def test_missing_file_is_skipped(tmp_path):
    report = cts.check_track(tmp_path, "IN", None, False)
    assert report["problems"] == []
    assert "不存在" in report["skipped"]


def test_gate1_rejects_invalid_json(tmp_path):
    (tmp_path / "criteria_parsed_IN.json").write_text("{不是合法 JSON", encoding="utf-8")
    assert any("闸1 JSON 不合法" in p for p in problems(tmp_path, "IN"))


def test_gate1_rejects_missing_四分类(tmp_path):
    (tmp_path / "criteria_parsed_EX.json").write_text('{"描述索引": {}}', encoding="utf-8")
    assert any("缺少 `四分类`" in p for p in problems(tmp_path, "EX"))


@pytest.mark.parametrize("key", ["方案元数据", "解析说明", "汇总统计"])
def test_gate1_rejects_global_fields(tmp_path, key):
    """单轨文件不得产出全篇级字段（两轨都写会冲突 / 必须由 assemble 重算）。"""
    write_track(tmp_path, "IN", {"入选_可从病例获取": ["IN-1"]}, **{key: {"x": 1}})
    assert any(f"不得含全篇级字段 `{key}`" in p for p in problems(tmp_path, "IN"))


def test_gate1_rejects_other_track_category(tmp_path):
    """IN 轨文件里出现排除类目 —— thread 5a1c8d95 的同类越界。"""
    write_track(tmp_path, "IN", {"入选_可从病例获取": ["IN-1"], "排除_可从病例获取": ["EX-1"]})
    probs = problems(tmp_path, "IN")
    assert any("出现非本轨类目" in p for p in probs)
    assert any("落在非本轨类目" in p for p in probs)


# ─────────────────── 闸 2/3：条件ID 唯一与前缀 ───────────────────


def test_gate2_detects_duplicate_ids(tmp_path):
    write_track(tmp_path, "IN", {"入选_可从病例获取": ["IN-1", "IN-2"], "入选_不可从病例获取": ["IN-1"]})
    assert any("闸2 条件ID 重复：['IN-1']" in p for p in problems(tmp_path, "IN"))


def test_gate3_detects_wrong_prefix(tmp_path):
    write_track(tmp_path, "IN", {"入选_可从病例获取": ["IN-1", "EX-9"]})
    assert any("前缀与轨不一致：['EX-9']" in p for p in problems(tmp_path, "IN"))


def test_gate3_detects_malformed_id(tmp_path):
    write_track(tmp_path, "EX", {"排除_可从病例获取": ["EX-1", "EX-5-E1"]})
    assert any("不符合" in p and "EX-5-E1" in p for p in problems(tmp_path, "EX"))


# ─────────────────── 闸 4：子序号规范 ───────────────────


def test_gate4_detects_mixed_subnumbering(tmp_path):
    """同一原条号「一个带子序号、一个不带」——SKILL 明令禁止的混用。"""
    write_track(tmp_path, "IN", {"入选_可从病例获取": ["IN-6", "IN-6-2"]})
    assert any("`IN-6` 混用" in p for p in problems(tmp_path, "IN"))


def test_gate4_detects_non_contiguous_subnumbers(tmp_path):
    write_track(tmp_path, "IN", {"入选_可从病例获取": ["IN-2-1", "IN-2-3"]})
    assert any("子序号不连续：[1, 3]" in p for p in problems(tmp_path, "IN"))


def test_gate4_accepts_single_bare_and_contiguous(tmp_path):
    write_track(tmp_path, "EX", {"排除_可从病例获取": ["EX-1", "EX-2-1", "EX-2-2", "EX-2-3"]})
    assert problems(tmp_path, "EX") == []


# ─────────────────── 闸 5：描述索引双向对齐 ───────────────────


def test_gate5_detects_two_id_systems(tmp_path):
    """thread 5d987e97：四分类用子条粒度、描述索引用父条粒度，形成两套 ID 体系。"""
    write_track(
        tmp_path,
        "EX",
        {"排除_可从病例获取": ["EX-2-1", "EX-2-2", "EX-4-1", "EX-4-2", "EX-4-3"]},
        index=["EX-2", "EX-4"],
    )
    report = cts.check_track(tmp_path, "EX", None, False)
    assert report["miss_in_index"] == ["EX-2-1", "EX-2-2", "EX-4-1", "EX-4-2", "EX-4-3"]
    assert report["extra_in_index"] == ["EX-2", "EX-4"]
    assert any("缺键" in p and "补索引（禁止反向删实体）" in p for p in report["problems"])
    assert any("无实体" in p and "丢条" in p for p in report["problems"])


def test_gate5_passes_when_aligned(tmp_path):
    write_track(tmp_path, "IN", {"入选_可从病例获取": ["IN-1"], "入选_不可从病例获取": ["IN-2"]})
    report = cts.check_track(tmp_path, "IN", None, False)
    assert report["miss_in_index"] == [] and report["extra_in_index"] == []
    assert report["problems"] == []


# ─────────────────── 闸 6：原条号全覆盖（无基线丢条探测）───────────────────


def test_gate6_detects_missing_parent_without_baseline(tmp_path):
    """5d987e97 核心回归：EX-7 整条消失，且总条数看起来「合并得挺合理」。"""
    write_meta(tmp_path, 排除=17)
    ids = [f"EX-{n}" for n in range(1, 18) if n != 7]
    write_track(tmp_path, "EX", {"排除_可从病例获取": ids}, index=[*ids, "EX-7"])
    probs = problems(tmp_path, "EX")
    assert any("闸6 原条号无任何实体条目：['EX-7']" in p and "末条号=17" in p for p in probs)


def test_gate6_accepts_merged_subconditions(tmp_path):
    """把 EX-9-1..-6 合并成 EX-9 是 QC 要求的正确动作，闸6 不得误报。"""
    write_meta(tmp_path, 排除=3)
    write_track(tmp_path, "EX", {"排除_可从病例获取": ["EX-1", "EX-2", "EX-3"]})
    assert problems(tmp_path, "EX") == []


def test_gate6_skipped_without_meta(tmp_path):
    write_track(tmp_path, "IN", {"入选_可从病例获取": ["IN-2"]})
    report = cts.check_track(tmp_path, "IN", None, False)
    assert report["problems"] == []
    assert any("闸6 跳过" in n for n in report["notes"])


def test_gate6_skipped_on_invalid_meta(tmp_path):
    (tmp_path / "criteria_meta.json").write_text("{坏", encoding="utf-8")
    write_track(tmp_path, "IN", {"入选_可从病例获取": ["IN-1"]})
    report = cts.check_track(tmp_path, "IN", None, False)
    assert any("闸6 跳过" in n for n in report["notes"])


# ─────────────────── 闸 7：QC 目标实体存在 ───────────────────


def test_gate7_detects_revision_that_dropped_qc_target(tmp_path):
    """5d987e97：QC 阻断项指向 EX-7-1，修订后原条号 7 一个实体都不剩。"""
    ids = [f"EX-{n}" for n in range(1, 18) if n != 7]
    write_track(tmp_path, "EX", {"排除_可从病例获取": ids})
    qc = write_qc(tmp_path, "EX", ["EX-7-1", "EX-2-1"])
    probs = problems(tmp_path, "EX", qc=qc)
    assert any("闸7" in p and "EX-7" in p and "修订把条目改丢了" in p for p in probs)
    # EX-2 仍在（合并成 EX-2 是合法结果），不得误报
    assert not any("EX-2（QC 项" in p for p in probs)


def test_gate7_accepts_merge_target_present(tmp_path):
    write_track(tmp_path, "EX", {"排除_可从病例获取": ["EX-2", "EX-4", "EX-7", "EX-9"]})
    qc = write_qc(tmp_path, "EX", ["EX-2-1", "EX-4-1", "EX-7-1", "EX-9-1"])
    assert problems(tmp_path, "EX", qc=qc) == []


def test_gate7_ignores_other_track_condition_ids(tmp_path):
    write_track(tmp_path, "EX", {"排除_可从病例获取": ["EX-1"]})
    qc = write_qc(tmp_path, "EX", ["IN-5-2"])
    assert problems(tmp_path, "EX", qc=qc) == []


def test_gate7_flags_missing_qc_report(tmp_path):
    write_track(tmp_path, "EX", {"排除_可从病例获取": ["EX-1"]})
    probs = problems(tmp_path, "EX", qc=tmp_path / "nope.json")
    assert any("闸7 QC 报告不存在" in p for p in probs)


# ─────────────────── 基线快照与差异 ───────────────────


def test_snapshot_then_diff_reports_vanished_and_added(tmp_path):
    write_track(tmp_path, "IN", {"入选_可从病例获取": ["IN-1-1", "IN-1-2", "IN-2"]})
    cts.check_track(tmp_path, "IN", None, True)
    assert (tmp_path / "criteria_structure_baseline_IN.json").exists()

    # 修订：IN-1-1/-2 合并为 IN-1
    write_track(tmp_path, "IN", {"入选_可从病例获取": ["IN-1", "IN-2"]})
    report = cts.check_track(tmp_path, "IN", None, False)
    assert report["基线差异"] == {
        "before_total": 3,
        "after_total": 2,
        "消失": ["IN-1-1", "IN-1-2"],
        "新增": ["IN-1"],
    }
    assert report["problems"] == []  # 差异是诊断信息，不是闸


def test_diff_absent_without_baseline(tmp_path):
    write_track(tmp_path, "IN", {"入选_可从病例获取": ["IN-1"]})
    assert "基线差异" not in cts.check_track(tmp_path, "IN", None, False)


# ─────────────────── 闸 8：QC 原地打转探测 ───────────────────


def test_gate8_detects_stalled_qc(tmp_path):
    """thread 345f2bf4：R3/R4/R5 阻断项完全一致，3 轮配额空转。"""
    write_track(tmp_path, "IN", {"入选_可从病例获取": ["IN-1", "IN-2"]})
    qc = write_qc(tmp_path, "IN", ["IN-1", "IN-2"], rnd=3)
    assert problems(tmp_path, "IN", qc=qc) == []  # 首轮无历史，不报

    qc = write_qc(tmp_path, "IN", ["IN-2", "IN-1"], rnd=4)  # 顺序不同、集合相同
    hits = [p for p in problems(tmp_path, "IN", qc=qc) if "闸8" in p]
    assert hits and "原地打转" in hits[0] and "upstream_issues" in hits[0]


def test_gate8_silent_when_blocking_set_changes(tmp_path):
    write_track(tmp_path, "IN", {"入选_可从病例获取": ["IN-1", "IN-2"]})
    problems(tmp_path, "IN", qc=write_qc(tmp_path, "IN", ["IN-1", "IN-2"], rnd=3))
    assert problems(tmp_path, "IN", qc=write_qc(tmp_path, "IN", ["IN-1"], rnd=4)) == []


def test_gate8_silent_when_no_blocking_issues(tmp_path):
    """收敛（阻断项为空）不得被判为打转。"""
    write_track(tmp_path, "IN", {"入选_可从病例获取": ["IN-1"]})
    problems(tmp_path, "IN", qc=write_qc(tmp_path, "IN", [], rnd=3))
    assert problems(tmp_path, "IN", qc=write_qc(tmp_path, "IN", [], rnd=4)) == []


def test_gate8_rerunning_same_round_is_not_stall(tmp_path):
    """同一轮重复跑脚本（改一条查一条）不得误判为打转。"""
    write_track(tmp_path, "IN", {"入选_可从病例获取": ["IN-1"]})
    qc = write_qc(tmp_path, "IN", ["IN-1"], rnd=3)
    assert problems(tmp_path, "IN", qc=qc) == []
    assert problems(tmp_path, "IN", qc=qc) == []
    assert problems(tmp_path, "IN", qc=qc) == []


def test_gate8_history_file_records_rounds(tmp_path):
    write_track(tmp_path, "IN", {"入选_可从病例获取": ["IN-1", "IN-2"]})
    problems(tmp_path, "IN", qc=write_qc(tmp_path, "IN", ["IN-1"], rnd=1))
    problems(tmp_path, "IN", qc=write_qc(tmp_path, "IN", ["IN-2"], rnd=2))
    hist = json.loads((tmp_path / "criteria_qc_history_IN.json").read_text(encoding="utf-8"))
    assert [h["round"] for h in hist] == [1, 2]
    assert [h["blocking_ids"] for h in hist] == [["IN-1"], ["IN-2"]]


def test_gate8_tolerates_corrupt_history(tmp_path):
    write_track(tmp_path, "IN", {"入选_可从病例获取": ["IN-1"]})
    (tmp_path / "criteria_qc_history_IN.json").write_text("{坏", encoding="utf-8")
    report = cts.check_track(tmp_path, "IN", write_qc(tmp_path, "IN", ["IN-1"], rnd=3), False)
    assert report["problems"] == []
    assert any("重新计数" in n for n in report["notes"])


# ─────────────────── 闸产物（QC 子代理前置自检）───────────────────


def test_gate_artifact_written_with_exit_code_and_digest(tmp_path):
    write_track(tmp_path, "EX", {"排除_可从病例获取": ["EX-1"]})
    assert run(tmp_path, "--track", "EX") == 0
    art = json.loads((tmp_path / "criteria_structure_gate_EX.json").read_text(encoding="utf-8"))
    assert art["exit_code"] == 0
    assert art["checked_file"] == "criteria_parsed_EX.json"
    assert len(art["content_sha256_16"]) == 16
    assert art["problems"] == []


def test_gate_artifact_records_failure(tmp_path):
    write_track(tmp_path, "EX", {"排除_可从病例获取": ["EX-1", "EX-1"]})
    assert run(tmp_path, "--track", "EX") == 2
    art = json.loads((tmp_path / "criteria_structure_gate_EX.json").read_text(encoding="utf-8"))
    assert art["exit_code"] == 2
    assert any("闸2" in p for p in art["problems"])


def test_gate_artifact_digest_tracks_file_change(tmp_path):
    """哈希必须随文件变化——QC 子代理靠它发现「闸跑完后文件又被改过」。"""
    write_track(tmp_path, "EX", {"排除_可从病例获取": ["EX-1"]})
    run(tmp_path, "--track", "EX")
    first = json.loads((tmp_path / "criteria_structure_gate_EX.json").read_text(encoding="utf-8"))
    write_track(tmp_path, "EX", {"排除_可从病例获取": ["EX-1", "EX-2"]})
    run(tmp_path, "--track", "EX")
    second = json.loads((tmp_path / "criteria_structure_gate_EX.json").read_text(encoding="utf-8"))
    assert first["content_sha256_16"] != second["content_sha256_16"]


def test_gate_artifact_skipped_for_missing_track(tmp_path):
    write_track(tmp_path, "IN", {"入选_可从病例获取": ["IN-1"]})
    run(tmp_path)  # EX 不存在
    assert (tmp_path / "criteria_structure_gate_IN.json").exists()
    assert not (tmp_path / "criteria_structure_gate_EX.json").exists()


# ─────────────────── CLI / 退出码 ───────────────────


def test_cli_exit_2_on_failure_and_0_on_clean(tmp_path):
    write_meta(tmp_path, 入选=2)
    write_track(tmp_path, "IN", {"入选_可从病例获取": ["IN-1"]})  # 缺 IN-2 → 闸6
    assert run(tmp_path, "--track", "IN") == 2

    write_track(tmp_path, "IN", {"入选_可从病例获取": ["IN-1", "IN-2"]})
    assert run(tmp_path, "--track", "IN") == 0


def test_cli_checks_both_tracks_by_default(tmp_path):
    write_track(tmp_path, "IN", {"入选_可从病例获取": ["IN-1"]})
    write_track(tmp_path, "EX", {"排除_可从病例获取": ["EX-1", "EX-1"]})  # 重复 → 闸2
    assert run(tmp_path) == 2


def test_cli_requires_track_with_qc(tmp_path):
    with pytest.raises(SystemExit):
        run(tmp_path, "--qc", str(tmp_path / "criteria_qc_EX.json"))


def test_cli_writes_json_report(tmp_path):
    write_track(tmp_path, "EX", {"排除_可从病例获取": ["EX-1"]})
    out = tmp_path / "report.json"
    assert run(tmp_path, "--track", "EX", "--json", str(out)) == 0
    assert json.loads(out.read_text(encoding="utf-8"))[0]["track"] == "EX"


def test_summary_blocks_qc_dispatch_message(tmp_path):
    """未过闸时摘要必须显式禁止派 QC —— 这是脚本存在的意义。"""
    write_track(tmp_path, "EX", {"排除_可从病例获取": ["EX-1", "EX-1"]})
    text = cts.summarize([cts.check_track(tmp_path, "EX", None, False)])
    assert "禁止发 task(quality-control)" in text


# ─────────────── 闸 9：原文忠实性（thread `6e5ac7c1`）───────────────
#
# 委派模板把 `段行号`（试验方案.md 坐标，约 3766-4052）用于 read_file
# `eligibility_criteria_raw.md`（仅 794 行）→ 越界切片静默返回空串，
# 子代理凭通用知识编造了 54 条中的 50 条。闸 1-8 全过，因为条件ID 体系自洽。


RAW_IN = """# 4.1 入选标准

1.  自愿参加临床试验，并签署知情同意书。

2.  年龄 18 周岁以上（含），70 岁以下（含）。性别不限。

# 4.2 排除标准

1.  筛选前，已知为微卫星高不稳定性（MSI-H）。
"""


def write_raw(workspace: Path, text: str = RAW_IN) -> Path:
    path = workspace / "eligibility_criteria_raw.md"
    path.write_text(text, encoding="utf-8")
    return path


def write_track_with_quotes(workspace: Path, track: str, quotes: dict[str, str]):
    """quotes: {条件ID: 原文}"""
    cat = "入选_可从病例获取" if track == "IN" else "排除_可从病例获取"
    payload = {
        "四分类": {cat: [{"条件ID": cid, "原文": q} for cid, q in quotes.items()]},
        "描述索引": {cid: "短描述" for cid in quotes},
    }
    (workspace / f"criteria_parsed_{track}.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_gate9_passes_when_quotes_are_verbatim(tmp_path):
    write_raw(tmp_path)
    write_track_with_quotes(
        tmp_path,
        "IN",
        {
            "IN-1": "自愿参加临床试验，并签署知情同意书。",
            "IN-2": "年龄 18 周岁以上（含），70 岁以下（含）。性别不限。",
        },
    )
    assert not [p for p in problems(tmp_path, "IN") if "闸9" in p]


def test_gate9_tolerates_whitespace_and_width_differences(tmp_path):
    """PDF 提取的空格/换行/全半角不稳定，不能因此假阳性。"""
    write_raw(tmp_path)
    write_track_with_quotes(
        tmp_path,
        "IN",
        {
            "IN-2": "年龄18周岁以上（含），\n70 岁以下（含）。性别不限。",  # 空白与换行都变了
        },
    )
    assert not [p for p in problems(tmp_path, "IN") if "闸9" in p]


def test_gate9_catches_fabricated_quotes(tmp_path):
    """ec 原始症状：原文是编造的通用 mCRC 标准，原方案里没有。"""
    write_raw(tmp_path)
    write_track_with_quotes(
        tmp_path,
        "IN",
        {
            "IN-1": "自愿参加临床试验，并签署知情同意书。",  # 真
            "IN-2": "7. 预期生存期≥3 个月。",  # 编造
            "IN-3": "已知存在 Gilbert 综合征。",  # 编造
        },
    )
    hits = [p for p in problems(tmp_path, "IN") if "闸9" in p]
    assert hits, "编造的原文必须被抓出"
    assert "IN-2" in hits[0] and "IN-3" in hits[0]
    assert "IN-1" not in hits[0], "逐字正确的条目不得被误报"
    assert "2/3" in hits[0]


def test_gate9_reports_ratio_and_truncates_long_lists(tmp_path):
    write_raw(tmp_path)
    write_track_with_quotes(tmp_path, "IN", {f"IN-{i}": f"编造内容第{i}条。" for i in range(1, 21)})
    hits = [p for p in problems(tmp_path, "IN") if "闸9" in p]
    assert hits
    assert "20/20" in hits[0] and "100%" in hits[0]
    assert "等 20 条" in hits[0], "长清单需截断并给总数"


def test_gate9_blocks_on_empty_raw(tmp_path):
    write_raw(tmp_path, "   \n")
    write_track_with_quotes(tmp_path, "IN", {"IN-1": "任何内容"})
    hits = [p for p in problems(tmp_path, "IN") if "闸9" in p]
    assert hits and "为空" in hits[0]


def test_gate9_skips_when_raw_missing(tmp_path):
    write_track_with_quotes(tmp_path, "IN", {"IN-1": "任何内容"})
    report = cts.check_track(tmp_path, "IN", None, False)
    assert not [p for p in report["problems"] if "闸9" in p]
    assert any("闸9 跳过" in n for n in report["notes"])


def test_gate9_skips_entities_without_quote_field(tmp_path):
    """既有夹具只写 条件ID、不写 原文，闸9 必须跳过而非报错。"""
    write_raw(tmp_path)
    write_track(tmp_path, "IN", {"入选_可从病例获取": ["IN-1", "IN-2"]})
    report = cts.check_track(tmp_path, "IN", None, False)
    assert not [p for p in report["problems"] if "闸9" in p]
    assert report["原文核对"]["已核对"] == 0


def test_gate9_message_names_the_coordinate_trap(tmp_path):
    """报错必须指向真正的病因：段行号 vs raw段行号。"""
    write_raw(tmp_path)
    write_track_with_quotes(tmp_path, "IN", {"IN-1": "编造内容。"})
    hits = [p for p in problems(tmp_path, "IN") if "闸9" in p]
    assert "raw段行号" in hits[0]
    assert "禁止逐条改写" in hits[0], "必须禁止改写原文去迁就已生成的结论"


def test_gate9_counts_reported_in_report(tmp_path):
    write_raw(tmp_path)
    write_track_with_quotes(tmp_path, "IN", {"IN-1": "自愿参加临床试验，并签署知情同意书。", "IN-2": "编造。"})
    report = cts.check_track(tmp_path, "IN", None, False)
    assert report["原文核对"] == {"已核对": 2, "查不到": 1}


def test_gate9_cli_exit_2(tmp_path):
    write_raw(tmp_path)
    write_meta(tmp_path, 入选=1)
    write_track_with_quotes(tmp_path, "IN", {"IN-1": "编造内容。"})
    assert run(tmp_path, "--track", "IN") == 2


# ─────────── --show：按条件ID 直取，替代读行区间（token 优化）───────────
#
# 会话 `69612125`：主代理为构造 str_replace 反复 read_file 行区间，
# criteria_parsed_EX.json 被读 33 次、read_file 总量 63% 是重复读。
# --show 按 ID 直取，单条 ≈300-800 字符且不会读错位置。


def write_track_full(tmp_path, track="EX"):
    payload = {
        "四分类": {
            "排除_可从病例获取": [
                {"条件ID": "EX-1", "来源标准": "排除标准 第1条", "原文": "甲。", "子条件": "甲子"},
                {"条件ID": "EX-2", "来源标准": "排除标准 第2条", "原文": "乙。", "子条件": "乙子"},
            ],
            "排除_不可从病例获取": [
                {"条件ID": "EX-3", "来源标准": "排除标准 第3条", "原文": "丙。", "子条件": "丙子"},
            ],
        },
        "描述索引": {"EX-1": "甲述", "EX-2": "乙述", "EX-3": "丙述"},
    }
    (tmp_path / f"criteria_parsed_{track}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def test_show_single_entity(tmp_path):
    write_track_full(tmp_path)
    out = cts.show_entities(tmp_path, "EX", ["EX-2"])
    assert '"条件ID": "EX-2"' in out
    assert "乙子" in out
    assert "EX-1" not in out and "EX-3" not in out, "只应返回点名的条目"
    assert "排除_可从病例获取" in out, "须给出所在类目"
    assert '描述索引["EX-2"] = "乙述"' in out, "须附带描述索引，避免改完忘记同步"


def test_show_multiple_entities_across_categories(tmp_path):
    write_track_full(tmp_path)
    out = cts.show_entities(tmp_path, "EX", ["EX-1", "EX-3"])
    assert '"条件ID": "EX-1"' in out and '"条件ID": "EX-3"' in out
    assert "EX-2" not in out


def test_show_reports_line_number(tmp_path):
    write_track_full(tmp_path)
    out = cts.show_entities(tmp_path, "EX", ["EX-2"])
    assert "文件第" in out and "行附近" in out


def test_show_flags_missing_ids_as_possible_loss(tmp_path):
    write_track_full(tmp_path)
    out = cts.show_entities(tmp_path, "EX", ["EX-2", "EX-99"])
    assert "未找到" in out and "EX-99" in out and "丢条" in out


def test_show_output_is_much_smaller_than_range_read(tmp_path):
    """核心收益：单条输出必须显著小于读同等信息的行区间。"""
    write_track_full(tmp_path)
    out = cts.show_entities(tmp_path, "EX", ["EX-2"])
    whole = (tmp_path / "criteria_parsed_EX.json").read_text(encoding="utf-8")
    assert len(out) < len(whole) / 2, f"--show {len(out)} 未显著小于全文 {len(whole)}"


def test_show_on_invalid_json_tells_you_to_fix_syntax_first(tmp_path):
    (tmp_path / "criteria_parsed_EX.json").write_text("{ not json", encoding="utf-8")
    out = cts.show_entities(tmp_path, "EX", ["EX-1"])
    assert "JSON 不合法" in out and "先修语法" in out


def test_show_on_missing_file(tmp_path):
    assert "不存在" in cts.show_entities(tmp_path, "EX", ["EX-1"])


def test_show_cli_requires_track(tmp_path):
    write_track_full(tmp_path)
    assert cts.main(["--workspace", str(tmp_path), "--show", "EX-1"]) == 2


def test_show_cli_returns_zero_and_skips_gates(tmp_path, capsys):
    """--show 是取值模式，不应因结构问题而 exit 2。"""
    write_track_full(tmp_path)  # 无 criteria_meta.json / raw.md，闸6/闸9 会跳过或报问题
    assert cts.main(["--workspace", str(tmp_path), "--track", "EX", "--show", "EX-1"]) == 0
    assert '"条件ID": "EX-1"' in capsys.readouterr().out


# ───── 闸 10：upstream_issues 必须已中性化（thread `afb85bcd`）─────
#
# R1 把 IN-10-4/IN-10-6 正确归入 upstream_issues，主代理指示「不要伪造修复」，
# 子代理正确地完全不动 —— 但「不动」保留的是解析时写下的确定性表达（超出原文），
# 于是 R2 的 QC 把它们升级为 blocking，占掉四项阻断里的两项；R3 中性化后才回落。
# 闸 10 把「本轮立即中性化」变成机械要求。


def write_qc_upstream(workspace: Path, track: str, upstream_ids: list[str], blocking: list[str] | None = None, rnd: int = 1):
    """写带 upstream_issues 的 QC 报告。"""
    path = workspace / f"criteria_qc_{track}.json"
    path.write_text(
        json.dumps(
            {
                "track": track,
                "passed": not (blocking or []),
                "round": rnd,
                "blocking_issues": [{"id": f"CQC-B{i}", "condition_id": c} for i, c in enumerate(blocking or [])],
                "upstream_issues": [{"id": f"CQC-U{i}", "condition_id": c, "type": "原文缺词/语义歧义"} for i, c in enumerate(upstream_ids)],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def write_track_entities(workspace: Path, track: str, entities: list[dict]):
    """entities 为完整实体 dict 列表（含 条件ID / 可从病例获取 / 备注 等）。"""
    cat = "入选_可从病例获取" if track == "IN" else "排除_可从病例获取"
    (workspace / f"criteria_parsed_{track}.json").write_text(
        json.dumps(
            {
                "四分类": {cat: entities},
                "描述索引": {str(e["条件ID"]): "短描述" for e in entities},
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def _gate10(workspace: Path, track: str, qc: Path) -> list[str]:
    return [p for p in problems(workspace, track, qc=qc) if "闸10" in p]


def test_gate10_flags_untouched_upstream_entity(tmp_path):
    """ec 原始症状：条目仍是「可从病例获取 + 确定阈值」，通篇无待核实标记。"""
    write_track_entities(
        tmp_path,
        "IN",
        [
            {
                "条件ID": "IN-10-4",
                "原文": "血小板 ≥ 100×10^9/L，在 1 周内没有输注过血小板…",
                "可从病例获取": True,
                "转化条件": {"匹配字段": "血小板输注", "运算符": "=", "阈值": "无"},
                "备注": None,
            }
        ],
    )
    qc = write_qc_upstream(tmp_path, "IN", ["IN-10-4"])
    hits = _gate10(tmp_path, "IN", qc)
    assert hits, "未中性化必须被抓出"
    assert "IN-10-4" in hits[0]
    assert "本轮就要中性化" in hits[0]
    assert "中性化 ≠ 放弃" in hits[0], "必须提醒仍要回原文核实"


def test_gate10_passes_when_downgraded_to_not_obtainable(tmp_path):
    write_track_entities(tmp_path, "IN", [{"条件ID": "IN-10-4", "可从病例获取": False, "备注": "原文该句语义不完整，降级处理"}])
    qc = write_qc_upstream(tmp_path, "IN", ["IN-10-4"])
    assert not _gate10(tmp_path, "IN", qc)


def test_gate10_passes_with_pending_mark_in_remark(tmp_path):
    write_track_entities(tmp_path, "IN", [{"条件ID": "IN-10-4", "可从病例获取": True, "备注": "原文疑缺否定词，待核实原件后再定"}])
    qc = write_qc_upstream(tmp_path, "IN", ["IN-10-4"])
    assert not _gate10(tmp_path, "IN", qc)


def test_gate10_pending_mark_may_sit_anywhere_in_entity(tmp_path):
    """标记词允许出现在任意字段（如转化条件内），不限于备注。"""
    write_track_entities(
        tmp_path,
        "IN",
        [{"条件ID": "IN-10-6", "可从病例获取": True, "转化条件": {"阈值": "原文歧义，暂不编码"}}],
    )
    qc = write_qc_upstream(tmp_path, "IN", ["IN-10-6"])
    assert not _gate10(tmp_path, "IN", qc)


def test_gate10_passes_when_entity_removed(tmp_path):
    """条目被合并/删除也是一种处置。"""
    write_track_entities(tmp_path, "IN", [{"条件ID": "IN-1", "可从病例获取": True}])
    qc = write_qc_upstream(tmp_path, "IN", ["IN-10-4"])
    assert not _gate10(tmp_path, "IN", qc)


def test_gate10_reports_only_the_unhandled_ones(tmp_path):
    write_track_entities(
        tmp_path,
        "IN",
        [
            {"条件ID": "IN-10-4", "可从病例获取": False},  # 已降级
            {"条件ID": "IN-10-6", "可从病例获取": True, "转化条件": {"阈值": "无"}},  # 未处理
        ],
    )
    qc = write_qc_upstream(tmp_path, "IN", ["IN-10-4", "IN-10-6"])
    hits = _gate10(tmp_path, "IN", qc)
    assert hits and "IN-10-6" in hits[0]
    assert "'IN-10-4'" not in hits[0], "已中性化的不得被点名"
    report = cts.check_track(tmp_path, "IN", qc, False)
    assert report["upstream_未中性化"] == ["IN-10-6"]


def test_gate10_skipped_when_no_upstream_issues(tmp_path):
    write_track_entities(tmp_path, "IN", [{"条件ID": "IN-1", "可从病例获取": True}])
    qc = write_qc_upstream(tmp_path, "IN", [])
    report = cts.check_track(tmp_path, "IN", qc, False)
    assert not [p for p in report["problems"] if "闸10" in p]
    assert "upstream_未中性化" not in report


def test_gate10_skipped_without_qc(tmp_path):
    """不带 --qc 时闸 7/8/10 全部不运行 —— 这正是 afb85bcd 的失效形态。"""
    write_track_entities(tmp_path, "IN", [{"条件ID": "IN-10-4", "可从病例获取": True}])
    write_qc_upstream(tmp_path, "IN", ["IN-10-4"])
    report = cts.check_track(tmp_path, "IN", None, False)
    assert not [p for p in report["problems"] if "闸10" in p]


def test_gate10_cli_exit_2(tmp_path):
    write_track_entities(tmp_path, "IN", [{"条件ID": "IN-10-4", "可从病例获取": True, "转化条件": {"阈值": "无"}}])
    write_meta(tmp_path, 入选=10)
    qc = write_qc_upstream(tmp_path, "IN", ["IN-10-4"])
    assert run(tmp_path, "--track", "IN", "--qc", str(qc)) == 2
