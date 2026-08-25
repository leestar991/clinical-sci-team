"""uncertain_recheck 反查闸:必须覆盖「不可从病例获取」条目。

**真实故障(受试者 XS-03-II201_S042002,IN-1「自愿参加临床试验并签署知情同意书」)**：

`criteria-parser` 把 IN-1 归入 `入选_不可从病例获取`(`可从病例获取: false`、`转化条件: null`)，
判定子代理据此**没有真正检索病历**，两份文档的 reason 都写"已查筛选期检查，未见知情同意签署
记录"(证据其实在**筛选期病历**里)，evidence 还写了一句 `未见知情同意相关记录。` `hit: false`。

而该患者 `筛选期病历/ocr_records.md` 里有明确记录：
    知情同意书签署=2026-04-15 16:21; 研究医生签署=2026-04-15 16:25; 筛选号=S042002
    ...患者经过充分考虑后表示完全理解并自愿参加本研究。
    知情同意书一式两份，一份知情同意书由受试者保存...

正确结论应为**符合**。四道闸全部放过，根因是本脚本第 166 行
`if not item or not item.get("可从病例获取"): continue`(注释："不可获取（如知情同意）跳过")：
**「不可从病例获取」条目被整体豁免机械反查**，于是 `suspected_missed` 为空、QC 也看不到。

修复要求(本文件锁定)：
1. 反查覆盖**全部**「无法判断」条目，不再按 `可从病例获取` 豁免。
2. 这类条目 `转化条件` 为 null，`同义词`/`匹配字段` 都没有 → 必须从 `子条件`/`原文` 派生关键词。
3. 产物标注 `可从病例获取` 与 `anchor_source`，让 QC 知道该条命中的证据强度。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "skills" / "custom" / "eligibility-judgment" / "scripts" / "uncertain_recheck.py"

if not SCRIPT_PATH.exists():  # skills/custom 为 gitignore 目录
    pytest.skip("eligibility-judgment 技能未安装", allow_module_level=True)


def _load():
    spec = importlib.util.spec_from_file_location("uncertain_recheck", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gate = _load()


# --- 夹具 -------------------------------------------------------------------


def criteria(*items, bucket: str = "入选_可从病例获取") -> dict:
    return {"四分类": {bucket: {str(i["条件ID"]): i for i in items}}}  # 类目规范形态：以 条件ID 为键的 dict（数组只是旧 workspace 的只读兼容形态）


def cond(cid, 子条件, *, 可获取=True, 同义词=None, 匹配字段=None, 原文=None) -> dict:
    item = {"条件ID": cid, "子条件": 子条件, "原文": 原文 or 子条件, "可从病例获取": 可获取}
    t = {}
    if 同义词 is not None:
        t["同义词"] = 同义词
    if 匹配字段 is not None:
        t["匹配字段"] = 匹配字段
    item["转化条件"] = t or None
    return item


def judgments(entries: dict, doc: str = "筛选期病历") -> dict:
    return {"patient_id": "S042002", "judgments": entries}  # 统一证据源判定产物：顶层 judgments


def uncertain(reason: str = "已查病历，未见记录；缺X；需补充Y。") -> dict:
    return {"conclusion": "无法判断", "reason": reason, "evidence": []}


def ocr(tmp_path: Path, text: str, source: str = "筛选期病历") -> Path:
    d = tmp_path / source
    d.mkdir(parents=True, exist_ok=True)
    p = d / "ocr_records.md"
    p.write_text(text, encoding="utf-8")
    return p


# --- 真实故障回归:IN-1 知情同意 --------------------------------------------

S042002_ICF_OCR = """\
| 项目 | 内容 |
| 入选前筛选 | 受试者于门诊就诊，研究者向其详细介绍本研究目的、流程与风险，并提供知情同意书。 |
| 知情过程 | 知情同意书签署=2026-04-15 16:21; 研究医生签署=2026-04-15 16:25; 筛选号=S042002 |
| 备注 | 研究者已充分讲解知情同意书内容，患者未提出问题，患者经过充分考虑后表示完全理解并自愿参加本研究。 |
| 文件留存 | 知情同意书一式两份，一份知情同意书由受试者保存，一份知情同意书保存在受试者文件夹中。 |
"""


def test_in1_informed_consent_is_flagged_as_missed(tmp_path: Path):
    """核心回归:知情同意在病历里有明确签署记录,却被判无法判断 → 必须报漏判。"""
    crit = criteria(
        cond("IN-1", "自愿参加临床试验并签署知情同意书", 可获取=False),
        bucket="入选_不可从病例获取",
    )
    jdg = judgments({"IN-1": uncertain("已查筛选期检查，未见知情同意签署记录；缺受试者签署知情同意书信息；需补充知情同意文件。")})
    report = gate.recheck(crit, jdg, [ocr(tmp_path, S042002_ICF_OCR)])

    assert report["suspected_missed"] == ["IN-1"], "「不可从病例获取」条目不得豁免机械反查"
    entry = next(e for e in report["entries"] if e["条件ID"] == "IN-1")
    assert entry["hit"] is True
    assert entry["grep_hits"], "必须给出 OCR 原文命中行，供改判时引用"
    assert any("知情同意书签署" in h["text"] for h in entry["grep_hits"])


def test_unobtainable_conditions_are_still_checked(tmp_path: Path):
    crit = criteria(cond("IN-1", "自愿参加临床试验并签署知情同意书", 可获取=False), bucket="入选_不可从病例获取")
    jdg = judgments({"IN-1": uncertain()})
    report = gate.recheck(crit, jdg, [ocr(tmp_path, "无关内容")])
    assert report["checked"] == 1, "无命中也要计入 checked，证明确实查过"
    assert report["suspected_missed"] == []


def test_entry_records_obtainability_and_anchor_source(tmp_path: Path):
    """QC 需要知道这条命中的关键词是策展词表还是子条件回退。"""
    crit = {
        "四分类": {
            "入选_可从病例获取": [cond("IN-9", "基线期ECOG评分为0或1", 同义词=["ECOG", "体力状态"])],
            "入选_不可从病例获取": [cond("IN-1", "自愿参加临床试验并签署知情同意书", 可获取=False)],
        }
    }
    jdg = judgments({"IN-1": uncertain(), "IN-9": uncertain()})
    report = gate.recheck(crit, jdg, [ocr(tmp_path, S042002_ICF_OCR + "\n| ECOG评分 | 1分 |")])
    by_id = {e["条件ID"]: e for e in report["entries"]}

    assert by_id["IN-1"]["可从病例获取"] is False
    assert by_id["IN-1"]["anchor_source"] == "子条件"
    assert by_id["IN-9"]["可从病例获取"] is True
    assert by_id["IN-9"]["anchor_source"] == "转化条件"
    assert sorted(report["suspected_missed"]) == ["IN-1", "IN-9"]


# --- 子条件关键词派生 -------------------------------------------------------


def test_subcondition_keywords_skip_generic_words(tmp_path: Path):
    """只命中「研究者/判断/试验」等通用词不算命中,否则每条兜底条款都会误报。"""
    crit = criteria(
        cond("EX-17", "研究者认为受试者存在其他不适合参加本试验的情况", 可获取=False),
        bucket="排除_不可从病例获取",
    )
    jdg = judgments({"EX-17": uncertain()})
    text = "研究者已完成评估，本试验流程如下，受试者签署文件。判断结论见附页。"
    report = gate.recheck(crit, jdg, [ocr(tmp_path, text)])
    assert report["suspected_missed"] == [], "通用词命中不得升级为漏判"


def test_subcondition_keywords_catch_domain_terms(tmp_path: Path):
    crit = criteria(cond("IN-8", "预计生存期≥12周", 可获取=False), bucket="入选_不可从病例获取")
    jdg = judgments({"IN-8": uncertain()})
    report = gate.recheck(crit, jdg, [ocr(tmp_path, "| 评估 | 预计生存期大于 6 个月 |")])
    assert report["suspected_missed"] == ["IN-8"]


def test_condition_without_usable_keywords_is_marked(tmp_path: Path):
    """派生不出可用关键词时必须显式标记,不能假装查过。"""
    crit = criteria(cond("IN-X", "研究者判断", 可获取=False), bucket="入选_不可从病例获取")
    jdg = judgments({"IN-X": uncertain()})
    report = gate.recheck(crit, jdg, [ocr(tmp_path, "任意内容")])
    entry = next(e for e in report["entries"] if e["条件ID"] == "IN-X")
    assert entry["keywords"] == []
    assert entry.get("no_keywords") is True
    assert report["suspected_missed"] == []


# --- 既有行为不得回归 -------------------------------------------------------


def test_existing_obtainable_path_still_works(tmp_path: Path):
    crit = criteria(cond("IN-9", "基线期ECOG评分为0或1", 同义词=["ECOG"]))
    jdg = judgments({"IN-9": uncertain()})
    report = gate.recheck(crit, jdg, [ocr(tmp_path, "体格检查 ECOG评分：1分")])
    assert report["suspected_missed"] == ["IN-9"]


def test_non_uncertain_judgments_are_ignored(tmp_path: Path):
    crit = criteria(cond("IN-1", "自愿参加临床试验并签署知情同意书", 可获取=False), bucket="入选_不可从病例获取")
    jdg = judgments({"IN-1": {"conclusion": "符合", "reason": "已签署", "evidence": []}})
    report = gate.recheck(crit, jdg, [ocr(tmp_path, S042002_ICF_OCR)])
    assert report["checked"] == 0
    assert report["suspected_missed"] == []


def test_builtin_scale_synonyms_still_expand(tmp_path: Path):
    crit = criteria(cond("IN-9", "基线期ECOG评分为0或1", 匹配字段="ECOG评分"))
    jdg = judgments({"IN-9": uncertain()})
    report = gate.recheck(crit, jdg, [ocr(tmp_path, "一般情况可")])
    assert report["suspected_missed"] == ["IN-9"], "内置量表同义词（一般情况）应仍然展开"


def test_unified_judgments_reported_once(tmp_path: Path):
    """统一证据源判定：一条条件只有一条判定条目，checked=1，不再有 per-物料重复。"""
    crit = criteria(cond("IN-1", "自愿参加临床试验并签署知情同意书", 可获取=False), bucket="入选_不可从病例获取")
    jdg = {"patient_id": "S042002", "judgments": {"IN-1": uncertain()}}
    report = gate.recheck(crit, jdg, [ocr(tmp_path, S042002_ICF_OCR)])
    assert report["suspected_missed"] == ["IN-1"]
    assert report["checked"] == 1


# --- 拒绝真空通过（会话 9a83ccc9 根因）--------------------------------------
#
# 判定子代理自创 schema（顶层 `judgments` 写成列表）后，本闸读不到任何判定条目，
# `checked=0` 与「本轨确实没有无法判断条目」在产物上长得一模一样，于是报「反查通过」。
# 「一条判定都读不到」必须自己出声，不能假定结构闸一定被跑过。


def test_unreadable_judgments_is_reported_as_blocking(tmp_path: Path):
    crit = criteria(cond("IN-9", "基线期ECOG评分为0或1", 匹配字段="ECOG评分"))
    invented = {
        "patient_id": "S042002",
        "criteria_track": "入选",
        "judgments": [{"条件ID": "IN-9", "判定": "无法判断"}],  # 列表而非 documents 嵌套 dict
    }
    report = gate.recheck(crit, invented, [ocr(tmp_path, "一般情况可")])
    assert report["judgments_seen"] == 0
    assert report["unreadable_judgments"] is True
    assert gate.main is not None  # CLI 存在（下面单独验退出码）


def test_zero_uncertain_with_readable_judgments_is_a_real_pass(tmp_path: Path):
    """本轨确实没有「无法判断」条目时不得误报——与「读不到条目」必须可区分。"""
    crit = criteria(cond("IN-9", "基线期ECOG评分为0或1", 匹配字段="ECOG评分"))
    jdg = judgments({"IN-9": {"conclusion": "符合", "reason": "ECOG 1分", "evidence": []}})
    report = gate.recheck(crit, jdg, [ocr(tmp_path, "ECOG评分：1分")])
    assert report["checked"] == 0
    assert report["judgments_seen"] == 1
    assert report["unreadable_judgments"] is False


def test_cli_exits_2_when_judgments_unreadable(tmp_path: Path):
    crit_p = tmp_path / "criteria.json"
    jud_p = tmp_path / "judgments.json"
    out_p = tmp_path / "recheck.json"
    crit_p.write_text(
        json.dumps(criteria(cond("IN-9", "基线期ECOG评分为0或1", 匹配字段="ECOG评分")), ensure_ascii=False),
        encoding="utf-8",
    )
    jud_p.write_text(json.dumps({"judgments": [{"条件ID": "IN-9"}]}, ensure_ascii=False), encoding="utf-8")
    rc = gate.main(
        ["--criteria", str(crit_p), "--judgments", str(jud_p), "--ocr", str(ocr(tmp_path, "x")), "--out", str(out_p)]
    )
    assert rc == 2
    assert json.loads(out_p.read_text(encoding="utf-8"))["unreadable_judgments"] is True


# --- CLI --------------------------------------------------------------------


def test_cli_writes_report(tmp_path: Path):
    crit_p = tmp_path / "criteria.json"
    crit_p.write_text(
        json.dumps(
            criteria(cond("IN-1", "自愿参加临床试验并签署知情同意书", 可获取=False), bucket="入选_不可从病例获取"),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    jdg_p = tmp_path / "judgments.json"
    jdg_p.write_text(json.dumps(judgments({"IN-1": uncertain()}), ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "uncertain_recheck.json"

    code = gate.main(
        ["--criteria", str(crit_p), "--judgments", str(jdg_p), "--ocr", str(ocr(tmp_path, S042002_ICF_OCR)), "--out", str(out)]
    )
    assert code == 0, "本闸始终 exit 0，结论以 JSON 的 suspected_missed 为准"
    assert json.loads(out.read_text(encoding="utf-8"))["suspected_missed"] == ["IN-1"]


# --- 覆盖「存疑」（会话 `1fee1395` EX-1-3）----------------------------------
#
# 该闸原先只对「无法判断」触发。会话 `1fee1395` 的 EX-1-3 判的是「存疑」，于是它连反查都没进：
# reason 里那句事实错误的「无全身性糖皮质激素或生物制剂处方」（OCR 明写 `地塞米松`）
# 就这样穿过了全部四道闸。
#
# 分级必须与「无法判断」区分开：
# - 「无法判断」命中 → `suspected_missed`（**阻断级**：该判「符合/不符合/存疑」却判了没内容）；
# - 「存疑」命中   → `uncertain_hits`（**建议级**）。因为「存疑」本身可能完全正确 ——
#   按 SKILL 原则十一 B 的三步判据，地塞米松治的是尿频尿急、落不到判据②，EX-1-3 判存疑是对的。
#   命中只回答「①/归类」，**不回答②的针对性**；若把它当阻断级，QC 就会把正确的 `存疑`
#   推成 `不符合`，反而制造错误排除。

DEXAMETHASONE_OCR = """\
处理：哮喘，临床试验筛选失败，建议更换氘恩扎如胺。用药前查PSA。
2025.04.09起地塞米松及青霉素治疗，尿频尿急改善，尿痛有好转
目前骨转移灶及前列腺原发灶中等分割放疗中，已9次。2025.04.09-05.19放疗
"""


def suspicious(reason: str = "哮喘病史成立；无全身性糖皮质激素或生物制剂处方；缺哮喘当前治疗方案。") -> dict:
    return {"conclusion": "存疑", "reason": reason, "evidence": []}


def _asthma_condition():
    return cond(
        "EX-1-3",
        "有特异性变态反应病史（哮喘、风湿、湿疹性皮炎）且仍需全身性药物治疗",
        同义词=["哮喘", "地塞米松", "全身性糖皮质激素", "泼尼松"],
        匹配字段=["既往史", "用药史"],
    )


def test_uncertain_conclusion_enters_the_recheck(tmp_path: Path):
    """「存疑」条目必须进反查——此前完全不进。"""
    r = gate.recheck(
        criteria(_asthma_condition(), bucket="排除_可从病例获取"),
        judgments({"EX-1-3": suspicious()}),
        [ocr(tmp_path, DEXAMETHASONE_OCR)],
    )
    assert r["checked"] == 1, f"存疑条目未进反查：{r}"
    e = r["entries"][0]
    assert e["条件ID"] == "EX-1-3"
    assert e["conclusion"] == "存疑", "产物须标明该条是哪种结论，否则 QC 分不清分级"
    assert e["hit"] is True
    assert any("地塞米松" in h["text"] for h in e["grep_hits"])


def test_uncertain_hit_is_advisory_not_blocking(tmp_path: Path):
    """存疑命中进 `uncertain_hits`（建议级），不得混进 `suspected_missed`（阻断级）。"""
    r = gate.recheck(
        criteria(_asthma_condition(), bucket="排除_可从病例获取"),
        judgments({"EX-1-3": suspicious()}),
        [ocr(tmp_path, DEXAMETHASONE_OCR)],
    )
    assert r["suspected_missed"] == [], "存疑命中不得进阻断级清单"
    assert r["uncertain_hits"] == ["EX-1-3"]


def test_unable_to_judge_hit_still_blocking(tmp_path: Path):
    """既有阻断级行为不得回归。"""
    r = gate.recheck(
        criteria(_asthma_condition(), bucket="排除_可从病例获取"),
        judgments({"EX-1-3": uncertain()}),
        [ocr(tmp_path, DEXAMETHASONE_OCR)],
    )
    assert r["suspected_missed"] == ["EX-1-3"]
    assert r["uncertain_hits"] == []


def test_uncertain_hits_carry_the_targeting_caveat(tmp_path: Path):
    """产物必须自带提示：命中只回答①/归类，不回答②的针对性。"""
    r = gate.recheck(
        criteria(_asthma_condition(), bucket="排除_可从病例获取"),
        judgments({"EX-1-3": suspicious()}),
        [ocr(tmp_path, DEXAMETHASONE_OCR)],
    )
    note = r.get("uncertain_hits_note", "")
    assert "针对" in note and "原则十一" in note, f"缺针对性提示：{note!r}"


def test_confirmed_conclusions_never_enter_the_recheck(tmp_path: Path):
    """`符合`/`不符合` 是对事实的断言，不在本闸范围（否则每条都被刷）。"""
    entries = {
        "EX-1-3": {"conclusion": "不符合", "reason": "长期口服泼尼松控制哮喘。", "evidence": []},
    }
    r = gate.recheck(
        criteria(_asthma_condition(), bucket="排除_可从病例获取"),
        judgments(entries),
        [ocr(tmp_path, DEXAMETHASONE_OCR)],
    )
    assert r["checked"] == 0
    assert r["suspected_missed"] == [] and r["uncertain_hits"] == []


def test_cli_reports_uncertain_hits_separately(tmp_path: Path, capsys):
    """CLI 输出必须把两级分开，否则 QC 会按阻断级处置存疑项。"""
    c = tmp_path / "criteria.json"
    c.write_text(json.dumps(criteria(_asthma_condition(), bucket="排除_可从病例获取"), ensure_ascii=False), encoding="utf-8")
    j = tmp_path / "judgments.json"
    j.write_text(json.dumps(judgments({"EX-1-3": suspicious()}), ensure_ascii=False), encoding="utf-8")
    o = ocr(tmp_path, DEXAMETHASONE_OCR)
    out = tmp_path / "recheck.json"
    code = gate.main(["--criteria", str(c), "--judgments", str(j), "--ocr", str(o), "--out", str(out)])
    assert code == 0, "存疑命中是建议级，不得改变退出码"
    text = capsys.readouterr().out
    assert "存疑" in text and "EX-1-3" in text
    assert "建议" in text or "不必改判" in text


# ===========================================================================
# Task 8：误报收紧四项
# ===========================================================================
#
# 会话 `2d628340` / `d393714d` 的实测误报，逐类锁定。收紧的红线是**召回不得下降**：
# 每一类都配一条「真实漏判仍必须报出」的反向用例，否则这个闸就从"吵"变成"瞎"。


class TestLabReferenceRangeIsNotAHit:
    """lab 参考值区间不是入排命中。

    `男≤26`、`男 0-7` 是化验单的**参考范围**列，不是"该患者有这件事"的记录。把它当命中，
    agent 就得花大量步数自证误报（`2d628340` step 86-93：连续 6 个 grep + 6 个空 AI 步）。
    """

    LAB_OCR = """\
| 项目 | 结果 | 参考范围 |
| 谷丙转氨酶 ALT | 18 | 男≤26 女≤22 |
| 中性粒细胞 | 3.2 | 男 0-7 |
| 前列腺特异抗原 | 1.1 | 男 6-17 |
"""

    def _cond(self):
        return cond("IN-5", "男性受试者", 同义词=["男"])

    def test_reference_range_line_is_not_a_hit(self, tmp_path: Path):
        report = gate.recheck(
            criteria(self._cond()),
            judgments({"IN-5": uncertain()}),
            [ocr(tmp_path, self.LAB_OCR)],
        )
        assert report["suspected_missed"] == [], f"参考范围命中不该报漏判：{report['entries'][0]['grep_hits']}"

    def test_real_gender_record_is_still_a_hit(self, tmp_path: Path):
        """反向用例：病历里真写了性别，仍必须报出。"""
        real = "| 基本信息 | 性别=男; 年龄=62 岁 |\n"
        report = gate.recheck(
            criteria(self._cond()),
            judgments({"IN-5": uncertain()}),
            [ocr(tmp_path, real)],
        )
        assert report["suspected_missed"] == ["IN-5"], "真实性别记录必须仍被抓到"

    def test_mixed_document_keeps_the_real_hit(self, tmp_path: Path):
        both = self.LAB_OCR + "| 基本信息 | 性别=男 |\n"
        report = gate.recheck(
            criteria(self._cond()),
            judgments({"IN-5": uncertain()}),
            [ocr(tmp_path, both)],
        )
        assert report["suspected_missed"] == ["IN-5"]
        sources = [h["text"] for h in report["entries"][0]["grep_hits"]]
        assert not any("参考范围" in t for t in sources), "命中行里不该再混入参考范围行"


class TestDrugClassKeywordsAreWordScoped:
    """类别短语不单独作为命中依据；具体药名仍然算。

    `新型内分泌治疗` 这类宽泛短语命中的往往是标准原文的复述段，而非患者用药记录
    （`2d628340` §3.2）。但 `阿比特龙` / `恩扎卢胺` 命中必须仍然报——那才是真实用药。
    """

    def _cond(self):
        return cond("IN-9", "既往接受新型内分泌治疗", 匹配字段=["内分泌治疗"])

    def test_class_phrase_alone_is_not_a_hit(self, tmp_path: Path):
        text = "| 既往治疗 | 患者既往未接受任何新型内分泌治疗相关药物 |\n"
        report = gate.recheck(
            criteria(self._cond()),
            judgments({"IN-9": uncertain()}),
            [ocr(tmp_path, text)],
        )
        hits = report["entries"][0]["grep_hits"]
        assert all("新型内分泌治疗" not in h["text"] or "阿比特龙" in h["text"] for h in hits), f"宽泛类别短语不应单独构成命中：{hits}"

    def test_specific_drug_name_is_still_a_hit(self, tmp_path: Path):
        text = "| 用药史 | 2025-03 起口服阿比特龙 1000mg qd |\n"
        report = gate.recheck(
            criteria(self._cond()),
            judgments({"IN-9": uncertain()}),
            [ocr(tmp_path, text)],
        )
        assert report["suspected_missed"] == ["IN-9"], "具体药名命中必须仍报漏判"


class TestUnifiedEvidenceSourceHits:
    """统一证据源判定：同一患者全部 OCR 材料是共享证据，**任何物料**的命中都构成本条目漏判。

    历史故障 `d393714d` step 148-152 是 per-物料判定时代的产物（病历命中被标到检查条目上）；
    统一判定后一条条件只有一条判定，不再有「跨文档误标」问题。
    """

    def test_hit_from_any_material_is_reported(self, tmp_path: Path):
        record = ocr(tmp_path, "| 既往史 | 明确记载 ECOG 评分 1 分 |\n", source="筛选期病历")
        exam = ocr(tmp_path, "| 血常规 | 白细胞 5.6 |\n", source="筛选期检查")
        report = gate.recheck(
            criteria(cond("IN-7", "ECOG 评分 0-1", 匹配字段=["ECOG"])),
            judgments({"IN-7": uncertain()}),
            [record, exam],
        )
        assert report["suspected_missed"] == ["IN-7"], "任一物料的命中都是统一判定的漏判证据"
        # 无「跨文档」概念：不产生跨文档留痕
        assert not report["entries"][0].get("cross_document_hits")

    def test_unmatchable_source_labels_do_not_silently_drop_hits(self, tmp_path: Path):
        """来源标签不可比时，必须退化为**不过滤**，而不是丢掉全部命中。"""
        stray = tmp_path / "ocr_records.md"
        stray.write_text("| 既往史 | ECOG 评分 1 分 |\n", encoding="utf-8")
        report = gate.recheck(
            criteria(cond("IN-7", "ECOG 评分 0-1", 匹配字段=["ECOG"])),
            judgments({"IN-7": uncertain()}),
            [stray],
        )
        assert report["suspected_missed"] == ["IN-7"], "标签不可比时本闸不得静默失效"


# ===========================================================================
# Task 7：轮次账本 + 分级熔断
# ===========================================================================
#
# `uncertain_recheck.py` 原本无跨轮次状态，于是「跑闸 → 改产物 → 再跑闸」可以无限重复
# （`2d628340` 12 次、`d393714d` 8 次）。复用 `check_track_structure.py` 闸8 的范式：
# 把每轮结果写进账本，同一集合连续 N 轮未清即升级。
#
# 分级（贯穿主计划的决策 2=c）：
#   - 阻断级 `suspected_missed` 触顶 → exit 3 + `stuck_items`，要求**失败上报**，
#     ⛔ 不允许静默降级（那会把漏判放过去）。
#   - 建议级 `uncertain_hits` 触顶 → exit 0 + 降级指令（标 `存疑` + `gate_escalated`）。


def _hit_setup(tmp_path: Path):
    """一个稳定命中的场景：结论「无法判断」但 OCR 里确有记录。"""
    return (
        criteria(cond("IN-1", "自愿参加临床试验并签署知情同意书", 可获取=False)),
        judgments({"IN-1": uncertain()}),
        [ocr(tmp_path, S042002_ICF_OCR)],
    )


class TestRoundLedger:
    def test_first_round_has_no_stuck_items(self, tmp_path: Path):
        crit, jdg, ocrs = _hit_setup(tmp_path)
        report = gate.recheck(crit, jdg, ocrs, history_path=tmp_path / "hist.json")
        assert report["suspected_missed"] == ["IN-1"]
        assert report.get("stuck_items") in (None, [])
        assert report.get("gate_escalated") is not True

    def test_same_set_three_rounds_escalates(self, tmp_path: Path):
        crit, jdg, ocrs = _hit_setup(tmp_path)
        hist = tmp_path / "hist.json"
        reports = [gate.recheck(crit, jdg, ocrs, history_path=hist) for _ in range(3)]
        assert reports[0].get("gate_escalated") is not True
        assert reports[1].get("gate_escalated") is not True
        assert reports[2]["gate_escalated"] is True, "同一集合第 3 轮必须升级"
        assert reports[2]["stuck_items"] == ["IN-1"]

    def test_changed_set_resets_the_counter(self, tmp_path: Path):
        """集合变化说明修订确实在推进，计数必须归零。"""
        crit, jdg, ocrs = _hit_setup(tmp_path)
        hist = tmp_path / "hist.json"
        gate.recheck(crit, jdg, ocrs, history_path=hist)
        gate.recheck(crit, jdg, ocrs, history_path=hist)

        cleared = judgments({"IN-1": {"conclusion": "符合", "reason": "已签署", "evidence": []}})
        gate.recheck(crit, cleared, ocrs, history_path=hist)

        again = gate.recheck(crit, jdg, ocrs, history_path=hist)
        assert again.get("gate_escalated") is not True, "集合清空过一次后应重新计数"

    def test_corrupt_ledger_resets_safely(self, tmp_path: Path):
        hist = tmp_path / "hist.json"
        hist.write_text("{not json", encoding="utf-8")
        crit, jdg, ocrs = _hit_setup(tmp_path)
        report = gate.recheck(crit, jdg, ocrs, history_path=hist)
        assert report["suspected_missed"] == ["IN-1"], "账本坏掉不得让主闸失效"
        assert any("账本" in n for n in report.get("notes", [])), f"应说明账本被重置：{report.get('notes')}"

    def test_ledger_is_bounded(self, tmp_path: Path):
        crit, jdg, ocrs = _hit_setup(tmp_path)
        hist = tmp_path / "hist.json"
        for _ in range(30):
            gate.recheck(crit, jdg, ocrs, history_path=hist)
        rounds = json.loads(hist.read_text(encoding="utf-8"))
        assert len(rounds) <= gate.MAX_HISTORY_ROUNDS


class TestEscalationIsGraded:
    def test_blocking_escalation_exits_3(self, tmp_path: Path, capsys):
        crit, jdg, ocrs = _hit_setup(tmp_path)
        c = tmp_path / "criteria.json"
        j = tmp_path / "judgments.json"
        c.write_text(json.dumps(crit, ensure_ascii=False), encoding="utf-8")
        j.write_text(json.dumps(jdg, ensure_ascii=False), encoding="utf-8")
        out = tmp_path / "recheck.json"
        hist = tmp_path / "hist.json"

        codes = [
            gate.main(
                [
                    "--criteria",
                    str(c),
                    "--judgments",
                    str(j),
                    "--ocr",
                    str(ocrs[0]),
                    "--out",
                    str(out),
                    "--history",
                    str(hist),
                ]
            )
            for _ in range(3)
        ]
        assert codes[:2] == [0, 0]
        assert codes[2] == 3, "阻断级触顶必须以 exit 3 要求上报，而不是继续让 agent 改产物"
        printed = capsys.readouterr().out
        assert "stuck" in printed.lower() or "卡住" in printed
        assert "禁止" in printed, "必须明确禁止继续改写绕闸"

    def test_advisory_escalation_stays_exit_0(self, tmp_path: Path):
        """建议级触顶只降级推进，不阻断——把它当阻断级会把正确的「存疑」推成「不符合」。"""
        crit = criteria(_asthma_condition())
        jdg = judgments({"EX-1-3": suspicious()})
        ocrs = [ocr(tmp_path, "| 用药 | 地塞米松 5mg 静滴 |\n")]
        hist = tmp_path / "hist.json"
        reports = [gate.recheck(crit, jdg, ocrs, history_path=hist) for _ in range(3)]
        assert reports[2]["uncertain_hits"] == ["EX-1-3"]
        assert reports[2].get("gate_escalated") is True
        assert reports[2].get("stuck_items") == [], "建议级不进 stuck_items（那是阻断级的失败上报清单）"
        assert "存疑" in reports[2].get("escalation_note", ""), "必须给出降级指令"
