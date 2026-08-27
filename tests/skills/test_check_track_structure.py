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
    """entities: {类目名: [条件ID...]}；index 省略时按 entities 的**主条件ID**自动对应。

    `描述索引` 描述的是原始标准条款（`EX-4`），不是拆分后的子条件（`EX-4-1/-2`）——
    两份 HTML 报告都按主条件ID 查它（`DESC[pid]` / `desc_index.get(pid)`）。
    """
    ids = [cid for cids in entities.values() for cid in cids]
    if index is None:
        seen: list[str] = []
        for cid in ids:
            p = cts.parse_cid(cid)
            pid = f"{p[0]}-{p[1]}" if p else cid
            if pid not in seen:
                seen.append(pid)
        index = seen
    payload = {
        "四分类": {cat: {cid: {"条件ID": cid} for cid in cids} for cat, cids in entities.items()},  # 类目规范形态：以 条件ID 为键的 dict（数组只是旧 workspace 的只读兼容形态）
        "描述索引": {cid: "短描述" for cid in index},
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


def run_report(workspace: Path, track: str, **kwargs) -> dict:
    return cts.check_track(workspace, track, kwargs.get("qc"), kwargs.get("snapshot", False))


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


# ─────────────────── 闸 5：描述索引双向对齐（按主条件ID）───────────────────


def test_gate5_accepts_parent_level_index(tmp_path):
    """`描述索引` 按**主条件ID** 索引是正确形态（`EX-2-1/-2` → `EX-2`）。

    描述索引描述的是原始标准条款，两份 HTML 报告也都按主条件ID 查它。此前本闸要求它与
    `四分类` 的子条件ID 一一对应，把正确形态判成「两套 ID 体系」，并把修复方向指向
    「按子条件ID 重写索引」——会话 9a83ccc9 因此产出 64 个子条件键、0 个主条件键，
    报告的主条件行只能回退成「首个子条件文本」，`IN-2` 显示成「年龄 ≥ 18 周岁」，
    静默丢掉了 ≤70 岁的上限。
    """
    write_track(
        tmp_path,
        "EX",
        {"排除_可从病例获取": ["EX-2-1", "EX-2-2", "EX-4-1", "EX-4-2", "EX-4-3"]},
        index=["EX-2", "EX-4"],
    )
    report = cts.check_track(tmp_path, "EX", None, False)
    assert report["miss_in_index"] == [] and report["extra_in_index"] == []
    assert not [p for p in report["problems"] if "闸5" in p]


def test_gate5_rejects_sub_condition_level_index(tmp_path):
    """按子条件ID 索引 → 主条件缺键 + 索引多余键，两侧都要报，并指明改写方向。"""
    write_track(
        tmp_path,
        "EX",
        {"排除_可从病例获取": ["EX-2-1", "EX-2-2", "EX-4-1"]},
        index=["EX-2-1", "EX-2-2", "EX-4-1"],
    )
    report = cts.check_track(tmp_path, "EX", None, False)
    assert report["miss_in_index"] == ["EX-2", "EX-4"]
    assert report["extra_in_index"] == ["EX-2-1", "EX-2-2", "EX-4-1"]
    assert any("缺键" in p and "主条件ID" in p for p in report["problems"])
    assert any("子条件ID" in p and "改写" in p for p in report["problems"])


def test_gate5_detects_missing_parent_key(tmp_path):
    """真正的丢键：拆了 EX-2/EX-4 两条却只索引了一条。"""
    write_track(
        tmp_path,
        "EX",
        {"排除_可从病例获取": ["EX-2-1", "EX-2-2", "EX-4-1"]},
        index=["EX-2"],
    )
    report = cts.check_track(tmp_path, "EX", None, False)
    assert report["miss_in_index"] == ["EX-4"]
    assert report["extra_in_index"] == []


def test_gate5_detects_index_key_without_entity(tmp_path):
    """索引里有主条件键但四分类无对应实体 → 疑似丢条。"""
    write_track(tmp_path, "IN", {"入选_可从病例获取": ["IN-1"]}, index=["IN-1", "IN-9"])
    report = cts.check_track(tmp_path, "IN", None, False)
    assert report["extra_in_index"] == ["IN-9"]
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
    # 跨类目重复 条件ID → 闸2。（同类目内重复在 dict 形态下不可表达：key 天然唯一。）
    write_track(tmp_path, "EX", {"排除_可从病例获取": ["EX-1"], "排除_不可从病例获取": ["EX-1"]})
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
    write_track(tmp_path, "EX", {"排除_可从病例获取": ["EX-1"], "排除_不可从病例获取": ["EX-1"]})  # 跨类目重复 → 闸2
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
    write_track(tmp_path, "EX", {"排除_可从病例获取": ["EX-1"], "排除_不可从病例获取": ["EX-1"]})
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
        "四分类": {cat: {cid: {"条件ID": cid, "原文": q} for cid, q in quotes.items()}},  # 类目规范形态：以 条件ID 为键的 dict（数组只是旧 workspace 的只读兼容形态）
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


# ───── 闸 11：`或组` 自洽 + `逻辑关系` 枚举 + 反向检查 ─────
#
# 旧闸 11 只遍历「`或组` 非空」的条目，`逻辑关系` 字段从不被读取。于是「该是 OR 组、
# 却标 `逻辑关系: "AND"` 且没有 `或组`」对它完全不可见——结构闸放行，问题堆到第二层
# 语义 QC 才被发现。会话 `1fee1395` 的 EX 轨 R2 阻断项就全是这一类：EX-9-1..6 与
# EX-4-1/4-2 的 AND→OR 误标，白吃掉两轨各自的轮次配额。
#
# `逻辑关系` 还是自由文本，实测取值包括 `"OR分支（同组：IN-10-OR）"` 与
# `"AND（同组：IN-10-OR…同时与IN-10-6（胆红素）AND关系）"` —— 同一个 OR 组里两条写法相反，
# 下游会把它们读成 AND（R3 的建议项 `CQC-R3-S02` 正是在报这个）。现收成枚举，
# 跨组关系说明移到 `逻辑关系备注`。


def _gate11(workspace: Path, track: str) -> list[str]:
    return [p for p in problems(workspace, track) if "闸11" in p]


def _notes11(workspace: Path, track: str) -> list[str]:
    report = cts.check_track(workspace, track, None, False)
    return [n for n in report["notes"] if "闸11" in n]


def or_member(cid: str, group: str, sem: str, logic: str = "OR分支", **extra) -> dict:
    e = {"条件ID": cid, "或组": group, "或组语义": sem, "逻辑关系": logic}
    e.update(extra)
    return e


EX_SEM = "任一触发即整条触发"
IN_SEM = "任一满足即整条满足"


# --- 既有组自洽检查（此前无测试覆盖，一并补上）---------------------


def test_gate11_accepts_well_formed_group(tmp_path):
    write_track_entities(
        tmp_path,
        "EX",
        [or_member("EX-1-1", "EX-1-OR", EX_SEM), or_member("EX-1-2", "EX-1-OR", EX_SEM)],
    )
    assert _gate11(tmp_path, "EX") == []


def test_gate11_rejects_single_member_group(tmp_path):
    write_track_entities(tmp_path, "EX", [or_member("EX-1-1", "EX-1-OR", EX_SEM)])
    hits = _gate11(tmp_path, "EX")
    assert hits and "只有 1 个成员" in hits[0]


def test_gate11_rejects_inconsistent_semantics_inside_group(tmp_path):
    write_track_entities(
        tmp_path,
        "EX",
        [or_member("EX-1-1", "EX-1-OR", EX_SEM), or_member("EX-1-2", "EX-1-OR", IN_SEM)],
    )
    assert any("组内 `或组语义` 不一致" in p for p in _gate11(tmp_path, "EX"))


def test_gate11_rejects_semantics_of_the_wrong_track(tmp_path):
    """写反会让整体入排结论反转，这是最贵的单点错误。"""
    write_track_entities(
        tmp_path,
        "IN",
        [or_member("IN-5-1", "IN-5-OR", EX_SEM), or_member("IN-5-2", "IN-5-OR", EX_SEM)],
    )
    assert any("与轨不符" in p for p in _gate11(tmp_path, "IN"))


def test_gate11_rejects_group_spanning_two_source_clauses(tmp_path):
    write_track_entities(
        tmp_path,
        "EX",
        [or_member("EX-1-1", "EX-1-OR", EX_SEM), or_member("EX-2-1", "EX-1-OR", EX_SEM)],
    )
    assert any("跨了不同原条号" in p for p in _gate11(tmp_path, "EX"))


# --- 新增：`逻辑关系` 枚举 ------------------------------------------


def test_gate11_rejects_free_text_logic_relation(tmp_path):
    """`"AND（同组：IN-10-OR…同时与IN-10-6（胆红素）AND关系）"` 这种自由文本必须被拒。"""
    write_track_entities(
        tmp_path,
        "IN",
        [
            or_member("IN-10-7", "IN-10-OR", IN_SEM, logic="OR分支（同组：IN-10-OR）"),
            or_member("IN-10-8", "IN-10-OR", IN_SEM, logic="AND（同组：IN-10-OR…同时与IN-10-6（胆红素）AND关系）"),
        ],
    )
    hits = _gate11(tmp_path, "IN")
    assert any("`逻辑关系` 取值非法" in p for p in hits), hits
    assert any("IN-10-8" in p for p in hits)
    assert any("逻辑关系备注" in p for p in hits), "必须告诉修订方跨组说明该往哪写"


@pytest.mark.parametrize("logic", ["单条件", "AND", "OR分支"])
def test_gate11_accepts_enumerated_logic_relations(tmp_path, logic):
    group = {"或组": "EX-1-OR", "或组语义": EX_SEM} if logic == "OR分支" else {}
    write_track_entities(
        tmp_path,
        "EX",
        [
            {"条件ID": "EX-1-1", "逻辑关系": logic, **group},
            {"条件ID": "EX-1-2", "逻辑关系": logic, **group},
        ],
    )
    assert _gate11(tmp_path, "EX") == []


def test_gate11_allows_cross_group_prose_in_the_remark_field(tmp_path):
    """跨组 AND 关系仍要说得清，只是搬到不参与校验的 `逻辑关系备注`。"""
    write_track_entities(
        tmp_path,
        "IN",
        [
            or_member("IN-10-7", "IN-10-OR", IN_SEM, 逻辑关系备注="与 IN-10-8 按肝转移状态互斥"),
            or_member(
                "IN-10-8",
                "IN-10-OR",
                IN_SEM,
                逻辑关系备注="同时与 IN-10-6（胆红素）为 AND 关系——肝功能三项均须满足",
            ),
        ],
    )
    assert _gate11(tmp_path, "IN") == []


def test_gate11_ignores_entities_without_logic_relation(tmp_path):
    """`逻辑关系` 缺失不由本闸管（旧产物大量缺该字段），只管写了但写错的。"""
    write_track_entities(tmp_path, "EX", [{"条件ID": "EX-1"}, {"条件ID": "EX-2"}])
    assert _gate11(tmp_path, "EX") == []


# --- 新增：反向检查（OR分支 却无 或组）------------------------------


def test_gate11_rejects_or_branch_label_without_group(tmp_path):
    write_track_entities(
        tmp_path,
        "EX",
        [{"条件ID": "EX-4-1", "逻辑关系": "OR分支"}, {"条件ID": "EX-4-2", "逻辑关系": "OR分支"}],
    )
    hits = _gate11(tmp_path, "EX")
    assert any("标了 `OR分支` 却没有 `或组`" in p for p in hits), hits
    assert any("EX-4-1" in p and "EX-4-2" in p for p in hits), "必须点名条件ID"


def test_gate11_catches_the_1fee1395_and_mislabel(tmp_path):
    """会话 `1fee1395` 的真实形态：EX-9-1..6 同出一条原文、彼此是 OR 替代分支，
    却全被标成 AND 且无 `或组` —— 旧闸完全看不见，R2 语义 QC 才报出来。

    实测该会话两轨的 `逻辑关系` **全是自由文本**（`"OR（异质替代分支，或组 EX-9-OR）"`、
    `"AND（与性别并列）"`、`"单条件"`…），所以 R2 那批 `"AND（…）"` 会先被枚举闸拦住。
    """
    write_track_entities(
        tmp_path,
        "EX",
        [
            {"条件ID": f"EX-9-{i}", "逻辑关系": "AND（与其余心血管事件并列）"}
            for i in range(1, 7)
        ],
    )
    hits = _gate11(tmp_path, "EX")
    assert any("`逻辑关系` 取值非法" in p for p in hits), hits
    assert any("EX-9-1" in p for p in hits), "必须点名条件ID"


def test_gate11_advises_when_all_and_children_share_an_enumerating_or_clause(tmp_path):
    """AND 误标的形状判据只给**建议级**：原文含列举式 OR 连接词（`和/或`/`任一`/`以下任何`…）
    而该原条号下全部子条件都标 AND、都无 `或组`。

    为什么不阻断：判据必然有假阳。真 AND 拆分的原文也可能顺带出现 `或` ——
    IN-10 的血细胞条 `…（14天内未接受输血或G-CSF…）` 就是 AND 拆分却含 `或`。
    误阻断的代价正是 Task 4 要消灭的「白吃一轮 QC 配额」。故这里只提示，由 QC 裁定。
    实测：本会话 IN 轨 33 条原文**无一**命中列举式 OR 词表，EX-9 / EX-2 命中——判据在真实
    数据上零假阳。
    """
    quote = "…未控制的高血压（收缩压≥160 mmHg和/或舒张压≥100 mmHg）、失代偿性充血性心力衰竭…"
    write_track_entities(
        tmp_path,
        "EX",
        [
            {"条件ID": "EX-9-1", "逻辑关系": "AND", "原文": quote},
            {"条件ID": "EX-9-2", "逻辑关系": "AND", "原文": quote},
        ],
    )
    notes = _notes11(tmp_path, "EX")
    assert any("EX-9" in n and "或组" in n for n in notes), notes
    assert _gate11(tmp_path, "EX") == [], "形状判据是建议级，不得阻断"


def test_gate11_bare_or_in_quote_does_not_trigger_advisory(tmp_path):
    """IN-10 血细胞条的真实反例：AND 拆分 + 原文含裸 `或`，不得提示。"""
    quote = "中性粒细胞绝对计数≥1.5×10^9/L，血小板≥100×10^9/L（14天内未接受输血或G-CSF支持）"
    write_track_entities(
        tmp_path,
        "IN",
        [
            {"条件ID": "IN-10-1", "逻辑关系": "AND", "原文": quote},
            {"条件ID": "IN-10-2", "逻辑关系": "AND", "原文": quote},
        ],
    )
    assert _notes11(tmp_path, "IN") == []


def test_gate11_advisory_skipped_when_clause_already_has_a_group(tmp_path):
    """该原条号下已有 OR 组成员 → 拆分方向已表达过，不再提示（IN-3 那种混合形态）。"""
    quote = "…经过1-2种紫衫类药物治疗方案或经研究者判断难以耐受，以下任何一项…"
    write_track_entities(
        tmp_path,
        "IN",
        [
            {"条件ID": "IN-3-1", "逻辑关系": "AND", "原文": quote},
            {"条件ID": "IN-3-2", "逻辑关系": "AND", "原文": quote},
            or_member("IN-3-3", "IN-3-OR", IN_SEM, 原文=quote),
            or_member("IN-3-4", "IN-3-OR", IN_SEM, 原文=quote),
        ],
    )
    assert _notes11(tmp_path, "IN") == []


def test_gate11_does_not_flag_genuine_and_split(tmp_path):
    """真 AND 拆分（年龄≥18 + 年龄≤70）不能被误报。"""
    write_track_entities(
        tmp_path,
        "IN",
        [
            {"条件ID": "IN-2-1", "逻辑关系": "AND", "原文": "筛选时年龄≥18周岁的男性患者；"},
            {"条件ID": "IN-2-2", "逻辑关系": "AND", "原文": "筛选时年龄≥18周岁的男性患者；"},
        ],
    )
    assert _gate11(tmp_path, "IN") == []
    assert _notes11(tmp_path, "IN") == []


def test_gate11_does_not_flag_single_and_child(tmp_path):
    """一个原条号下只有 1 个子条件，谈不上组。"""
    write_track_entities(
        tmp_path,
        "EX",
        [{"条件ID": "EX-9-1", "逻辑关系": "AND", "原文": "甲和/或乙。"}],
    )
    assert _gate11(tmp_path, "EX") == []
    assert _notes11(tmp_path, "EX") == []


# --- 既有建议级提示不回归 -------------------------------------------


def test_gate11_advises_when_many_or_branches_are_not_split(tmp_path):
    write_track_entities(
        tmp_path,
        "EX",
        [{"条件ID": "EX-7", "转化条件": {"或条件": ["甲", "乙", "丙"]}}],
    )
    notes = _notes11(tmp_path, "EX")
    assert notes and "未拆成 `或组`" in notes[0]
    assert _gate11(tmp_path, "EX") == [], "这是建议级，不得阻断"


def test_gate11_two_or_branches_do_not_trigger_advisory(tmp_path):
    """2 支常是同一事实的单位变体（睾酮 <50 ng/dL 或 <1.7 nmol/L），不提示。"""
    write_track_entities(tmp_path, "IN", [{"条件ID": "IN-4", "转化条件": {"或条件": ["甲", "乙"]}}])
    assert _notes11(tmp_path, "IN") == []


# ───── 闸 12：阈值/运算符可执行性分档（会话 `1fee1395` 的轮次空转）─────
#
# 该会话 IN 轨 R2 的 12 项阻断里 8 项是「阈值不可执行」，其中 5 项（IN-5-1/5-2/5-3、
# IN-7-1/7-2）到 R3 才被中性化为 `upstream_issues` —— 它们依赖 PCWG3 / RECIST V1.1 的
# 定义，或本质是相对比较（"与上次骨扫描相比"），**在忠实原文的前提下结构化层无解**。
# 两轮配额纯空转。
#
# 根因是规范自相矛盾：`criteria-parser/SKILL.md` 的 `转化条件` 字段定义允许
# `"阈值": 数值 或 "文字描述" 或 [离散值]`，而 `criteria-qc-checklist.md` 把复合自然语言
# 阈值列为**阻断级**。解析方照 SKILL 写，QC 照清单判阻断。
#
# 本闸只做一件可机械判定的事：把「运算符不在标准集合内」的条目点出来，附上三档判据，
# 让两轨 QC 首轮就按同一口径归档。**建议级**——到底属第二档（可结构化未结构化，阻断）
# 还是第三档（依赖外部标准，upstream）需要语义判断，闸不越权。


def _notes12(workspace: Path, track: str) -> list[str]:
    report = cts.check_track(workspace, track, None, False)
    return [n for n in report["notes"] if "闸12" in n]


def _gate12(workspace: Path, track: str) -> list[str]:
    return [p for p in problems(workspace, track) if "闸12" in p]


def transformed(cid: str, *, 运算符, 阈值=None, 子条件="x", **extra) -> dict:
    e = {
        "条件ID": cid,
        "子条件": 子条件,
        "可从病例获取": True,
        "转化条件": {"匹配字段": "字段", "运算符": 运算符, "阈值": 阈值, "同义词": ["同"], "证据位置": "病历"},
    }
    e.update(extra)
    return e


@pytest.mark.parametrize("op", ["≥", "≤", ">", "<", "=", "!=", "in", "∈", "不限"])
def test_gate12_canonical_operators_are_silent(tmp_path, op):
    """标准运算符不得产生噪声。`∈` 与 `in` 等价（实测 IN-9 用 `∈` 写 ECOG in[0,1]）。"""
    write_track_entities(tmp_path, "IN", [transformed("IN-9", 运算符=op, 阈值=[0, 1])])
    assert _notes12(tmp_path, "IN") == []
    assert _gate12(tmp_path, "IN") == []


def test_gate12_flags_non_canonical_operator(tmp_path):
    """IN-5-1 的真实形态：运算符 `进展`、阈值是一句自然语言。"""
    write_track_entities(
        tmp_path,
        "IN",
        [
            transformed(
                "IN-5-1",
                运算符="进展",
                阈值="连续2次升高，起始值≥2.0 ng/mL，间隔≥1周",
                子条件="血清/血浆PSA进展：连续2次PSA升高（至少间隔1周），最小起始值2.0 ng/mL",
            )
        ],
    )
    notes = _notes12(tmp_path, "IN")
    assert notes and "IN-5-1" in notes[0], notes
    assert "进展" in notes[0], "必须点出是哪个运算符不可执行"
    assert "upstream_issues" in notes[0], "必须给出第三档出路，否则又是一轮阻断空转"
    assert _gate12(tmp_path, "IN") == [], "分档需语义判断，闸不阻断"


def test_gate12_marks_reference_standard_dependency_as_third_tier(tmp_path):
    """命中引用型标准名（PCWG3/RECIST/CTCAE/NYHA…）→ 明确提示更可能是第三档。"""
    write_track_entities(
        tmp_path,
        "IN",
        [
            transformed(
                "IN-7-1",
                运算符="存在",
                阈值="至少1个RECIST V1.1可测量病灶",
                子条件="根据RECIST V1.1标准至少有一个可测量的病灶",
            )
        ],
    )
    notes = _notes12(tmp_path, "IN")
    assert notes and "IN-7-1" in notes[0]
    assert "RECIST" in notes[0], "须回报命中的标准名，供 QC 直接判第三档"
    assert "第三档" in notes[0]


def test_gate12_reference_standard_in_subcondition_is_enough(tmp_path):
    """IN-5-2 的标准名只出现在 `子条件`（`软组织进展（根据PCWG3和RECIST V1.1定义）`）。"""
    write_track_entities(
        tmp_path,
        "IN",
        [transformed("IN-5-2", 运算符="进展", 阈值="影像学进展", 子条件="软组织进展（根据PCWG3和RECIST V1.1定义）")],
    )
    notes = _notes12(tmp_path, "IN")
    assert any("PCWG3" in n for n in notes), notes


def test_gate12_lists_all_offenders_in_one_note(tmp_path):
    """一条 note 汇总，避免 5 条条目刷出 5 条提示淹没其它闸。"""
    write_track_entities(
        tmp_path,
        "IN",
        [
            transformed("IN-5-1", 运算符="进展", 阈值="连续2次升高"),
            transformed("IN-5-3", 运算符="进展", 阈值="至少两个新发病灶（与上次骨扫描相比）"),
            transformed("IN-7-2", 运算符="存在", 阈值="至少1处PCWG3骨转移病灶"),
        ],
    )
    notes = _notes12(tmp_path, "IN")
    assert len(notes) == 1, f"应汇总为一条，实际 {len(notes)} 条"
    for cid in ("IN-5-1", "IN-5-3", "IN-7-2"):
        assert cid in notes[0]


def test_gate12_ignores_not_obtainable_entities(tmp_path):
    """`可从病例获取=false` 的 `转化条件` 本就是 null，不在本闸范围。"""
    write_track_entities(tmp_path, "IN", [{"条件ID": "IN-1", "可从病例获取": False, "转化条件": None}])
    assert _notes12(tmp_path, "IN") == []


def test_gate12_ignores_missing_operator(tmp_path):
    """运算符缺失是另一类问题（转化条件不完整），由语义 QC 管，本闸不重复报。"""
    write_track_entities(tmp_path, "IN", [transformed("IN-3-1", 运算符=None, 阈值="有")])
    assert _notes12(tmp_path, "IN") == []


# ───── 闸 15：定性阈值 × 定量要件混用（会话 `c80c47d9` 的第三轮残留）─────
#
# `EX-12-1` 原文是「活动性乙型肝炎（HBsAg **阳性** 且 HBV-DNA **> 10^3 IU/ml**…）」——
# 一条子条件里混了定性判定与定量阈值，而 `转化条件` 只有一组 `运算符`/`阈值`，表达不了两种。
# 修订方的应对是自创一个 schema 外字段 `并列条件`，被 QC 记进 residual：
#   「『并列条件』字段未在 criteria-parser 的 转化条件 schema 中定义」
# 闸 12 查不出——`=` 是合法运算符，它只看运算符是否在标准集合内。
#
# 该条目从第 2 轮才被发现（`criteria_qc_history_EX.json` 两轮 blocking_ids 零交集），
# 到第 3 轮 `passed` 仍为 false，是本会话唯一未收敛项。
#
# 分级取舍：**阻断级**判据必须同时满足「阈值是定性词」+「原文含定量要件」两条，
# 单看任一条都会误伤（正常的 `阈值='阳性'` 条目、原文里恰好出现条号数字的条目）。
# 这与闸 11/12/14 注释里已写明的取舍一致——误阻断要白吃一轮 QC 配额，比漏报更贵。


def _gate15(workspace: Path, track: str) -> list[str]:
    return [p for p in problems(workspace, track) if "闸15" in p]


def _notes15(workspace: Path, track: str) -> list[str]:
    report = cts.check_track(workspace, track, None, False)
    return [n for n in report["notes"] if "闸15" in n]


def test_gate15_blocks_qualitative_threshold_with_quantitative_source(tmp_path):
    """EX-12-1 的真实形态：阈值『阳性』作用于含数值型字段的匹配字段，原文带 `> 10^3 IU/ml`。"""
    write_track_entities(
        tmp_path,
        "EX",
        [
            transformed(
                "EX-12-1",
                运算符="=",
                阈值="阳性",
                子条件="活动性乙型肝炎（HBsAg阳性且HBV-DNA>10^3IU/ml，或符合研究中心诊断活动性乙肝感染标准）",
                原文="活动性乙型肝炎（HBsAg阳性且HBV-DNA>10^3IU/ml）或活动性丙型肝炎。",
            )
        ],
    )
    got = _gate15(tmp_path, "EX")
    assert got, "定性阈值 + 原文定量要件必须阻断"
    assert "EX-12-1" in got[0], got
    assert "阳性" in got[0], "必须点出是哪个定性阈值"
    assert "parsing-rules" in got[0], "必须给出拆分出路，否则修订方会再自创字段"


def test_gate15_shared_source_text_does_not_contaminate_siblings(tmp_path):
    """同一原条号的子条件**共享**整段 `原文` —— 定量要件只属其中一条时不得连坐。

    实测反例（会话 `c80c47d9` 的 EX-12 四条）：`原文` 是「乙肝 HBsAg阳性且HBV-DNA>10^3IU/ml
    / 丙肝 / HIV / 梅毒」整段，四条子条件共用它（忠实原文的正确做法）。定量要件只在乙肝那支。
    按 `原文` 判会把 EX-12-2/3/4 一起误报 —— 3 条误阻断就是一轮 QC 配额白烧。
    """
    shared = "筛选时，受试者存在以下病毒性感染中的一种：·活动性乙型肝炎：HBsAg阳性且HBV-DNA>10^3IU/ml。·活动性丙型肝炎：HCV抗体阳性且HCV-RNA阳性。·活动性梅毒：梅毒螺旋体抗体阳性。"
    write_track_entities(
        tmp_path,
        "EX",
        [
            transformed("EX-12-1", 运算符="=", 阈值="阳性", 子条件="活动性乙型肝炎（HBsAg阳性且HBV-DNA>10^3IU/ml）", 原文=shared),
            transformed("EX-12-2", 运算符="=", 阈值="阳性", 子条件="活动性丙型肝炎（HCV抗体阳性且HCV-RNA阳性）", 原文=shared),
            transformed("EX-12-4", 运算符="=", 阈值="阳性", 子条件="活动性梅毒（梅毒螺旋体抗体阳性）", 原文=shared),
        ],
    )
    got = _gate15(tmp_path, "EX")
    assert len(got) == 1, got
    assert "EX-12-1" in got[0], "定量要件在本条 `子条件` 里 → 应报"
    assert "EX-12-2" not in got[0] and "EX-12-4" not in got[0], f"共享原文不得连坐：{got[0]}"


def test_gate15_falls_back_to_source_text_without_subcondition(tmp_path):
    """缺 `子条件` 时退回 `原文` —— 否则该条会完全逃过本闸。"""
    write_track_entities(
        tmp_path,
        "EX",
        [transformed("EX-20", 运算符="=", 阈值="阳性", 子条件=None, 原文="ALT阳性且总胆红素>1.5倍正常上限。")],
    )
    assert "EX-20" in "".join(_gate15(tmp_path, "EX"))


def test_gate15_silent_when_threshold_is_purely_qualitative(tmp_path):
    """`阈值='阳性'` 但原文无定量要件 → 正常条目，不得阻断（§6.2 的假阳防线）。"""
    write_track_entities(
        tmp_path,
        "EX",
        [
            transformed(
                "EX-12-3",
                运算符="=",
                阈值="阳性",
                子条件="已知HIV感染（HIV抗体阳性）",
                原文="已知人类免疫缺陷病毒（HIV）感染者。",
            )
        ],
    )
    assert _gate15(tmp_path, "EX") == []


def test_gate15_digits_in_clause_number_are_not_quantitative(tmp_path):
    """原文里的条号/纯数字不构成定量要件——否则几乎每条都会误命中。"""
    write_track_entities(
        tmp_path,
        "EX",
        [
            transformed(
                "EX-12-4",
                运算符="=",
                阈值="阳性",
                子条件="活动性梅毒（梅毒螺旋体抗体阳性）",
                原文="排除标准第 12 条第 4 项：活动性梅毒。",
            )
        ],
    )
    assert _gate15(tmp_path, "EX") == []


def test_gate15_silent_when_threshold_is_numeric(tmp_path):
    """定量阈值 + 定量原文是本该有的形态，不得报。"""
    write_track_entities(
        tmp_path,
        "EX",
        [
            transformed(
                "EX-12-1b",
                运算符=">",
                阈值=1000,
                子条件="HBV-DNA > 10^3 IU/ml",
                原文="HBV-DNA>10^3IU/ml。",
            )
        ],
    )
    assert _gate15(tmp_path, "EX") == []


def test_gate15_ignores_not_obtainable_entities(tmp_path):
    """`可从病例获取=false` 的 `转化条件` 是 null，不在本闸范围。"""
    write_track_entities(tmp_path, "EX", [{"条件ID": "EX-1-2", "可从病例获取": False, "转化条件": None, "原文": "经研究者评估适合接受治疗，且ALT>3倍。"}])
    assert _gate15(tmp_path, "EX") == []


def test_gate15_lists_all_offenders(tmp_path):
    """多条命中要一次点全，别让 agent 修一条跑一轮。"""
    write_track_entities(
        tmp_path,
        "EX",
        [
            transformed("EX-12-1", 运算符="=", 阈值="阳性", 子条件="HBsAg阳性且HBV-DNA>10^3IU/ml"),
            transformed("EX-13-1", 运算符="=", 阈值="阳性", 子条件="HCV抗体阳性且HCV-RNA≥15IU/ml"),
        ],
    )
    got = _gate15(tmp_path, "EX")
    assert len(got) == 1, "一条 problem 点全部命中，不要每条一行"
    assert "EX-12-1" in got[0] and "EX-13-1" in got[0], got


# ───── 闸 16（建议级）：跨 `可从病例获取` 边界的 AND ─────
#
# `EX-1` 的原文是「MSI-H/dMMR（客观检测） 且 经研究者评估适合免疫治疗（主观）」。这个 AND
# **必拆**：不拆则整条只能标一个 `可从病例获取`，标 true 会让判定侧去病历找研究者评估
# （必然「无法判断」），标 false 会丢掉可客观核验的 MSI 检测。
#
# 会话 `c80c47d9` 里解析子代理**自己想明白了**（`逻辑关系备注` 写着"客观检测与研究者评估按
# 可获取性拆分"），但那是判断力而非规则保证：`parsing-rules.md` 的 AND 例外只写了
# 「限定性 AND 不拆」，按字面 `EX-1` 完全可以被当成限定性 AND 留在一条内。
#
# 只做**建议级**：判定"原文是否含主观评估要件"需要词表，词表必有假阳。已拆的正确形态
# （AND 兄弟的 `可从病例获取` 取值不一致）必须静默，否则正确做法反而被报错。


def _notes16(workspace: Path, track: str) -> list[str]:
    report = cts.check_track(workspace, track, None, False)
    return [n for n in report["notes"] if "闸16" in n]


def test_gate16_silent_when_and_siblings_span_obtainability(tmp_path):
    """EX-1-1/EX-1-2 已按可获取性拆开 —— 这是正确形态，必须静默。"""
    write_track_entities(
        tmp_path,
        "EX",
        [
            {
                "条件ID": "EX-1-1",
                "子条件": "结直肠癌原发灶或转移灶为MSI-H或dMMR",
                "原文": "已知为MSI-H或dMMR，且经研究者评估适合接受免疫检查点抑制剂治疗的患者。",
                "逻辑关系": "AND",
                "可从病例获取": True,
                "转化条件": {"匹配字段": "MSI状态", "运算符": "=", "阈值": "MSI-H", "同义词": ["MSI-H"], "证据位置": "病理"},
            },
            {
                "条件ID": "EX-1-2",
                "子条件": "经研究者评估适合接受免疫检查点抑制剂治疗",
                "原文": "已知为MSI-H或dMMR，且经研究者评估适合接受免疫检查点抑制剂治疗的患者。",
                "逻辑关系": "AND",
                "可从病例获取": False,
                "转化条件": None,
            },
        ],
    )
    assert _notes16(tmp_path, "EX") == []


def test_gate16_flags_unsplit_subjective_and(tmp_path):
    """单条 AND 的原文同时含客观检测与主观评估要件，却只有一个实体 → 建议复核。"""
    write_track_entities(
        tmp_path,
        "EX",
        [
            transformed(
                "EX-1",
                运算符="=",
                阈值="MSI-H",
                子条件="MSI-H或dMMR且经研究者评估适合免疫治疗",
                原文="已知为MSI-H或dMMR，且经研究者评估适合接受免疫检查点抑制剂治疗的患者。",
                逻辑关系="AND",
            )
        ],
    )
    notes = _notes16(tmp_path, "EX")
    assert notes and "EX-1" in notes[0], notes
    assert "可从病例获取" in notes[0], "必须说明为什么要拆"
    assert _gate15(tmp_path, "EX") == [], "本闸只给建议，不得阻断"


def test_gate16_silent_without_subjective_marker(tmp_path):
    """原文没有主观评估要件的 AND 不在本闸范围。"""
    write_track_entities(
        tmp_path,
        "EX",
        [
            transformed(
                "EX-3-1",
                运算符="≥",
                阈值=3,
                子条件="ALT≥3倍正常上限",
                原文="ALT≥3倍正常上限，且AST≥3倍正常上限。",
                逻辑关系="AND",
            )
        ],
    )
    assert _notes16(tmp_path, "EX") == []


# ═══════════════════════════════════════════════════════════════════
# Task 12/13：闸9 失配诊断化 + 归一化扩展 + OR 分段匹配
# ═══════════════════════════════════════════════════════════════════
#
# 故障（`d393714d`）：EX 轨解析 863k/23 步 → **2.81M/54 步**，3 倍恶化。闸9 只说"查不到"，
# agent 于是反复 read_file 脚本源码逆推归一化规则、再逐字符比对 raw。两个真因：
#   ① NFKC **不折叠**的视觉等价字符（`·`、`–—‐`、零宽、中英引号）——全角 `＞` 其实早就被折叠了，
#      之前的分析找错了对象；
#   ② `a) …… 或 b) ……` 分支列表在原方案里跨行排布，拼进一条 `原文` 后不再是连续子串。

RAW_OR = """# 4.2 排除标准

3.  存在下列任一情况者：a) 6 个月内发生过心肌梗死或不稳定性心绞痛；
    b) 需要药物干预的充血性心力衰竭；c) 未能控制的高血压（收缩压≥160 mmHg）。

4.  已知对研究药物任一辅料过敏。
"""


class TestGate9NormalizationFolding:
    """视觉等价但码位不同的字符不得判为改写。"""

    def _assert_passes(self, tmp_path, raw: str, quote: str, why: str):
        write_raw(tmp_path, raw)
        write_track_with_quotes(tmp_path, "IN", {"IN-1": quote})
        assert not [p for p in problems(tmp_path, "IN") if "闸9" in p], why

    def test_interpunct_variants_fold(self, tmp_path):
        self._assert_passes(
            tmp_path,
            "1. 既往接受过 PD-1・PD-L1 抑制剂治疗。\n",
            "既往接受过 PD-1·PD-L1 抑制剂治疗。",  # U+00B7 vs U+30FB
            "间隔号族必须折叠",
        )

    def test_dash_family_folds(self, tmp_path):
        self._assert_passes(
            tmp_path,
            "1. 年龄 18—70 周岁。\n",
            "年龄 18-70 周岁。",  # U+2014 vs ASCII -
            "破折号族必须折叠",
        )

    def test_tilde_range_folds(self, tmp_path):
        self._assert_passes(
            tmp_path,
            "1. 体重 40～100 kg。\n",
            "体重 40~100 kg。",  # U+FF5E vs ASCII ~
            "波浪号族必须折叠",
        )

    def test_zero_width_chars_are_dropped(self, tmp_path):
        self._assert_passes(
            tmp_path,
            "1. 自愿参加临床试验，并签署知情同意书。\n",
            "自愿参加\u200b临床试验，\ufeff并签署知情同意书。",
            "零宽字符必须删除",
        )

    def test_curly_quotes_fold(self, tmp_path):
        self._assert_passes(
            tmp_path,
            '1. 研究者判断为“不适合入组”者。\n',
            '研究者判断为"不适合入组"者。',
            "中英引号必须折叠",
        )

    def test_comparison_operators_are_not_folded(self, tmp_path):
        """⛔ 反向底线：`≥3 个月` 改写成 `>3 个月` 是**真实篡改**，绝不能折叠。"""
        write_raw(tmp_path, "1. 预期生存期≥3 个月。\n")
        write_track_with_quotes(tmp_path, "IN", {"IN-1": "预期生存期>3 个月。"})
        assert [p for p in problems(tmp_path, "IN") if "闸9" in p], "比较符差异必须仍被拦"

    def test_digits_are_not_folded(self, tmp_path):
        write_raw(tmp_path, "1. 年龄 18 周岁以上。\n")
        write_track_with_quotes(tmp_path, "IN", {"IN-1": "年龄 19 周岁以上。"})
        assert [p for p in problems(tmp_path, "IN") if "闸9" in p], "数字被改必须仍被拦"


class TestGate9OrSegmentMatching:
    def _run(self, tmp_path, quote: str, track: str = "EX"):
        write_raw(tmp_path, RAW_OR)
        write_track_with_quotes(tmp_path, track, {f"{track}-3": quote})
        return problems(tmp_path, track)

    def test_cross_line_or_branches_pass(self, tmp_path):
        """三个分支跨行排布，拼接后不是连续子串，但逐段命中且顺序一致 → 放行。"""
        quote = (
            "6 个月内发生过心肌梗死或不稳定性心绞痛；"
            "需要药物干预的充血性心力衰竭；"
            "未能控制的高血压（收缩压≥160 mmHg）"
        )
        assert not [p for p in self._run(tmp_path, quote) if "闸9" in p]

    def test_or_joined_pass_is_recorded_in_notes(self, tmp_path):
        """放行 ≠ 无声：跨行拼接必须留痕，否则真拼接与真改写在报告里没有区别。"""
        quote = (
            "6 个月内发生过心肌梗死或不稳定性心绞痛；"
            "需要药物干预的充血性心力衰竭；"
            "未能控制的高血压（收缩压≥160 mmHg）"
        )
        write_raw(tmp_path, RAW_OR)
        write_track_with_quotes(tmp_path, "EX", {"EX-3": quote})
        report = run_report(tmp_path, "EX")
        assert report["原文核对"].get("OR分段通过") == ["EX-3"]
        assert any("跨行拼接" in n for n in report["notes"])

    def test_out_of_order_segments_are_rejected(self, tmp_path):
        """乱序拼接是**语义篡改**（把 c) 的内容挂到 a) 的条件下），必须拦。"""
        quote = (
            "未能控制的高血压（收缩压≥160 mmHg）；"
            "6 个月内发生过心肌梗死或不稳定性心绞痛；"
            "需要药物干预的充血性心力衰竭"
        )
        assert [p for p in self._run(tmp_path, quote) if "闸9" in p], "乱序必须被拦"

    def test_short_segments_are_rejected(self, tmp_path):
        """短段在长文里几乎必然能找到，放过短段等于不设闸。"""
        assert [p for p in self._run(tmp_path, "过敏；心衰；高血压") if "闸9" in p]

    def test_too_many_segments_are_rejected(self, tmp_path):
        """段数越多越接近"把改写后的句子剁碎后逐块碰运气"。"""
        seg = "需要药物干预的充血性心力衰竭"
        assert [p for p in self._run(tmp_path, "；".join([seg] * 7)) if "闸9" in p]

    def test_fabricated_segment_among_real_ones_is_rejected(self, tmp_path):
        """反向底线：真分支里混一条编造的，仍必须拦。"""
        quote = (
            "6 个月内发生过心肌梗死或不稳定性心绞痛；"
            "需要药物干预的充血性心力衰竭；"
            "既往接受过两线以上系统性抗肿瘤治疗"  # 原文没有
        )
        assert [p for p in self._run(tmp_path, quote) if "闸9" in p]

    def test_enumeration_comma_is_not_a_split_char(self, tmp_path):
        """⛔ `、` 做并列，拿它切段会把任何长句拆成必然命中的碎片 → 本闸失效。"""
        write_raw(tmp_path, "1. 肝、肾功能显著异常者。\n")
        write_track_with_quotes(tmp_path, "IN", {"IN-1": "心、肺、脑功能显著异常者，另需评估者"})
        assert [p for p in problems(tmp_path, "IN") if "闸9" in p]


class TestGate9MismatchDiagnostics:
    def _diag(self, tmp_path, raw: str, quote: str, cid: str = "IN-1") -> dict:
        write_raw(tmp_path, raw)
        write_track_with_quotes(tmp_path, "IN", {cid: quote})
        return run_report(tmp_path, "IN")["原文失配定位"][cid]

    def test_reports_first_mismatch_offset_and_matched_prefix(self, tmp_path):
        diag = self._diag(tmp_path, "1. 预期生存期≥3 个月。\n", "预期生存期≥6 个月。")
        assert diag["首个失配偏移"] == len("预期生存期≥")
        assert diag["最长匹配前缀"].endswith("预期生存期≥")
        assert "6" in diag["失配处"]

    def test_reports_nearest_raw_fragment_with_the_real_characters(self, tmp_path):
        """诊断的价值在于**直接给出 raw 的原字符**，agent 照抄即可。"""
        diag = self._diag(tmp_path, "1. 既往接受过≥2 线系统性抗肿瘤治疗者。\n", "既往接受过>2 线系统性抗肿瘤治疗者。")
        assert "≥2 线系统性抗肿瘤治疗" in diag["raw最相近片段"].replace(" ", "") or "≥" in diag["raw最相近片段"]
        assert "照抄" in diag["建议"]

    def test_single_char_tamper_in_a_short_quote_is_not_called_fabrication(self, tmp_path):
        """把 `≥3 个月` 写成 `>3 个月` 只需改一个字符，⛔ 不得建议"整轨重做"。

        实跑回放时发现的真实误导：`raw最相近片段` 用了「≥8 字」的绝对阈值，短 `原文` 的公共段
        天然不足 8 字（这里是 `预期生存期`，5 字）→ 被判成凭空生成 → 把一个字符的活儿升级成整轨重做。
        """
        diag = self._diag(tmp_path, "1. 预期生存期≥3 个月。\n", "预期生存期>3 个月。")
        assert diag["raw最相近片段"], "短引文的相近片段必须给出来"
        assert "照抄" in diag["建议"]
        assert "整轨重做" not in diag["建议"]

    def test_fabricated_quote_is_told_to_reparse_not_to_tweak_characters(self, tmp_path):
        """完全编造时不能建议"改字符"——那会把 agent 推进逐字符循环。"""
        diag = self._diag(tmp_path, "1. 自愿参加临床试验，并签署知情同意书。\n", "已知存在 Gilbert 综合征且胆红素显著升高者")
        assert diag["raw最相近片段"] == ""
        assert "整轨重做" in diag["建议"]

    def test_or_shaped_quote_gets_the_segment_rule_hint(self, tmp_path):
        """带分隔符但没通过分段规则时，要告诉 agent 分段规则的三条约束。"""
        write_raw(tmp_path, RAW_OR)
        write_track_with_quotes(tmp_path, "EX", {"EX-3": "过敏；心衰；高血压"})
        diag = run_report(tmp_path, "EX")["原文失配定位"]["EX-3"]
        assert "分支" in diag["建议"] and "8" in diag["建议"]

    def test_problem_text_points_at_the_diagnosis_block(self, tmp_path):
        """报告正文必须把 agent 引到诊断块，并明说不用读脚本源码。"""
        write_raw(tmp_path)
        write_track_with_quotes(tmp_path, "IN", {"IN-1": "编造内容"})
        hit = [p for p in problems(tmp_path, "IN") if "闸9" in p][0]
        assert "失配定位" in hit and "脚本源码" in hit

    def test_diagnostics_are_capped_to_keep_the_report_small(self, tmp_path):
        """报告自己不能变成上下文炸弹（54 步循环的一部分就是反复读大报告）。"""
        write_raw(tmp_path)
        write_track_with_quotes(tmp_path, "IN", {f"IN-{i}": f"编造内容第{i}条足够长的句子。" for i in range(1, 21)})
        diag = run_report(tmp_path, "IN")["原文失配定位"]
        assert len(diag) == 5


# ─────────── 改一条查一条：--only（会话 a7c19ea1 的 28 次全量闸）───────────


class TestOnlyNarrowsTheReport:
    """单任务 28 次全量闸、编辑只有 10 次 —— 「改一条 → 跑全闸 → 在整份报告里找自己那条」。

    全量闸的输出本身就是大段上下文，往返成本远高于改一条，于是 901 秒 / 258 万 token。
    """

    def test_only_keeps_the_named_condition(self):
        problems = [
            "闸11 EX-5-1 `或组` 成员数不足 2",
            "闸12 EX-9-2 `运算符` 不在标准集合内",
        ]
        assert cts.filter_problems_to_ids(problems, ["EX-5-1"]) == [problems[0]]

    def test_only_accepts_several_ids(self):
        problems = ["闸11 EX-5-1 ...", "闸12 EX-9-2 ...", "闸12 EX-3-4 ..."]
        assert cts.filter_problems_to_ids(problems, ["EX-5-1", "EX-3-4"]) == [problems[0], problems[2]]

    def test_file_level_problems_always_survive(self):
        """「只查这一条」绝不能把「文件已经坏了」一起过滤掉 —— 那比不查更危险。"""
        problems = [
            "闸1 缺少 `四分类` 顶层字段",
            "闸13 类目容器形态不合规",
            "闸12 EX-9-2 `运算符` 不在标准集合内",
        ]
        assert cts.filter_problems_to_ids(problems, ["EX-5-1"]) == problems[:2]

    def test_empty_selection_is_a_no_op(self):
        problems = ["闸12 EX-9-2 ..."]
        assert cts.filter_problems_to_ids(problems, []) == problems
        assert cts.filter_problems_to_ids(problems, [" "]) == problems

    def test_only_requires_track(self, tmp_path):
        write_track(tmp_path, "EX", {"排除_可从病例获取": ["EX-1"]})
        with pytest.raises(SystemExit):
            run(tmp_path, "--only", "EX-1")

    def test_only_does_not_write_the_gate_artifact(self, tmp_path):
        """产物是「本轨已过闸」的凭据，下游都读它；单条校验去覆盖等于伪造全轨结论。"""
        write_raw(tmp_path)
        write_track(tmp_path, "EX", {"排除_可从病例获取": ["EX-1"]})
        run(tmp_path, "--track", "EX", "--only", "EX-1")
        assert not (tmp_path / "criteria_structure_gate_EX.json").exists()

    def test_full_run_still_writes_the_gate_artifact(self, tmp_path):
        write_raw(tmp_path)
        write_track(tmp_path, "EX", {"排除_可从病例获取": ["EX-1"]})
        run(tmp_path, "--track", "EX")
        assert (tmp_path / "criteria_structure_gate_EX.json").exists()

    def test_only_output_names_what_it_reported(self, tmp_path, capsys):
        write_raw(tmp_path)
        write_track(tmp_path, "EX", {"排除_可从病例获取": ["EX-1"]})
        run(tmp_path, "--track", "EX", "--only", "EX-1")
        out = capsys.readouterr().out
        assert "--only" in out and "EX-1" in out


# ─────────── --qc 漏值不该烧一轮模型往返（a7c19ea1，lead 与子代理各撞一次）───────────


class TestContractIsPrintableWithoutReadingSource:
    """a7c19ea1：修订子代理 grep 了 9 次本脚本源码，问的全是这里几行常量。

    从常量现取现印，不手写 —— 手写那份一定会漂移，届时比没有更糟。
    """

    def test_contract_lists_every_enforced_value_set(self):
        text = cts.contract_text()
        for op in cts.CANONICAL_OPERATORS:
            assert op in text
        for rel in cts.LOGIC_RELATIONS:
            assert rel in text
        for mark in cts.UPSTREAM_PENDING_MARKS:
            assert mark in text
        for sem in cts.OR_GROUP_SEMANTICS.values():
            assert sem in text

    def test_contract_points_at_the_reference_docs_not_the_source(self):
        text = cts.contract_text()
        assert "criteria-repair.md" in text and "criteria-qc-checklist.md" in text
        assert "不要读本脚本源码" in text

    def test_contract_runs_without_a_workspace(self, capsys):
        """它不读 workspace；要求填一个只会让 agent 多猜一次路径。"""
        assert cts.main(["--contract"]) == 0
        assert "标准集合" in capsys.readouterr().out

    def test_workspace_is_still_required_for_every_other_path(self):
        with pytest.raises(SystemExit):
            cts.main(["--track", "EX"])


class TestQcPathIsOptional:
    def test_bare_qc_flag_falls_back_to_the_conventional_path(self, tmp_path):
        """原为必填值：漏值时 argparse exit 2 打印用法，每次白烧一轮完整往返。"""
        write_raw(tmp_path)
        write_track(tmp_path, "EX", {"排除_可从病例获取": ["EX-1"]})
        write_qc(tmp_path, "EX", ["EX-1"])
        assert run(tmp_path, "--track", "EX", "--qc") == 0

    def test_explicit_qc_path_still_honoured(self, tmp_path):
        write_raw(tmp_path)
        write_track(tmp_path, "EX", {"排除_可从病例获取": ["EX-1"]})
        qc = write_qc(tmp_path, "EX", ["EX-1"])
        assert run(tmp_path, "--track", "EX", "--qc", str(qc)) == 0

    def test_qc_still_requires_track(self, tmp_path):
        write_track(tmp_path, "EX", {"排除_可从病例获取": ["EX-1"]})
        with pytest.raises(SystemExit):
            run(tmp_path, "--qc")
