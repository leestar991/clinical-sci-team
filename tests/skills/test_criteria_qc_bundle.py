"""`criteria_qc_bundle.py` —— 解析 QC 取证预装配（治步数）。

会话 `e3c15416`：`EX track QC round 2` 跑 77 步、工具构成 `bash 28 / read_file 11`，撞上
`max_turns=150` **失败**；全会话 26 处内联 `python3 -c` 多数只是 `json.load` + `print`。
这与判定侧 `93d8a2c6` 的 QC 步数病同形，只是换了阶段。

测试守两件事：
1. **素材完整**：QC 不必再 grep 原文、不必再写内联 python —— 原条号分组、raw 原文、
   转化条件要素、机械预判齐全；
2. **产物自己不能变成新的上下文炸弹** —— 每组 raw 行数、单组字符、总字符都有上限。

⛔ 它不下 QC 结论、不改产物；判据口径直接复用 `check_track_structure.py`（闸9 归一化、
闸12 运算符集合），不在新脚本里重写。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "skills" / "custom" / "criteria-parser" / "scripts" / "criteria_qc_bundle.py"

if not SCRIPT.exists():  # skills/custom 为 gitignore 目录
    pytest.skip("criteria-parser 技能未安装", allow_module_level=True)


def _load():
    spec = importlib.util.spec_from_file_location("criteria_qc_bundle", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


bundle = _load()


RAW = """# 4.2 排除标准

1.  已知对研究药物任一辅料过敏。

2.  存在下列任一情况者：a) 6 个月内心肌梗死；b) 需药物干预的心力衰竭。

3.  PSA 进展，根据 PCWG3 定义。
"""


def _ws(tmp_path: Path, parsed: dict, raw: str = RAW) -> Path:
    (tmp_path / "eligibility_criteria_raw.md").write_text(raw, encoding="utf-8")
    (tmp_path / "criteria_parsed_EX.json").write_text(json.dumps(parsed, ensure_ascii=False), encoding="utf-8")
    return tmp_path


def _item(cid, 子条件, 原文, *, 转化条件=None, 日期维度=None, 或组=None, 或组语义=None):
    it = {"条件ID": cid, "子条件": 子条件, "原文": 原文}
    if 转化条件 is not None:
        it["转化条件"] = 转化条件
    if 日期维度 is not None:
        it["日期维度"] = 日期维度
    if 或组:
        it["或组"] = 或组
    if 或组语义:
        it["或组语义"] = 或组语义
    return it


def _parsed(*items, 描述索引=None, cat="排除_可从病例获取"):
    return {"四分类": {cat: {str(i["条件ID"]): i for i in items}}, "描述索引": 描述索引 or {}}  # 类目规范形态：以 条件ID 为键的 dict（数组只是旧 workspace 的只读兼容形态）


# ─────────────── 素材完整性：按原条号分组 ───────────────


class TestGroupingByClause:
    def test_groups_subconditions_under_their_original_clause(self, tmp_path):
        """解析 QC 问的是「原文这一条拆得对不对」，所以必须按原条号成组。"""
        ws = _ws(
            tmp_path,
            _parsed(
                _item("EX-2-1", "6 个月内心肌梗死", "6 个月内心肌梗死", 转化条件={"同义词": ["心肌梗死"]}),
                _item("EX-2-2", "需药物干预的心力衰竭", "需药物干预的心力衰竭", 转化条件={"同义词": ["心力衰竭"]}),
                _item("EX-1", "对辅料过敏", "已知对研究药物任一辅料过敏。", 转化条件={"同义词": ["过敏"]}),
            ),
        )
        text, stats = bundle.build_bundle(ws, "EX")
        assert "## 原条号 1" in text and "## 原条号 2" in text
        assert stats["原条号组数"] == 2 and stats["子条件数"] == 3
        group2 = text.split("## 原条号 2", 1)[1]
        assert "EX-2-1" in group2 and "EX-2-2" in group2
        assert "该原条号被拆成 2 条子条件" in group2

    def test_raw_text_appears_once_per_group_not_per_subcondition(self, tmp_path):
        """raw 原文按组贴一次 —— 判定侧取证包的教训：按条目贴会把同一段贴好几遍。"""
        ws = _ws(
            tmp_path,
            _parsed(
                _item("EX-2-1", "a", "6 个月内心肌梗死", 转化条件={"同义词": ["心肌梗死"]}),
                _item("EX-2-2", "b", "需药物干预的心力衰竭", 转化条件={"同义词": ["心力衰竭"]}),
            ),
        )
        text, _ = bundle.build_bundle(ws, "EX")
        assert text.count("存在下列任一情况者") == 1, "同一段 raw 原文不得重复贴出"

    def test_raw_window_carries_line_numbers(self, tmp_path):
        """行号是必须的：QC 要补读更宽区间时得知道从哪读。"""
        ws = _ws(tmp_path, _parsed(_item("EX-1", "过敏", "已知对研究药物任一辅料过敏。", 转化条件={"同义词": ["过敏"]})))
        text, _ = bundle.build_bundle(ws, "EX")
        assert "**raw 原文** `L" in text
        assert "已知对研究药物任一辅料过敏。" in text

    def test_unlocatable_original_text_is_flagged(self, tmp_path):
        ws = _ws(tmp_path, _parsed(_item("EX-9", "x", "这句话原文里根本没有的内容", 转化条件={"同义词": ["x"]})))
        text, _ = bundle.build_bundle(ws, "EX")
        assert "未能按 `原文` 定位" in text


    def test_windows_do_not_overlap_across_clauses(self, tmp_path):
        """实跑发现的真缺陷：按「锚点 + 固定行数」出窗口时，各组大面积重叠。

        原条号 1 给 L1-8、2 给 L3-8、3 给 L6-8 —— 第 3~8 行被贴了三遍。窗口必须由 raw 自身的
        条号边界决定。（与 `evidence_bundle.py` 同一个教训：装配类产物必须实跑看一眼。）
        """
        ws = _ws(
            tmp_path,
            _parsed(
                _item("EX-1", "过敏", "已知对研究药物任一辅料过敏。", 转化条件={"同义词": ["过敏"]}),
                _item("EX-2-1", "心梗", "6 个月内心肌梗死", 转化条件={"同义词": ["心肌梗死"]}),
                _item("EX-3", "PSA", "PSA 进展，根据 PCWG3 定义。", 转化条件={"同义词": ["PSA"]}),
            ),
        )
        text, _ = bundle.build_bundle(ws, "EX")
        assert text.count("1.  已知对研究药物任一辅料过敏。") == 1
        assert text.count("3.  PSA 进展，根据 PCWG3 定义。") == 1

    def test_clause_span_stops_before_the_next_clause(self, tmp_path):
        raw_lines = RAW.splitlines()
        spans = bundle.clause_spans(raw_lines)
        assert set(spans) >= {1, 2, 3}
        assert spans[1][1] < spans[2][0], "第 1 条的窗口不得延伸到第 2 条"
        assert spans[2][1] < spans[3][0]


# ─────────────── 条号窗口必须按轨取（会话 a7c19ea1）───────────────

# 入排两段的条号都从 1 起编 —— 这是 raw.md 的常态（该会话实测 IN 段 L11 起、EX 段 L79 起，
# 各自 1．开头）。旧代码无条件「保留最后一个」并注释为"本轨段在后"，只对 EX 成立。
_TWO_SECTION_RAW = """# 入排标准提取

## 入选标准

1．自愿参加临床试验，并签署知情同意书。

2．年龄 18 周岁以上（含）。

## 排除标准

1．已知对研究药物任一辅料过敏。

2．既往接受过贝伐珠单抗治疗。

## 补充章节

### 3.6 研究周期
"""


class TestClauseSpansAreTrackScoped:
    """跑 IN 轨时拿到排除标准的原文 —— 会话 a7c19ea1 现场诊断出的真 bug。

    后果不是报错而是**假阳性**：闸 9 用错误的窗口比对 `原文` 与 raw，报出的差异全是假的，
    而修订子代理会照着这些假差异去改产物。当时因 `/mnt/skills` 只读、且 agent 不知道
    `skill_manage(action="write_file")` 能写 `scripts/`，这个 bug 被诊断出来却没能修。
    """

    def test_in_track_takes_the_inclusion_section(self):
        raw_lines = _TWO_SECTION_RAW.splitlines()
        spans = bundle.clause_spans(raw_lines, "IN")
        first_line = raw_lines[spans[1][0] - 1]
        assert "自愿参加临床试验" in first_line, f"IN 轨条号 1 取到了别的段落：{first_line!r}"

    def test_ex_track_takes_the_exclusion_section(self):
        raw_lines = _TWO_SECTION_RAW.splitlines()
        spans = bundle.clause_spans(raw_lines, "EX")
        first_line = raw_lines[spans[1][0] - 1]
        assert "已知对研究药物任一辅料过敏" in first_line

    def test_the_two_tracks_never_share_a_window(self):
        raw_lines = _TWO_SECTION_RAW.splitlines()
        in_spans = bundle.clause_spans(raw_lines, "IN")
        ex_spans = bundle.clause_spans(raw_lines, "EX")
        assert set(in_spans) == set(ex_spans) == {1, 2}, "两段条号同样起编，键必然重合"
        for num in in_spans:
            assert in_spans[num] != ex_spans[num], f"条号 {num} 在两轨拿到同一窗口"
        assert max(e for _, e in in_spans.values()) < min(s for s, _ in ex_spans.values())

    def test_last_clause_of_a_track_does_not_swallow_the_next_section(self):
        raw_lines = _TWO_SECTION_RAW.splitlines()
        in_spans = bundle.clause_spans(raw_lines, "IN")
        tail = "\n".join(raw_lines[in_spans[2][0] - 1 : in_spans[2][1]])
        assert "排除标准" not in tail
        assert "过敏" not in tail

    def test_unknown_track_falls_back_instead_of_failing(self):
        """raw.md 由上游抽取产生，标题写法可能变 —— 退回旧行为，不要让取证包整体失败。"""
        raw_lines = _TWO_SECTION_RAW.splitlines()
        assert bundle.clause_spans(raw_lines, None) == bundle.clause_spans(raw_lines)

    def test_missing_heading_falls_back_to_whole_document(self):
        raw_lines = ["1．第一条。", "", "2．第二条。"]
        assert set(bundle.clause_spans(raw_lines, "IN")) == {1, 2}

    def test_build_bundle_quotes_the_inclusion_text_for_the_in_track(self, tmp_path):
        """端到端：闸 9 的比对素材必须来自本轨段。"""
        # `_ws` 只写 criteria_parsed_EX.json，这里要的是 IN 轨输入。
        (tmp_path / "eligibility_criteria_raw.md").write_text(_TWO_SECTION_RAW, encoding="utf-8")
        parsed = _parsed(_item("IN-1", "知情同意", "自愿参加临床试验，并签署知情同意书。"), cat="入选_可从病例获取")
        (tmp_path / "criteria_parsed_IN.json").write_text(json.dumps(parsed, ensure_ascii=False), encoding="utf-8")
        text, _ = bundle.build_bundle(tmp_path, "IN")
        assert "自愿参加临床试验" in text
        assert "已知对研究药物任一辅料过敏" not in text, "IN 轨的取证包引了排除标准的原文"


# ─────────────── 转化条件要素与机械预判 ───────────────


class TestTransformElementsAndPreChecks:
    def test_transform_fields_are_surfaced(self, tmp_path):
        ws = _ws(
            tmp_path,
            _parsed(
                _item(
                    "EX-1",
                    "过敏",
                    "已知对研究药物任一辅料过敏。",
                    转化条件={"同义词": ["过敏"], "匹配字段": ["过敏史"], "运算符": "=", "阈值": "有", "除外": "轻度皮疹"},
                )
            ),
        )
        text, _ = bundle.build_bundle(ws, "EX")
        for key in ("同义词", "匹配字段", "运算符", "阈值", "除外"):
            assert key in text, f"缺 {key}，QC 又得自己去读文件"

    def test_text_threshold_is_marked_for_tiering(self, tmp_path):
        """三档判据是首轮必须定档的事，机械提示能省掉一轮空转。"""
        ws = _ws(tmp_path, _parsed(_item("EX-1", "过敏", "已知对研究药物任一辅料过敏。", 转化条件={"阈值": "研究者判断的严重过敏"})))
        text, stats = bundle.build_bundle(ws, "EX")
        assert "需按三档判据定档" in text
        assert "EX-1" in stats["需定档"]

    def test_deterministic_threshold_is_not_flagged(self, tmp_path):
        """实跑发现的假阳：`阈值="有"` 是明确离散值、本来就可执行，不该报"需定档"。

        把假阳报给 QC 等于让它白核一条 —— 而本脚本存在的意义就是减少无谓步数。
        """
        ws = _ws(tmp_path, _parsed(_item("EX-1", "过敏", "已知对研究药物任一辅料过敏。", 转化条件={"阈值": "有"})))
        text, stats = bundle.build_bundle(ws, "EX")
        assert stats["需定档"] == []
        assert "需按三档判据定档" not in text

    def test_dependency_signal_words_are_flagged(self, tmp_path):
        """依赖外部评价/相对比较的措辞必须标出来——那才是三档判据要处理的。"""
        for value in ("按 PCWG3 定义", "研究者判断为严重", "与上次骨扫描相比"):
            ws = _ws(tmp_path, _parsed(_item("EX-1", "x", "已知对研究药物任一辅料过敏。", 转化条件={"阈值": value})))
            _t, stats = bundle.build_bundle(ws, "EX")
            assert stats["需定档"] == ["EX-1"], f"{value!r} 应被标为需定档"

    def test_nonstandard_operator_triggers_gate12_hint(self, tmp_path):
        ws = _ws(tmp_path, _parsed(_item("EX-3", "PSA 进展", "PSA 进展，根据 PCWG3 定义。", 转化条件={"运算符": "进展"})))
        text, stats = bundle.build_bundle(ws, "EX")
        assert "闸12 命中" in text
        assert "EX-3" in stats["闸12命中"]

    def test_gate12_hint_names_the_external_standard(self, tmp_path):
        """命中外部标准名 = 第三档 upstream 的最强线索，必须回报。"""
        ws = _ws(
            tmp_path,
            _parsed(_item("EX-3", "PSA 进展", "PSA 进展，根据 PCWG3 定义。", 转化条件={"运算符": "进展", "阈值": "按 PCWG3 定义"})),
        )
        text, _ = bundle.build_bundle(ws, "EX")
        assert "PCWG3" in text

    def test_standard_operator_gets_no_hint(self, tmp_path):
        ws = _ws(tmp_path, _parsed(_item("EX-1", "过敏", "已知对研究药物任一辅料过敏。", 转化条件={"运算符": "≥", "阈值": 3})))
        text, stats = bundle.build_bundle(ws, "EX")
        assert "闸12 命中" not in text
        assert stats["闸12命中"] == [] and stats["需定档"] == []

    def test_verbatim_check_uses_the_gate9_criteria(self, tmp_path):
        """口径必须复用闸9：空白/全半角差异不算改写，否则会报出一堆假阳让 QC 白核。"""
        ws = _ws(tmp_path, _parsed(_item("EX-1", "过敏", "已知对研究药物任一辅料过敏。", 转化条件={"同义词": ["过敏"]})))
        _text, stats = bundle.build_bundle(ws, "EX")
        assert stats["原文查不到"] == []

    def test_rewritten_original_text_is_flagged_as_blocking(self, tmp_path):
        ws = _ws(tmp_path, _parsed(_item("EX-1", "过敏", "已知对研究药物的辅料存在过敏反应者。", 转化条件={"同义词": ["过敏"]})))
        text, stats = bundle.build_bundle(ws, "EX")
        assert stats["原文查不到"] == ["EX-1"]
        assert "阻断级线索" in text

    def test_or_group_semantics_absence_is_marked(self, tmp_path):
        ws = _ws(tmp_path, _parsed(_item("EX-2-1", "a", "6 个月内心肌梗死", 或组="EX-2-OR")))
        text, _ = bundle.build_bundle(ws, "EX")
        assert "语义缺失" in text

    def test_accessible_item_without_transform_is_marked(self, tmp_path):
        ws = _ws(tmp_path, _parsed(_item("EX-1", "过敏", "已知对研究药物任一辅料过敏。")))
        text, _ = bundle.build_bundle(ws, "EX")
        assert "却无 `转化条件`" in text

    def test_inaccessible_item_without_transform_is_not_marked(self, tmp_path):
        """「不可从病例获取」的 `转化条件` 本就该是 null，不该报警。"""
        ws = _ws(tmp_path, _parsed(_item("EX-4", "研究者判断", "研究者判断不适合入组。"), cat="排除_不可从病例获取"))
        text, _ = bundle.build_bundle(ws, "EX")
        assert "却无 `转化条件`" not in text


# ─────────────── 产物不能变成新的上下文炸弹 ───────────────


class TestBundleStaysSmall:
    def test_raw_window_is_capped(self, tmp_path):
        raw = "# 4.2 排除标准\n\n1.  已知对研究药物任一辅料过敏。\n" + "".join(f"填充行 {i}\n" for i in range(200))
        ws = _ws(tmp_path, _parsed(_item("EX-1", "过敏", "已知对研究药物任一辅料过敏。", 转化条件={"同义词": ["过敏"]})), raw=raw)
        text, _ = bundle.build_bundle(ws, "EX")
        fenced = text.split("```")[1] if "```" in text else ""
        assert len(fenced.strip().splitlines()) <= bundle.MAX_RAW_LINES_PER_GROUP

    def test_total_is_capped_and_keeps_the_irreplaceable_part(self, tmp_path):
        items = [
            _item(f"EX-{i}", "很长的子条件描述" * 20, "已知对研究药物任一辅料过敏。", 转化条件={"同义词": ["过敏"] * 30, "阈值": "文字描述" * 20})
            for i in range(1, 200)
        ]
        ws = _ws(tmp_path, _parsed(*items))
        text, stats = bundle.build_bundle(ws, "EX")
        assert len(text) <= bundle.MAX_BUNDLE_CHARS
        assert stats.get("超长已省略窗口") is True
        assert "按各组给出的行号补读" in text, "省略窗口时必须告诉 QC 怎么补读"
        assert "装配摘要" in text


# ─────────────── 契约与 CLI ───────────────


class TestContractAndCli:
    def test_bundle_states_it_carries_no_verdicts(self, tmp_path):
        ws = _ws(tmp_path, _parsed(_item("EX-1", "过敏", "已知对研究药物任一辅料过敏。", 转化条件={"同义词": ["过敏"]})))
        text, _ = bundle.build_bundle(ws, "EX")
        assert "不含任何 QC 结论" in text

    def test_bundle_bans_per_item_grep_and_inline_python(self, tmp_path):
        ws = _ws(tmp_path, _parsed(_item("EX-1", "过敏", "已知对研究药物任一辅料过敏。", 转化条件={"同义词": ["过敏"]})))
        text, _ = bundle.build_bundle(ws, "EX")
        assert "不要再逐条" in text and "grep" in text
        assert "python3 -c" in text, "必须点名禁止内联 python 只读自检"

    def test_criteria_are_reused_from_the_structure_gate(self):
        """⛔ 判据不得在本脚本里重写 —— 重写就会与闸给出不同结论。"""
        gate = bundle._load_gate_module()
        assert hasattr(gate, "CANONICAL_OPERATORS") and hasattr(gate, "_norm_text")
        assert hasattr(gate, "parse_cid")

    def test_exit_2_when_parsed_file_missing(self, tmp_path):
        assert bundle.main(["--workspace", str(tmp_path), "--track", "EX"]) == 2

    def test_exit_2_on_broken_json(self, tmp_path):
        (tmp_path / "criteria_parsed_EX.json").write_text("{not json", encoding="utf-8")
        assert bundle.main(["--workspace", str(tmp_path), "--track", "EX"]) == 2

    def test_exit_0_even_when_original_text_is_unverifiable(self, tmp_path):
        """查不到原文是 QC 的判断对象，不是装配失败 —— 否则装配阶段就把流程卡死。"""
        ws = _ws(tmp_path, _parsed(_item("EX-1", "x", "原文里没有这句", 转化条件={"同义词": ["x"]})))
        assert bundle.main(["--workspace", str(ws), "--track", "EX"]) == 0
        assert (ws / "criteria_qc_bundle_EX.md").exists()

    def test_default_output_path_follows_the_track(self, tmp_path):
        ws = _ws(tmp_path, _parsed(_item("EX-1", "过敏", "已知对研究药物任一辅料过敏。", 转化条件={"同义词": ["过敏"]})))
        assert bundle.main(["--workspace", str(ws), "--track", "EX"]) == 0
        assert (ws / "criteria_qc_bundle_EX.md").exists()


# ─── 真实 raw.md 形态：带编号前缀的标题 + 段内 #### 小标题（会话 `5aa5d6d6`）───
#
# 上面 `TestClauseSpansAreTrackScoped` 的夹具用的是**裸标题**（`## 入选标准`），而生产的
# raw.md 是 `## 4.1 入选标准` / `## 4.2 排除标准`，排除段里还有 `#### 肿瘤性疾病` 之类的
# 段内小标题。两个 bug 都只在这个形态下发作，所以它们通过了当时的全部测试、活到了生产：
#
#   bug1  `_TRACK_HEADINGS` = `^\s*#{1,6}\s*入选标准\s*$` 不匹配 `## 4.1 入选标准`
#         → 段定位失败 → 退回整篇 → 「同编号保留最后一个」→ IN 轨每条取到排除段原文
#   bug2  end 探测 `#{1,3}\s*\S` 的 `\S` 会接受第 4 个 `#`（回溯）→ `#### 肿瘤性疾病`
#         被当成段边界 → EX 段在第一个小标题处截断 → 该段所有条号定位不到
#
# 两次的表征都不是报错，而是**带着全错的映射 exit 0**。代价：两轨 QC 子代理各自撞上，
# 一个自行绕过、一个据此产出假阳性阻断项（EX-6）；主代理为查 bug 花掉 10 分钟与两次全量
# 脚本重写，13 条真阻断项一条未修。因此除了修正则，还必须让**定位失败本身 exit 2**。

_REAL_RAW = """# 入排标准提取

## 4.1 入选标准

受试者入组须满足以下所有条件：

1．自愿参加临床试验，并签署知情同意书。

2．年龄 18 周岁以上（含）。

## 4.2 排除标准

符合下列任一条件的患者，不得进入本临床研究：

#### 肿瘤性疾病

1．已知对研究药物任一辅料过敏。

2．既往接受过贝伐珠单抗治疗。

#### 基础性疾病

3．合并未控制的高血压。

## 补充章节

### 3.6 研究周期
"""


class TestNumberedHeadingsAndInSectionSubheadings:
    """生产 raw.md 的真实形态。裸标题的用例覆盖不到这里的任何一个 bug。"""

    RAW_LINES = _REAL_RAW.splitlines()

    def test_in_track_heading_with_section_number_is_matched(self):
        """bug1：`## 4.1 入选标准` 必须被认出来，否则整篇回退、原文全错。"""
        spans, problem = bundle.clause_spans_checked(self.RAW_LINES, "IN")
        assert problem is None, problem
        assert "自愿参加临床试验" in self.RAW_LINES[spans[1][0] - 1]

    def test_ex_track_heading_with_section_number_is_matched(self):
        spans, problem = bundle.clause_spans_checked(self.RAW_LINES, "EX")
        assert problem is None, problem
        assert "已知对研究药物任一辅料过敏" in self.RAW_LINES[spans[1][0] - 1]

    def test_bare_headings_still_work(self):
        """编号前缀是**可选**的：老 raw.md 的裸标题不能因为这个修复失效。"""
        spans, problem = bundle.clause_spans_checked(_TWO_SECTION_RAW.splitlines(), "IN")
        assert problem is None
        assert "自愿参加临床试验" in _TWO_SECTION_RAW.splitlines()[spans[1][0] - 1]

    def test_level4_subheading_is_not_a_section_boundary(self):
        """bug2：`#### 肿瘤性疾病` 在段内，不是段边界 —— 否则该段所有条号都定位不到。"""
        spans, problem = bundle.clause_spans_checked(self.RAW_LINES, "EX")
        assert problem is None, problem
        assert set(spans) == {1, 2, 3}, f"段被小标题截断了：{spans}"

    def test_clauses_after_a_level4_subheading_are_still_found(self):
        """第 3 条在第二个 `#### 基础性疾病` 之后 —— 段内小标题不得终止扫描。"""
        spans, _ = bundle.clause_spans_checked(self.RAW_LINES, "EX")
        assert "合并未控制的高血压" in self.RAW_LINES[spans[3][0] - 1]

    def test_level3_heading_still_ends_the_section(self):
        """`###` 仍是段边界：只排除 4 级及以上，不是放弃 end 探测。"""
        raw = ["## 4.2 排除标准", "", "1．第一条。", "", "### 3.6 研究周期", "", "1．这条属于别的章节。"]
        spans, problem = bundle.clause_spans_checked(raw, "EX")
        assert problem is None
        assert set(spans) == {1}
        assert "第一条" in raw[spans[1][0] - 1]

    def test_the_two_tracks_get_different_windows_under_real_headings(self):
        in_spans, _ = bundle.clause_spans_checked(self.RAW_LINES, "IN")
        ex_spans, _ = bundle.clause_spans_checked(self.RAW_LINES, "EX")
        for num in set(in_spans) & set(ex_spans):
            assert in_spans[num] != ex_spans[num], f"条号 {num} 在两轨拿到同一窗口"
        assert max(e for _, e in in_spans.values()) < min(s for s, _ in ex_spans.values())


class TestMappingFailureIsBlocking:
    """⛔ 段定位失败必须 exit 2 —— 静默退回整篇是两个 bug 都能通过的那道门。

    为什么不能只在产物里写一行警告：警告要靠 QC 子代理读到并正确解读，而实测两个 QC
    子代理面对同一份错误映射给出了两种反应（一个绕过、一个据此报假阳性阻断项）。
    """

    def _ws_in(self, tmp_path: Path, raw: str) -> Path:
        (tmp_path / "eligibility_criteria_raw.md").write_text(raw, encoding="utf-8")
        (tmp_path / "criteria_parsed_IN.json").write_text(
            json.dumps(
                {"四分类": {"入选_可从病例获取": {"IN-1": {"条件ID": "IN-1", "子条件": "签署知情同意书", "原文": "1．自愿参加临床试验，并签署知情同意书。"}}, "入选_不可从病例获取": {}}},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return tmp_path

    def test_unrecognised_heading_reports_a_problem(self):
        raw = ["# 提取", "", "## 【入选】", "", "1．自愿参加临床试验，并签署知情同意书。"]
        _, problem = bundle.clause_spans_checked(raw, "IN")
        assert problem and "找不到 IN 轨段标题" in problem, problem

    def test_section_without_any_clause_reports_a_problem(self):
        """段边界被截断的形态（bug2）：段内一个条号都没有，而全篇有。"""
        raw = ["## 4.2 排除标准", "", "#### 肿瘤性疾病", "", "1．已知对研究药物任一辅料过敏。"]
        # 人为退回旧的 end 探测正则，模拟 bug2
        import re

        original = bundle._HEADING_LINE
        try:
            bundle._HEADING_LINE = re.compile(r"^\s*#{1,3}\s*\S")
            spans, problem = bundle.clause_spans_checked(raw, "EX")
        finally:
            bundle._HEADING_LINE = original
        assert spans == {}
        assert problem and "找不到任何 `N．` 条号行" in problem, problem

    def test_build_bundle_raises_instead_of_producing_wrong_evidence(self, tmp_path):
        ws = self._ws_in(tmp_path, "# 提取\n\n## 【入选】\n\n1．自愿参加临床试验，并签署知情同意书。\n")
        with pytest.raises(bundle.MappingUntrusted):
            bundle.build_bundle(ws, "IN")

    def test_cli_exits_2_and_names_the_cause(self, tmp_path, capsys):
        ws = self._ws_in(tmp_path, "# 提取\n\n## 【入选】\n\n1．自愿参加临床试验，并签署知情同意书。\n")
        assert bundle.main(["--workspace", str(ws), "--track", "IN"]) == 2
        err = capsys.readouterr().err
        assert "段定位不可信" in err
        assert "找不到 IN 轨段标题" in err
        assert "不要跳过取证包直接派 QC" in err

    def test_no_bundle_is_written_on_mapping_failure(self, tmp_path):
        ws = self._ws_in(tmp_path, "# 提取\n\n## 【入选】\n\n1．自愿参加临床试验，并签署知情同意书。\n")
        bundle.main(["--workspace", str(ws), "--track", "IN"])
        assert not (ws / "criteria_qc_bundle_IN.md").exists()

    def test_stale_bundle_from_a_previous_run_is_removed(self, tmp_path):
        """留着上一轮的产物 = 让本轮的 exit 2 被一份过期素材包掩盖（QC 前置只看文件在不在）。"""
        ws = self._ws_in(tmp_path, _REAL_RAW)
        assert bundle.main(["--workspace", str(ws), "--track", "IN"]) == 0
        stale = ws / "criteria_qc_bundle_IN.md"
        assert stale.exists()
        before = stale.read_text(encoding="utf-8")

        (ws / "eligibility_criteria_raw.md").write_text(_REAL_RAW.replace("## 4.1 入选标准", "## 【入选】"), encoding="utf-8")
        assert bundle.main(["--workspace", str(ws), "--track", "IN"]) == 2
        assert not stale.exists(), "过期产物没被删除，QC 会拿它当本轮素材"
        assert before  # 上一轮确实产出过，不是从来没写成功

    def test_explicit_out_path_is_also_cleaned(self, tmp_path):
        ws = self._ws_in(tmp_path, _REAL_RAW)
        out = tmp_path / "nested" / "b.md"
        assert bundle.main(["--workspace", str(ws), "--track", "IN", "--out", str(out)]) == 0
        assert out.exists()
        (ws / "eligibility_criteria_raw.md").write_text("# 提取\n\n## 【入选】\n\n1．自愿参加临床试验，并签署知情同意书。\n", encoding="utf-8")
        assert bundle.main(["--workspace", str(ws), "--track", "IN", "--out", str(out)]) == 2
        assert not out.exists()

    def test_missing_raw_file_is_not_a_mapping_failure(self, tmp_path):
        """raw.md 缺失时全篇没有原文可对照，每组如实标「未能定位」，闸 9 会独立报缺原文。

        这不该被本闸拦住 —— 否则 raw 抽取还没跑完就把装配阶段卡死。
        """
        (tmp_path / "criteria_parsed_IN.json").write_text(
            json.dumps({"四分类": {"入选_可从病例获取": {"IN-1": {"条件ID": "IN-1", "子条件": "x", "原文": "y"}}, "入选_不可从病例获取": {}}}, ensure_ascii=False),
            encoding="utf-8",
        )
        assert bundle.main(["--workspace", str(tmp_path), "--track", "IN"]) == 0

    def test_raw_without_any_clause_line_is_not_a_mapping_failure(self, tmp_path):
        """全篇一个条号都没有 = 上游抽取问题，不是段定位问题（`marks` 为空时不报）。"""
        ws = self._ws_in(tmp_path, "# 提取\n\n## 4.1 入选标准\n\n本段没有任何编号条目。\n")
        assert bundle.main(["--workspace", str(ws), "--track", "IN"]) == 0

    def test_clause_spans_wrapper_keeps_the_old_signature(self):
        """旧封装仍返回纯 dict —— 其他调用方（若有）不因本次改动而破。"""
        spans = bundle.clause_spans(_REAL_RAW.splitlines(), "EX")
        assert isinstance(spans, dict) and set(spans) == {1, 2, 3}
        assert bundle.clause_spans(_REAL_RAW.splitlines(), None) == bundle.clause_spans(_REAL_RAW.splitlines())
