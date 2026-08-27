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

BARE_IN_SHARD = {
    "patient_id": "M018",
    "judgments": {
        "IN-7-1": {"conclusion": "无法判断", "reason": "缺 RECIST 基线评估", "evidence": [], "matching": {}},
        "IN-7-2": {"conclusion": "符合", "reason": "ECT 全身多发骨转移", "evidence": [], "matching": {}},
        "IN-9": {"conclusion": "无法判断", "reason": "缺 ECOG 评分", "evidence": [], "matching": {}},
    },
}

# 标准包（`criteria_judge_IN.json` 的形态：保留 四分类 外层结构）
IN_CRITERIA_PACK = {
    "四分类": {
        "入选_可从病例获取": [
            {"条件ID": "IN-7-1", "子条件": "RECIST V1.1 可测量病灶", "逻辑关系": "OR分支", "或组": "IN-7-OR", "或组语义": "任一满足即整条满足"},
            {"条件ID": "IN-7-2", "子条件": "PCWG3 骨转移病灶", "逻辑关系": "OR分支", "或组": "IN-7-OR", "或组语义": "任一满足即整条满足"},
            {"条件ID": "IN-9", "子条件": "ECOG 0 或 1", "逻辑关系": "单条件"},
        ]
    }
}
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


# --- slim（按轨精简） --------------------------------------------------------


# --- assemble（双轨合成全量包） ----------------------------------------------


# --- merge-judgments -------------------------------------------------------


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


def test_merge_judgments_unified_top_level():
    """统一证据源判定：两轨顶层 judgments 合并，无 documents 维度，summary 重算。"""
    merged = pack.merge_judgments([IN_SHARD, EX_SHARD])
    assert "documents" not in merged
    judgments = merged["judgments"]
    assert set(judgments) == {"IN-1", "IN-2-1", "IN-10-2", "EX-4", "EX-15"}
    assert merged["patient_id"] == "M016_ZALO"
    assert merged["patient_name"] == "张安龙"
    assert merged["warnings"] == ["资料缺少筛选访视表"]  # 去重


def test_merge_judgments_preserves_conclusions_verbatim():
    merged = pack.merge_judgments([IN_SHARD, EX_SHARD])
    ex15 = merged["judgments"]["EX-15"]
    assert ex15 == {"conclusion": "符合", "exclusion_triggered": False, "reason": "全阴性，未触发排除条件"}


def test_merge_judgments_recomputes_summary():
    merged = pack.merge_judgments([IN_SHARD, EX_SHARD])
    assert merged["summary"] == {"符合": 3, "不符合": 0, "存疑": 1, "无法判断": 1}


def test_merge_judgments_sorts_in_before_ex_and_numerically():
    merged = pack.merge_judgments([EX_SHARD, IN_SHARD])
    assert list(merged["judgments"]) == ["IN-1", "IN-2-1", "IN-10-2", "EX-4", "EX-15"]


def test_merge_judgments_conflicting_cid_between_tracks_is_warned():
    """两轨同名条件ID（本不该发生）→ 合并出声而不是静默覆盖。"""
    a = {"patient_id": "P", "judgments": {"IN-1": {"conclusion": "符合", "reason": "a"}}}
    b = {"patient_id": "P", "judgments": {"IN-1": {"conclusion": "不符合", "reason": "b"}}}
    merged = pack.merge_judgments([a, b])
    assert merged["judgments"]["IN-1"]["conclusion"] == "符合"  # 先到者保留
    assert any("IN-1" in w for w in (merged.get("warnings") or []) + (merged.get("rollup_warnings") or []))


# --- merge-judgments：主条件组级汇总（criteria_rollup） ----------------------
#
# 子条件判定回答「IN-10-3 达标吗」，读者要的是「入选标准第 10 条整体达标吗」。
# 折叠算法在 scripts/rollup.py（真值表见 tests/skills/test_judgment_rollup.py），
# 这里只验证「合并阶段确实产出了它、且没有把判定本身弄脏」。

ROLLUP_IN_SHARD = {
    "patient_id": "M017",
    "judgments": {
                # 或组：一支符合 + 两支无法判断 → 整条符合（防错误淘汰患者）
                "IN-5-1": {"conclusion": "符合", "或组": "IN-5-OR", "或组语义": "任一满足即整条满足", "reason": "PSA 进展"},
                "IN-5-2": {"conclusion": "无法判断", "或组": "IN-5-OR", "或组语义": "任一满足即整条满足", "reason": "无软组织评估"},
                "IN-5-3": {"conclusion": "无法判断", "或组": "IN-5-OR", "或组语义": "任一满足即整条满足", "reason": "无骨扫描"},
                # 并列子条件：AND 折叠，存疑压过无法判断
                "IN-10-1": {"conclusion": "符合", "reason": "ANC 3.1"},
                "IN-10-2": {"conclusion": "存疑", "reason": "PLT 边界值"},
                "IN-10-3": {"conclusion": "无法判断", "reason": "缺 CrCl"},
            },
}

ROLLUP_EX_SHARD = {
    "patient_id": "M017",
    "judgments": {
                # 或组：任一支触发即整条触发
                "EX-1-1": {"conclusion": "符合", "exclusion_triggered": False, "或组": "EX-1-OR", "或组语义": "任一触发即整条触发", "reason": "未触发该排除条件"},
                "EX-1-2": {"conclusion": "不符合", "exclusion_triggered": True, "或组": "EX-1-OR", "或组语义": "任一触发即整条触发", "reason": "触发该排除条件"},
                "EX-6": {"conclusion": "符合", "exclusion_triggered": False, "reason": "未触发该排除条件"},
            },
}


def test_merge_judgments_writes_criteria_rollup_beside_judgments():
    """⛔ 汇总必须与 judgments 平级：塞进 judgments 会让结构闸闸 2 直接 exit 2。"""
    merged = pack.merge_judgments([ROLLUP_IN_SHARD, ROLLUP_EX_SHARD])
    assert "criteria_rollup" in merged
    assert "rollup_summary" in merged
    # judgments 键集合只含子条件ID，未被主条件ID 污染（闸 2 语义不破）
    assert set(merged["judgments"]) == {
        "IN-5-1",
        "IN-5-2",
        "IN-5-3",
        "IN-10-1",
        "IN-10-2",
        "IN-10-3",
        "EX-1-1",
        "EX-1-2",
        "EX-6",
    }
    assert not {"IN-5", "IN-10", "EX-1"} & set(merged["judgments"])


def test_merge_judgments_rollup_covers_both_tracks_in_natural_order():
    merged = pack.merge_judgments([ROLLUP_EX_SHARD, ROLLUP_IN_SHARD])
    table = merged["criteria_rollup"]
    assert list(table) == ["IN-5", "IN-10", "EX-1", "EX-6"]


def test_merge_judgments_rollup_conclusions_follow_track_semantics():
    merged = pack.merge_judgments([ROLLUP_IN_SHARD, ROLLUP_EX_SHARD])
    table = merged["criteria_rollup"]
    assert table["IN-5"]["conclusion"] == "符合"  # 或组任一满足即整条满足
    assert table["IN-5"]["decided_by"] == ["IN-5-1"]
    assert table["IN-10"]["conclusion"] == "存疑"  # AND：存疑 > 无法判断
    assert table["EX-1"]["conclusion"] == "不符合"  # 或组任一触发即整条触发
    assert table["EX-6"]["conclusion"] == "符合"
    assert table["EX-6"]["rule"] == "单条"


def test_merge_judgments_rollup_summary_counts_parents_not_sub_conditions():
    merged = pack.merge_judgments([ROLLUP_IN_SHARD, ROLLUP_EX_SHARD])
    # 子条件口径：9 条
    assert sum(merged["summary"].values()) == 9
    # 主条件口径：4 条（IN-5 / IN-10 / EX-1 / EX-6）
    assert sum(merged["rollup_summary"].values()) == 4
    assert merged["rollup_summary"] == {"符合": 2, "不符合": 1, "存疑": 1, "无法判断": 0}


def test_merge_judgments_rollup_does_not_mutate_judgment_entries():
    """合并是机械操作：汇总不得改结论/理由/证据/方向字段任何一个字节。"""
    before = json.loads(json.dumps([ROLLUP_IN_SHARD, ROLLUP_EX_SHARD]))
    merged = pack.merge_judgments([ROLLUP_IN_SHARD, ROLLUP_EX_SHARD])
    judgments = merged["judgments"]
    for shard in before:
        for cid, entry in shard["judgments"].items():
            assert judgments[cid] == entry, f"{cid} 被合并阶段改动了"


def test_merge_judgments_rollup_is_idempotent():
    """重复合并（含已带 rollup 的输入）结果一致，不会累积或残留陈旧值。"""
    once = pack.merge_judgments([ROLLUP_IN_SHARD, ROLLUP_EX_SHARD])
    twice = pack.merge_judgments([json.loads(json.dumps(once))])
    assert twice["criteria_rollup"] == once["criteria_rollup"]
    assert twice["rollup_summary"] == once["rollup_summary"]


def test_merge_judgments_rollup_overwrites_stale_rollup():
    """输入里带着与判定不符的旧汇总时，必须被重算结果覆盖。"""
    stale = json.loads(json.dumps(ROLLUP_IN_SHARD))
    stale["criteria_rollup"] = {"IN-5": {"conclusion": "不符合"}}
    stale["rollup_summary"] = {"符合": 0, "不符合": 99, "存疑": 0, "无法判断": 0}
    merged = pack.merge_judgments([stale])
    assert merged["criteria_rollup"]["IN-5"]["conclusion"] == "符合"
    assert merged["rollup_summary"]["不符合"] == 0


def test_merge_judgments_surfaces_rollup_warnings():
    missing_semantics = {
        "judgments": {
            "IN-5-1": {"conclusion": "符合", "或组": "IN-5-OR"},
            "IN-5-2": {"conclusion": "无法判断", "或组": "IN-5-OR"},
        }
    }
    merged = pack.merge_judgments([missing_semantics])
    assert any("或组语义" in w for w in merged["rollup_warnings"])
    # 告警不阻断：汇总照常产出且按轨前缀推断
    assert merged["criteria_rollup"]["IN-5"]["conclusion"] == "符合"


def test_merge_judgments_omits_rollup_warnings_key_when_clean():
    merged = pack.merge_judgments([ROLLUP_IN_SHARD, ROLLUP_EX_SHARD])
    assert "rollup_warnings" not in merged


def test_merge_judgments_cli_reports_rollup(tmp_path: Path, capsys):
    a = _write_json(tmp_path / "a.json", ROLLUP_IN_SHARD)
    b = _write_json(tmp_path / "b.json", ROLLUP_EX_SHARD)
    out = tmp_path / "judgments_M017.json"
    assert pack.main(["merge-judgments", "--shards", str(a), str(b), "--out", str(out)]) == 0
    assert "主条件组级汇总" in capsys.readouterr().out
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["criteria_rollup"]["IN-5"]["conclusion"] == "符合"


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


# --- merge-reasons 已移除（回归防复活）-------------------------------------
#
# 独立理由阶段（task(report-writer) → reasons_*.json → merge-reasons 回填）已整体移除。
# 故障 thread `81562273`：理由子代理拿不到标准包与 OCR，按"脑内通用标准顺序"位置映射，
# IN 轨 24 条中 16 条条件ID↔理由错位，并引用该患者 OCR 命中 0 次的化验值
# （ANC 3.55 / PLT 206 / HGB 133 / 肌酐 80.1；真实 PLT 136 / HGB 121 / 肌酐 64），
# 覆盖发生在 QC 通过之后且无闸复核，编造值直达交付报告。
# `reason` 现由判定子代理落盘 judgments_draft 时一次写定，合并直接消费 draft。


def test_merge_reasons_api_removed():
    assert not hasattr(pack, "merge_reasons"), "merge_reasons 不得复活：理由须在判定阶段写定"


def test_merge_reasons_subcommand_removed(capsys):
    with pytest.raises(SystemExit) as exc:
        pack.main(["merge-reasons", "--judgments", "a.json", "--reasons", "b.json", "--out", "c.json"])
    assert exc.value.code == 2
    assert "invalid choice: 'merge-reasons'" in capsys.readouterr().err


# --- 或组字段必须穿过 slim（否则拆分语义在切包时丢失）---------------------
#
# OR 分支已改为拆成并行原子子条件 + `或组`/`或组语义` 标记（criteria-parser 闸 8）。
# `slim` 用 KEEP_FIELDS 白名单裁字段，若这两个键不在白名单里，判定子代理拿到的包里
# 就没有组信息 —— 它会把同组分支当成彼此独立的条件，IN 轨于是把"满足其一即可"
# 按约束 18 读成"必须全部满足"，患者被错误淘汰。


# --- CLI ------------------------------------------------------------------


def _write_json(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


# --- split 闸门 -------------------------------------------------------------
#
# 回归 thread 5a1c8d95 的两个故障：
#   1. 切分与 QC 第 2 轮同轮发出 → 在 QC 未收敛、EX-* 还错放在「入选」类目时切出
#      IN 46 条 / EX 1 条的残缺判定包；
#   2. QC 2 轮仍有阻断项时，主代理把 blocking_issues 挪进 residual_issues 标注
#      「已达QC轮次上限，带建议放行」、置 passed=true，未经 QC 复核即推进判定。


# 故障态（双轨版）：EX-* 被写进 IN 轨的「入选_不可从病例获取」



def test_merge_judgments_takes_or_groups_from_the_criteria_pack():
    """判定条目不带 `或组` 时，或组必须来自标准包 —— 这是 d1883294 的正解。"""
    groups = pack.rollup.extract_or_groups(IN_CRITERIA_PACK)
    merged = pack.merge_judgments([BARE_IN_SHARD], groups=groups)
    table = merged["criteria_rollup"]
    assert table["IN-7"]["conclusion"] == "符合"
    assert table["IN-7"]["rule"] == "OR组"
    assert table["IN-7"]["decided_by"] == ["IN-7-2"]
    assert table["IN-9"]["rule"] == "单条"


def test_merge_judgments_without_pack_reproduces_the_failure():
    """锁死故障形态：不传标准包时同一份数据仍折叠成 AND / 无法判断。

    保留这条是为了让「传包」与「不传包」的差异始终可见，
    防止日后有人以为不传包也安全。
    """
    merged = pack.merge_judgments([BARE_IN_SHARD])
    table = merged["criteria_rollup"]
    assert table["IN-7"]["conclusion"] == "无法判断"
    assert table["IN-7"]["rule"] == "AND"


def test_merge_judgments_cli_criteria_fixes_the_rollup(tmp_path):
    shard = tmp_path / "in.json"
    shard.write_text(json.dumps(BARE_IN_SHARD, ensure_ascii=False), encoding="utf-8")
    criteria = tmp_path / "criteria_judge_IN.json"
    criteria.write_text(json.dumps(IN_CRITERIA_PACK, ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "judgments_draft.json"

    rc = pack.main(["merge-judgments", "--shards", str(shard), "--criteria", str(criteria), "--out", str(out)])
    assert rc == 0
    merged = json.loads(out.read_text(encoding="utf-8"))
    table = merged["criteria_rollup"]
    assert table["IN-7"]["conclusion"] == "符合"
    assert table["IN-7"]["rule"] == "OR组"


def test_merge_judgments_cli_accepts_multiple_criteria_packs(tmp_path):
    """两轨包分别传入（`--criteria IN.json EX.json`）。"""
    ex_shard = {
        "patient_id": "M018",
        "judgments": {"EX-1-1": {"conclusion": "符合", "exclusion_triggered": False, "reason": "未触发"}},
    }
    a = tmp_path / "in.json"
    b = tmp_path / "ex.json"
    a.write_text(json.dumps(BARE_IN_SHARD, ensure_ascii=False), encoding="utf-8")
    b.write_text(json.dumps(ex_shard, ensure_ascii=False), encoding="utf-8")
    cin = tmp_path / "cin.json"
    cex = tmp_path / "cex.json"
    cin.write_text(json.dumps(IN_CRITERIA_PACK, ensure_ascii=False), encoding="utf-8")
    cex.write_text(
        json.dumps({"四分类": {"排除_可从病例获取": {"EX-1-1": {"条件ID": "EX-1-1", "或组": "EX-1-OR", "或组语义": "任一触发即整条触发"}}}}, ensure_ascii=False),
        encoding="utf-8",
    )
    out = tmp_path / "m.json"
    rc = pack.main(["merge-judgments", "--shards", str(a), str(b), "--criteria", str(cin), str(cex), "--out", str(out)])
    assert rc == 0
    table = json.loads(out.read_text(encoding="utf-8"))["criteria_rollup"]
    assert table["IN-7"]["rule"] == "OR组"
    assert table["EX-1"]["rule"] == "OR组"


def test_merge_judgments_cli_exit_2_when_declared_groups_do_not_materialise(tmp_path):
    """包声明的或组一个都没落地 → exit 2 且不落盘，禁止交付退化成 AND 的汇总。"""
    shard = tmp_path / "in.json"
    # 条件ID 与标准包对不上（拼写漂移）
    shard.write_text(
        json.dumps({"judgments": {"IN-7-01": {"conclusion": "符合"}}}, ensure_ascii=False),
        encoding="utf-8",
    )
    criteria = tmp_path / "c.json"
    criteria.write_text(json.dumps(IN_CRITERIA_PACK, ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "m.json"
    rc = pack.main(["merge-judgments", "--shards", str(shard), "--criteria", str(criteria), "--out", str(out)])
    assert rc == 2
    assert not out.exists(), "阻断时不得留下半成品交付物"


def test_merge_judgments_cli_warns_when_criteria_is_absent(tmp_path):
    """不传 `--criteria` 仍可跑（老流程兼容），但必须出声提示或组可能退化。"""
    shard = tmp_path / "in.json"
    shard.write_text(json.dumps(BARE_IN_SHARD, ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "m.json"
    rc = pack.main(["merge-judgments", "--shards", str(shard), "--out", str(out)])
    assert rc == 0
    assert out.exists()


def test_merge_judgments_pack_group_overrides_entry_group_with_warning():
    """条目抄错或组名时以包为准，并把告警带进 rollup_warnings 供 QC 看见。"""
    shard = {
        "judgments": {
            "IN-7-1": {"conclusion": "无法判断", "或组": "IN-7-TYPO", "或组语义": "任一满足即整条满足"},
            "IN-7-2": {"conclusion": "符合", "或组": "IN-7-OR", "或组语义": "任一满足即整条满足"},
        }
    }
    groups = pack.rollup.extract_or_groups(IN_CRITERIA_PACK)
    merged = pack.merge_judgments([shard], groups=groups)
    assert merged["criteria_rollup"]["IN-7"]["conclusion"] == "符合"
    assert any("IN-7-TYPO" in w for w in merged.get("rollup_warnings") or []), merged.get("rollup_warnings")


# ── plan-batches：轨内按 12 条一批（会话 09eeaffb）────────────────────────────
#
# 整轨一次派的失败形态：IN 28 条 / EX 45 条由单个子代理判完，99 个 AI 回合 0 次 write_file，
# 撞满 recursion_limit=420，两轨 10.02M token / 42 分钟、产物为零（recursion_limit 分支
# 不打捞部分产物）。分批让每批各自落盘：撞限只损失一批。
#
# 本组测试锁的是分批**不能破坏**的三件事：批次不跨轨、不按四分类类目切、不切标准包。


def _pack_with_ids(track: str, ids: list[str], *, unobtainable: set[str] | None = None) -> dict:
    """造一个只有条件ID 的单轨包。`unobtainable` 指定落在「不可从病例获取」类目的条件ID。"""
    keyword = pack.SHARDS[track]
    cannot = unobtainable or set()
    return {
        "分片": keyword,
        "条件数": len(ids),
        "四分类": {
            f"{keyword}_可从病例获取": {cid: {"条件ID": cid} for cid in ids if cid not in cannot},
            f"{keyword}_不可从病例获取": {cid: {"条件ID": cid} for cid in ids if cid in cannot},
        },
    }


# 会话 09eeaffb 的真实 IN 轨：28 条，其中 4 条「不可从病例获取」。
IN_28_IDS = [
    "IN-1", "IN-2-1", "IN-2-2", "IN-3-1", "IN-3-2", "IN-4-1", "IN-4-2", "IN-4-3",
    "IN-5", "IN-6", "IN-7-1", "IN-7-2", "IN-8", "IN-9",
    "IN-10-1", "IN-10-2", "IN-10-3", "IN-10-4", "IN-10-5", "IN-10-6",
    "IN-10-7", "IN-10-8", "IN-10-9", "IN-10-10", "IN-10-11", "IN-10-12",
    "IN-11-1", "IN-11-2",
]
# 真实归属（该会话 criteria_judge_IN.json 实测）：这 4 条在自然序里是**散布**的，
# 不是末尾连续的一段 —— 正因如此，按自然序切分必然让每一批都混含两个类目。
IN_28_UNOBTAINABLE = {"IN-1", "IN-6", "IN-9", "IN-11-2"}


def test_plan_batches_splits_28_into_12_12_4():
    """会话 09eeaffb 的真实 IN 轨规模：28 条 → 3 批（12/12/4）。"""
    batches = pack.plan_batches(_pack_with_ids("IN", IN_28_IDS), batch_size=12)
    assert [b["count"] for b in batches] == [12, 12, 4]
    assert [b["batch"] for b in batches] == [1, 2, 3]


def test_plan_batches_is_a_partition_of_the_track():
    """每条恰好属于一批：不漏、不重 —— 漏一批就等于漏判一批条件。"""
    batches = pack.plan_batches(_pack_with_ids("IN", IN_28_IDS), batch_size=12)
    flat = [cid for b in batches for cid in b["condition_ids"]]
    assert sorted(flat, key=pack._sort_key) == sorted(IN_28_IDS, key=pack._sort_key)
    assert len(flat) == len(set(flat)), "同一条件被切进了两批"


def test_plan_batches_cuts_across_four_category_boundaries():
    """⛔ 不按 `四分类` 类目切。

    按类目切会让 IN 轨「不可从病例获取」那 4 条单独成批，而它们同样要全量核查病历
    （见 judge-delegation 模板「『不可从病例获取』条目同样必须核查病历」），
    等于多派一次任务、多付一份 OCR 读取。

    用真实归属做夹具：IN-1 / IN-6 / IN-9 / IN-11-2 在自然序里是散布的，
    所以「按自然序连续切分」与「按类目切分」会给出可区分的结果。
    """
    pk = _pack_with_ids("IN", IN_28_IDS, unobtainable=IN_28_UNOBTAINABLE)
    batches = pack.plan_batches(pk, batch_size=12)

    # 两个类目的条目必须混在同一批里（按类目切时不可能出现这种批次）
    mixed = [
        b["batch"]
        for b in batches
        if (set(b["condition_ids"]) & IN_28_UNOBTAINABLE) and (set(b["condition_ids"]) - IN_28_UNOBTAINABLE)
    ]
    assert mixed, f"没有任何批次混含两个类目 —— 疑似按类目切了：{batches}"
    # 且没有任何一批只由「不可从病例获取」构成
    for b in batches:
        assert not set(b["condition_ids"]) <= IN_28_UNOBTAINABLE, f"批 {b['batch']} 只含「不可从病例获取」条目"


def test_plan_batches_keeps_natural_condition_id_order():
    """批内与批间都按条件ID 自然序，便于人工核对与补派。"""
    batches = pack.plan_batches(_pack_with_ids("IN", IN_28_IDS), batch_size=12)
    flat = [cid for b in batches for cid in b["condition_ids"]]
    assert flat == sorted(flat, key=pack._sort_key)


def test_batch_plan_never_mixes_tracks():
    """⛔ 批次不跨轨：两轨的模板与闸命令都不同（EX 独有方向校验与 exclusion_triggered）。"""
    in_plan = pack.batch_plan(_pack_with_ids("IN", IN_28_IDS), "IN", batch_size=12, patient="P001")
    ex_ids = [f"EX-{n}" for n in range(1, 46)]
    ex_plan = pack.batch_plan(_pack_with_ids("EX", ex_ids), "EX", batch_size=12, patient="P001")

    for plan, prefix in ((in_plan, "IN-"), (ex_plan, "EX-")):
        for b in plan["batches"]:
            assert all(cid.startswith(prefix) for cid in b["condition_ids"]), b


def test_batch_plan_file_names_carry_batch_suffix():
    """批级产物必须带 `_b{N}`：写成整轨文件名会覆盖别批的成果。"""
    plan = pack.batch_plan(_pack_with_ids("IN", IN_28_IDS), "IN", batch_size=12, patient="P001")
    assert plan["batch_count"] == 3
    assert plan["total_conditions"] == 28
    assert [b["draft_file"] for b in plan["batches"]] == [
        "judgments_draft_P001_IN_b1.json",
        "judgments_draft_P001_IN_b2.json",
        "judgments_draft_P001_IN_b3.json",
    ]
    assert plan["track_draft_file"] == "judgments_draft_P001_IN.json"
    assert plan["merge_shards"] == [b["draft_file"] for b in plan["batches"]]
    # 批级 draft 与整轨 draft 必须是不同文件，否则合并会读到自己
    assert plan["track_draft_file"] not in plan["merge_shards"]


def test_batch_plan_leaves_id_placeholder_without_patient():
    plan = pack.batch_plan(_pack_with_ids("IN", IN_28_IDS[:5]), "IN", batch_size=12)
    assert plan["patient_id"] is None
    assert plan["batches"][0]["draft_file"] == "judgments_draft_{id}_IN_b1.json"


def test_plan_batches_rejects_empty_pack():
    with pytest.raises(pack.SplitBlocked, match="没有任何条件ID"):
        pack.plan_batches({"四分类": {"入选_可从病例获取": {}}}, batch_size=12)


def test_plan_batches_rejects_non_positive_batch_size():
    with pytest.raises(pack.SplitBlocked, match="必须 ≥ 1"):
        pack.plan_batches(_pack_with_ids("IN", IN_28_IDS), batch_size=0)


def test_plan_batches_accepts_legacy_list_categories():
    """旧 workspace 的类目是 list（只读兼容），同样能规划批次。"""
    legacy = {"四分类": {"入选_可从病例获取": [{"条件ID": cid} for cid in IN_28_IDS]}}
    batches = pack.plan_batches(legacy, batch_size=12)
    assert [b["count"] for b in batches] == [12, 12, 4]


def test_plan_batches_deduplicates_ids():
    """同一条件ID 在两个类目里各出现一次时只排一次（否则会被两批同时判）。"""
    dup = {
        "四分类": {
            "入选_可从病例获取": {"IN-1": {"条件ID": "IN-1"}, "IN-2": {"条件ID": "IN-2"}},
            "入选_不可从病例获取": {"IN-1": {"条件ID": "IN-1"}},
        }
    }
    batches = pack.plan_batches(dup, batch_size=12)
    flat = [cid for b in batches for cid in b["condition_ids"]]
    assert flat == ["IN-1", "IN-2"]


def test_plan_batches_cli_writes_plan(tmp_path):
    criteria = tmp_path / "criteria_judge_IN.json"
    criteria.write_text(json.dumps(_pack_with_ids("IN", IN_28_IDS), ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "judge_batches_P001_IN.json"

    rc = pack.main(["plan-batches", "--criteria", str(criteria), "--track", "IN", "--patient", "P001", "--out", str(out)])
    assert rc == 0
    plan = json.loads(out.read_text(encoding="utf-8"))
    assert plan["batch_size"] == pack.DEFAULT_BATCH_SIZE == 12
    assert plan["batch_count"] == 3
    assert plan["track"] == "IN"


def test_plan_batches_cli_respects_batch_size(tmp_path):
    criteria = tmp_path / "c.json"
    criteria.write_text(json.dumps(_pack_with_ids("IN", IN_28_IDS), ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "p.json"
    assert pack.main(["plan-batches", "--criteria", str(criteria), "--track", "IN", "--batch-size", "6", "--out", str(out)]) == 0
    plan = json.loads(out.read_text(encoding="utf-8"))
    assert [b["count"] for b in plan["batches"]] == [6, 6, 6, 6, 4]


def test_plan_batches_cli_blocks_empty_pack(tmp_path):
    criteria = tmp_path / "c.json"
    criteria.write_text(json.dumps({"四分类": {}}, ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "p.json"
    assert pack.main(["plan-batches", "--criteria", str(criteria), "--track", "IN", "--out", str(out)]) == 2
    assert not out.exists(), "阻断时不得留下半成品批次清单"


def test_batch_drafts_merge_back_into_a_complete_track():
    """分批的收益要能收口：各批 draft 合并回整轨，条目与整轨一次判定等价。

    这是「不切标准包」的直接后果之一 —— `merge-judgments --criteria` 拿整轨包重算或组，
    因此或组分支落在不同批次也不影响汇总。
    """
    groups = pack.rollup.extract_or_groups(IN_CRITERIA_PACK)
    # IN-7-OR 的两个分支被人为分到两批，模拟或组跨批边界
    batch1 = {
        "patient_id": "M018",
        "judgments": {"IN-7-1": {"conclusion": "无法判断"}},
    }
    batch2 = {
        "patient_id": "M018",
        "judgments": {"IN-7-2": {"conclusion": "符合"}},
    }
    merged = pack.merge_judgments([batch1, batch2], groups=groups)
    judgments = merged["judgments"]

    assert sorted(judgments) == ["IN-7-1", "IN-7-2"], "批次合并丢了条目"
    # 或组按「任一满足即整条满足」折叠 —— 跨批不影响
    assert merged["criteria_rollup"]["IN-7"]["conclusion"] == "符合"
    assert merged["summary"] == {"符合": 1, "不符合": 0, "存疑": 0, "无法判断": 1}
