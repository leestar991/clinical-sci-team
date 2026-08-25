"""`evidence_bundle.py` —— QC 取证素材预装配（治步数，不是治读取量）。

会话 `93d8a2c6` 的成本模型（实测）：**计费 input ≈ (AI 步数 / 2) × 该 task 累积内容量**。
失败的 `IN judgment QC round 2` task 就是标本：106 步 / 50 AI 步 / 1.97M token，
做的是 31 次 read_file + 15 次 grep 逐条取证，最后耗尽 `max_turns=150` 而失败。

本脚本把这几十步取证压成一次读。所以它的测试要守住两件事：
1. **素材完整**：QC 不必再回去 grep —— 条件、锚点、判定、reason、引文核验、OCR 窗口齐全；
2. **产物自己不能变成新的上下文炸弹** —— 命中数、窗口、单块、总量都有上限。

⛔ 它不下判定结论、不改产物。「引文核验」是确定性逐字比对，不是语义判断。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "skills" / "custom" / "eligibility-judgment" / "scripts" / "evidence_bundle.py"

if not SCRIPT.exists():  # skills/custom 为 gitignore 目录
    pytest.skip("eligibility-judgment 技能未安装", allow_module_level=True)


def _load():
    """按 `test_uncertain_recheck.py` 的同一套方式加载，避免污染 sys.path。"""
    spec = importlib.util.spec_from_file_location("evidence_bundle", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


eb = _load()


# ─────────────────────────── 夹具 ───────────────────────────

OCR_EXAM = """来源图片：/x/筛选期检查_page_001.jpg
| 项目 | 结果 | 参考范围 |
| 血小板计数 PLT | 136 | 125-350 |
| 中性粒细胞 GRAN | 4.2 | 2.0-7.0 |
来源图片：/x/筛选期检查_page_002.jpg
| ECOG 评分 | 1 分 |
| 靶病灶 | 右侧髂血管旁淋巴结 15.91 mm |
"""

OCR_RECORD = """来源图片：/x/筛选期病历_page_001.jpg
患者男性，年龄 62 岁。
知情同意书签署=2026-04-15 16:21
既往史：2025-03 起口服阿比特龙 1000mg qd
"""


def _criteria(*items) -> dict:
    return {"四分类": {"入选_可从病例获取": {str(i["条件ID"]): i for i in items}}}  # 类目规范形态：以 条件ID 为键的 dict（数组只是旧 workspace 的只读兼容形态）


def _cond(cid, 子条件, *, 同义词=None, 匹配字段=None):
    t = {}
    if 同义词:
        t["同义词"] = 同义词
    if 匹配字段:
        t["匹配字段"] = 匹配字段
    item = {"条件ID": cid, "子条件": 子条件, "原文": 子条件}
    if t:
        item["转化条件"] = t
    return item


def _judgments(entries: dict, *, doc="筛选期检查", patient="P1") -> dict:
    return {"patient_id": patient, "judgments": entries}  # 统一证据源判定产物：顶层 judgments


def _entry(conclusion, reason, evidence=None, **extra):
    e = {"conclusion": conclusion, "reason": reason, "evidence": evidence if evidence is not None else []}
    e.update(extra)
    return e


def _ocr(tmp_path: Path, text: str, source: str = "筛选期检查") -> Path:
    d = tmp_path / source
    d.mkdir(parents=True, exist_ok=True)
    p = d / "ocr_records.md"
    p.write_text(text, encoding="utf-8")
    return p


def _build(tmp_path, criteria, judgments, ocrs=None, **kw):
    return eb.build_bundle(criteria, judgments, ocrs or [_ocr(tmp_path, OCR_EXAM)], **kw)


# ─────────────────── 素材完整性（QC 不必回去 grep）───────────────────


class TestBundleCarriesEverythingQcNeeds:
    def test_includes_condition_anchors_verdict_and_reason(self, tmp_path):
        text, _ = _build(
            tmp_path,
            _criteria(_cond("IN-10-2", "血小板（PLT）≥100×10^9/L", 同义词=["血小板", "PLT"])),
            _judgments({"IN-10-2": _entry("符合", "血常规示血小板计数 136×10^9/L，满足≥100。")}),
        )
        assert "IN-10-2" in text
        assert "血小板（PLT）≥100" in text, "缺条件原文，QC 无法判断该查什么"
        assert "PLT" in text, "缺锚点"
        assert "`符合`" in text, "缺当前判定"
        assert "血常规示血小板计数 136" in text, "缺 reason"

    def test_includes_ocr_hit_window_with_line_numbers(self, tmp_path):
        """行号是必须的：QC 若要补读更宽的窗口，得知道从哪一行读。"""
        text, _ = _build(
            tmp_path,
            _criteria(_cond("IN-10-2", "血小板", 同义词=["血小板"])),
            _judgments({"IN-10-2": _entry("符合", "PLT 136。")}),
        )
        assert "OCR 窗口" in text, "缺窗口附录"
        assert "血小板计数 PLT | 136" in text, "缺原文"
        assert "筛选期检查:3" in text, "条目块必须给出命中行号"
        assert "W1" in text, "条目块必须能指到具体窗口"

    def test_exclusion_and_or_group_fields_are_surfaced(self, tmp_path):
        """EX 轨方向核验与 `或组` 汇总都是阻断级检查项，字段必须在素材里。"""
        text, _ = _build(
            tmp_path,
            _criteria(_cond("EX-5-2", "存在脑转移", 同义词=["脑转移"])),
            _judgments({"EX-5-2": _entry("不符合", "影像示脑转移。", exclusion_triggered=True, **{"或组": "EX-5-OR", "或组语义": "任一触发即整条触发"})}),
        )
        assert "exclusion_triggered=True" in text
        assert "EX-5-OR" in text and "任一触发即整条触发" in text

    def test_missing_or_group_semantics_is_marked(self, tmp_path):
        text, _ = _build(
            tmp_path,
            _criteria(_cond("IN-5-1", "既往接受过治疗", 同义词=["治疗史"])),
            _judgments({"IN-5-1": _entry("符合", "有。", **{"或组": "IN-5-OR"})}),
        )
        assert "语义缺失" in text


# ─────────────────── 引文逐字核验（最该机械化的一步）───────────────────


class TestQuoteVerification:
    def test_traceable_quote_is_marked_with_source_and_line(self, tmp_path):
        text, stats = _build(
            tmp_path,
            _criteria(_cond("IN-10-2", "血小板", 同义词=["血小板"])),
            _judgments({"IN-10-2": _entry("符合", "PLT 136。", [{"source": "筛选期检查", "page": 1, "quote": "血小板计数 PLT | 136"}])}),
        )
        assert stats["引文可溯源"] == 1 and stats["引文未找到"] == 0
        assert "✅ 筛选期检查:3" in text, "必须给出行号，否则 QC 还得自己找"

    def test_untraceable_quote_is_flagged_as_blocking_lead(self, tmp_path):
        """引文在 OCR 里找不到 = 跨患者污染或编造，是 M018 那类整批错位的确证。"""
        text, stats = _build(
            tmp_path,
            _criteria(_cond("IN-10-1", "中性粒细胞", 同义词=["中性粒细胞"])),
            _judgments({"IN-10-1": _entry("符合", "ANC 3.55。", [{"source": "筛选期检查", "page": 1, "quote": "中性粒细胞绝对值 3.55"}])}),
        )
        assert stats["引文未找到"] == 1
        assert "❌ OCR 中未找到" in text
        assert "阻断级线索" in text

    def test_whitespace_and_width_differences_do_not_break_traceability(self, tmp_path):
        """空白/全半角是提取噪声，不是造假。硬比会把大量真引文报成找不到，QC 又得逐条人工核。"""
        _, stats = _build(
            tmp_path,
            _criteria(_cond("IN-10-2", "血小板", 同义词=["血小板"])),
            _judgments({"IN-10-2": _entry("符合", "x", [{"source": "筛选期检查", "page": 1, "quote": "血小板计数PLT|１３６"}])}),
        )
        assert stats["引文可溯源"] == 1

    def test_quote_spanning_multiple_lines_is_found(self, tmp_path):
        """表格类引文常跨行，逐行匹配必然失败 → 必须回退到整份文本匹配。"""
        _, stats = _build(
            tmp_path,
            _criteria(_cond("IN-1", "x", 同义词=["ECOG"])),
            _judgments({"IN-1": _entry("符合", "x", [{"source": "筛选期检查", "page": 2, "quote": "| ECOG 评分 | 1 分 || 靶病灶 |"}])}),
        )
        assert stats["引文可溯源"] == 1

    def test_entry_without_evidence_is_listed(self, tmp_path):
        text, stats = _build(
            tmp_path,
            _criteria(_cond("IN-3", "x", 同义词=["ECOG"])),
            _judgments({"IN-3": _entry("无法判断", "未见记录。")}),
        )
        assert stats["无 evidence 的条目"] == ["IN-3"]
        assert "无 evidence 的条目" in text

    def test_non_object_evidence_is_called_out(self, tmp_path):
        text, _ = _build(
            tmp_path,
            _criteria(_cond("IN-4", "x", 同义词=["ECOG"])),
            _judgments({"IN-4": _entry("符合", "x", ["血小板 136"])}),
        )
        assert "非对象形态" in text and "闸12" in text


# ─────────────────── 产物自己不能变成上下文炸弹 ───────────────────


class TestBundleStaysSmall:
    def test_hits_per_entry_are_capped(self, tmp_path):
        big = "".join(f"| ECOG 评分 | {i} 分 |\n" for i in range(200))
        text, _ = _build(
            tmp_path,
            _criteria(_cond("IN-7", "ECOG", 同义词=["ECOG"])),
            _judgments({"IN-7": _entry("符合", "ECOG 1 分。")}),
            [_ocr(tmp_path, big)],
        )
        assert text.count("```") <= 2 * eb.MAX_HITS_PER_ENTRY

    def test_overlapping_windows_are_merged(self, tmp_path):
        """相邻命中行的窗口重叠时必须合并，否则同一段原文会被贴好几遍。"""
        text, _ = _build(
            tmp_path,
            _criteria(_cond("IN-7", "x", 同义词=["来源图片"])),
            _judgments({"IN-7": _entry("符合", "x")}),
            [_ocr(tmp_path, "来源图片：a\n来源图片：b\n来源图片：c\n")],
        )
        assert text.count("```") == 2, "三个相邻命中应合并成一个窗口块"

    def test_windows_are_deduplicated_ACROSS_entries(self, tmp_path):
        """实测发现的真问题：按条目各自出窗口时，相邻条目大面积重叠。

        IN-10-2 给 L1-6、IN-10-3 给 L2-8、IN-7 给 L4-10 —— 第 4/5/6 行被贴三遍。
        30 个条目就是三倍 payload，而这份产物会被后续每一步重传。
        """
        text, stats = _build(
            tmp_path,
            _criteria(
                _cond("IN-10-2", "血小板", 同义词=["血小板"]),
                _cond("IN-10-3", "血红蛋白", 同义词=["血红蛋白"]),
                _cond("IN-7", "ECOG", 匹配字段=["ECOG"]),
            ),
            _judgments(
                {
                    "IN-10-2": _entry("符合", "a"),
                    "IN-10-3": _entry("符合", "b"),
                    "IN-7": _entry("符合", "c"),
                }
            ),
        )
        assert text.count("| 中性粒细胞 GRAN | 4.2 | 2.0-7.0 |") == 1, "同一行原文只能出现一次"
        assert text.count("| 血小板计数 PLT | 136 |") == 1
        assert text.count("| ECOG 评分 | 1 分 |") == 1
        assert stats["窗口数"] >= 1

    def test_each_entry_still_points_at_its_own_window(self, tmp_path):
        """去重不能牺牲可用性：每个条目都要能指到覆盖它命中行的窗口。"""
        text, _ = _build(
            tmp_path,
            _criteria(
                _cond("IN-10-2", "血小板", 同义词=["血小板"]),
                _cond("IN-7", "ECOG", 匹配字段=["ECOG"]),
            ),
            _judgments({"IN-10-2": _entry("符合", "a"), "IN-7": _entry("符合", "c")}),
        )
        for cid in ("IN-10-2", "IN-7"):
            block = text.split(f"## {cid}", 1)[1].split("\n## ", 1)[0]
            assert "窗口 [W" in block, f"{cid} 没有指向任何窗口"

    def test_long_reason_is_truncated_with_marker(self, tmp_path):
        text, _ = _build(
            tmp_path,
            _criteria(_cond("IN-8", "x", 同义词=["ECOG"])),
            _judgments({"IN-8": _entry("符合", "很长的理由。" * 500)}),
        )
        assert "截断" in text

    def test_bundle_total_is_capped_and_keeps_the_irreplaceable_part(self, tmp_path):
        """超长时砍窗口、留判定与引文核验 —— 后者是本产物不可替代的部分。"""
        entries, items = {}, []
        for i in range(1, 400):
            cid = f"IN-{i}"
            items.append(_cond(cid, "ECOG 体力状况评分 0-1 分（含）" * 12, 同义词=["ECOG", "来源图片"]))
            entries[cid] = _entry(
                "符合",
                "理由。" * 220,
                [{"source": "筛选期检查", "page": 1, "quote": "ECOG 评分"} for _ in range(4)],
            )
        text, stats = _build(tmp_path, _criteria(*items), _judgments(entries))
        assert len(text) <= eb.MAX_BUNDLE_CHARS
        assert stats.get("超长已省略窗口") is True
        assert "引文核验" in text, "砍掉的必须是窗口，不是引文核验"
        assert "按行号" in text, "省略窗口时必须告诉 QC 怎么补读"


# ─────────────────── 边界与契约 ───────────────────


class TestContractAndEdges:
    def test_bundle_states_it_carries_no_verdicts(self, tmp_path):
        """⛔ 它是素材，不是结论。写清楚，避免 QC 把它当判定依据直接抄。"""
        text, _ = _build(
            tmp_path,
            _criteria(_cond("IN-1", "x", 同义词=["ECOG"])),
            _judgments({"IN-1": _entry("符合", "x")}),
        )
        assert "不含任何判定结论" in text

    def test_bundle_tells_qc_not_to_grep_per_item(self, tmp_path):
        text, _ = _build(
            tmp_path,
            _criteria(_cond("IN-1", "x", 同义词=["ECOG"])),
            _judgments({"IN-1": _entry("符合", "x")}),
        )
        assert "不要再逐条" in text and "grep" in text

    def test_broad_anchors_are_not_used_for_retrieval(self, tmp_path):
        """用「治疗」检索会命中标准原文复述，等于给 QC 一堆噪声。"""
        assert eb.anchors_for({"转化条件": {"同义词": ["治疗", "内分泌治疗", "阿比特龙"]}}) == ["阿比特龙"]

    def test_missing_anchors_are_reported_not_silently_skipped(self, tmp_path):
        text, stats = _build(
            tmp_path,
            _criteria(_cond("IN-9", "研究者判断不适合入组")),
            _judgments({"IN-9": _entry("无法判断", "开放式条款。", [{"source": "x", "page": 1, "quote": "血小板计数 PLT | 136"}])}),
        )
        assert stats["锚点缺失的条目"] == ["IN-9"]
        assert "标准包无可用锚点" in text

    def test_zero_hit_anchor_is_distinguished_from_no_anchor(self, tmp_path):
        text, _ = _build(
            tmp_path,
            _criteria(_cond("IN-11", "Gilbert 综合征", 同义词=["Gilbert"])),
            _judgments({"IN-11": _entry("无法判断", "未见。")}),
        )
        assert "零命中" in text

    def test_multiple_ocr_sources_are_all_searched(self, tmp_path):
        text, stats = _build(
            tmp_path,
            _criteria(_cond("IN-2", "年龄", 同义词=["年龄"])),
            _judgments({"IN-2": _entry("符合", "62 岁。", [{"source": "筛选期病历", "page": 1, "quote": "年龄 62 岁"}])}),
            [_ocr(tmp_path, OCR_EXAM), _ocr(tmp_path, OCR_RECORD, source="筛选期病历")],
        )
        assert stats["引文可溯源"] == 1
        assert "筛选期病历" in text

    def test_flatten_accepts_flat_criteria_shape(self):
        assert set(eb.flatten_criteria({"条件": [{"条件ID": "IN-1"}]})) == {"IN-1"}

    def test_empty_judgments_produce_a_valid_but_empty_bundle(self, tmp_path):
        text, stats = _build(tmp_path, _criteria(), _judgments({}))
        assert stats["条目数"] == 0
        assert "装配摘要" in text


class TestCli:
    def test_exit_2_when_input_is_missing(self, tmp_path):
        code = eb.main(["--criteria", str(tmp_path / "nope.json"), "--judgments", str(tmp_path / "n2.json"), "--out", str(tmp_path / "o.md")])
        assert code == 2

    def test_exit_2_on_broken_json(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        good = tmp_path / "g.json"
        good.write_text(json.dumps(_judgments({})), encoding="utf-8")
        assert eb.main(["--criteria", str(bad), "--judgments", str(good), "--out", str(tmp_path / "o.md")]) == 2

    def test_exit_0_even_when_quotes_are_untraceable(self, tmp_path):
        """找不到引文是 QC 的判断对象，不是本脚本的失败 —— 否则装配阶段就把流程卡死了。"""
        c = tmp_path / "c.json"
        c.write_text(json.dumps(_criteria(_cond("IN-1", "x", 同义词=["ECOG"])), ensure_ascii=False), encoding="utf-8")
        j = tmp_path / "j.json"
        j.write_text(json.dumps(_judgments({"IN-1": _entry("符合", "x", [{"source": "s", "page": 1, "quote": "查无此句"}])}), ensure_ascii=False), encoding="utf-8")
        out = tmp_path / "bundle.md"
        code = eb.main(["--criteria", str(c), "--judgments", str(j), "--ocr", str(_ocr(tmp_path, OCR_EXAM)), "--out", str(out), "--patient", "P1", "--track", "IN"])
        assert code == 0
        assert "❌ OCR 中未找到" in out.read_text(encoding="utf-8")

    def test_writes_the_bundle_and_creates_parent_dirs(self, tmp_path):
        c = tmp_path / "c.json"
        c.write_text(json.dumps(_criteria(_cond("IN-1", "x", 同义词=["ECOG"])), ensure_ascii=False), encoding="utf-8")
        j = tmp_path / "j.json"
        j.write_text(json.dumps(_judgments({"IN-1": _entry("符合", "ECOG 1 分")}), ensure_ascii=False), encoding="utf-8")
        out = tmp_path / "deep" / "nested" / "bundle.md"
        assert eb.main(["--criteria", str(c), "--judgments", str(j), "--ocr", str(_ocr(tmp_path, OCR_EXAM)), "--out", str(out)]) == 0
        assert out.exists() and "IN-1" in out.read_text(encoding="utf-8")
