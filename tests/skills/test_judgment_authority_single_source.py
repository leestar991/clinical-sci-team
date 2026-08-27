"""「同一条件的结论口径只能有一个出处」的回归测试。

会话 `1fee1395` 的 EX-1-3（`有特异性变态反应病史（哮喘、风湿、湿疹性皮炎）且仍需全身性药物治疗`）
在技能里有**四处**说法，其中三处互相矛盾：

| 出处 | 当时的说法 |
|---|---|
| `references/schema_example.json` | `conclusion="存疑"`，reason 写"未见该哮喘当前仍需全身性药物治疗的记录" |
| `SKILL.md` 原则十一 B 末段 | "依据应取研究者自己的结论……据此判 `不符合` 是成立的" |
| `references/qc-delegation.md` | "正确依据是研究者写的'哮喘，临床试验筛选失败'" |
| `references/judge-delegation.md` | "真正触发该条款的是地塞米松……属于全身性药物治疗范畴" |

后两者都会进任务 prompt。判定子代理照抄了 `schema_example.json`（正确的那个），但同一个
prompt 里塞着两条相反指令，输出正确纯属侥幸。

**本轮裁定的三步判据**（唯一权威 = `references/judgment-principles.md` §原则十一 B；
2026-08-10 前该权威在 `SKILL.md`，判定规则整体搬入 references 后随之迁移）：
① 病历有该变态反应病史 → 只满足前半句；
② 必须找到**针对该病史**的治疗记录，再对该药做归类查证 —— 全身性 → `不符合`（触发）；
   局部/外用/吸入 → `符合`（未触发）；
③ 有病史但无任何针对该病史的治疗记录 → `存疑`。

据此：`地塞米松（2025.04.09起，用于尿频尿急/放疗期症状）`落不到 ②，
研究者的"临床试验筛选失败"是结论而非治疗记录，也落不到 ② —— 该会话判 `存疑` **正确**。
所以要改的是规则权威文件末段、`qc-delegation.md`、`judge-delegation.md` 三处，
`schema_example.json` 是唯一正确锚点，本测试把它锁死。
"""

from __future__ import annotations

import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
JUDGMENT = REPO / "skills" / "custom" / "eligibility-judgment"
SKILL = JUDGMENT / "SKILL.md"
# 判定规则本体的住址。2026-08-10 起原则十一 B 在这里，不在 SKILL.md（SKILL.md 只留编排 + 指向）。
PRINCIPLES = JUDGMENT / "references" / "judgment-principles.md"
JUDGE_DELEGATION = JUDGMENT / "references" / "judge-delegation.md"
QC_DELEGATION = JUDGMENT / "references" / "qc-delegation.md"
SCHEMA_EXAMPLE = JUDGMENT / "references" / "schema_example.json"

# 「研究者写了筛选失败 ⇒ 排除被触发」这条被本轮裁定推翻的推理，任何文件都不得再出现。
_INVESTIGATOR_CONCLUSION_TRIGGER = "临床试验筛选失败"
# 「地塞米松触发 EX-1-3」这条被本轮裁定推翻的推理。
_DEXAMETHASONE = "地塞米松"


def _text(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ── schema_example.json 是唯一正确锚点，锁死 ────────────────────────


def _ex_1_3_examples() -> list[dict]:
    data = json.loads(_text(SCHEMA_EXAMPLE))
    found: list[dict] = []

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "EX-1-3" and isinstance(v, dict):
                    found.append(v)
                walk(v)
        elif isinstance(node, list):
            for x in node:
                walk(x)

    walk(data)
    return found


def test_schema_example_keeps_ex_1_3_as_uncertain():
    examples = _ex_1_3_examples()
    assert examples, "schema_example.json 必须保留 EX-1-3 示例——它是子代理最常照抄的对象"
    for ex in examples:
        assert ex.get("conclusion") == "存疑", f"EX-1-3 示例结论必须是 `存疑`：有哮喘病史，但病历无任何**针对哮喘**的治疗记录。实际 {ex.get('conclusion')!r}"


def test_schema_example_demonstrates_rejecting_an_off_target_systemic_drug():
    """光有「氘恩扎如胺是抗肿瘤药」不够——还要示范拒绝一个**确实是全身性激素**的药。

    地塞米松是本会话的真实数据：它确实属全身性皮质激素（归类通过），但用于尿频尿急，
    不是哮喘治疗，所以照样落不到 ②。这个反例比抗肿瘤药更能说明「归类 ≠ 触发」。
    """
    examples = _ex_1_3_examples()
    blob = json.dumps(examples, ensure_ascii=False)
    assert _DEXAMETHASONE in blob, "须补入地塞米松反例"
    assert "尿" in blob, "须写明它治的是什么（尿频尿急），否则读者看不出为何不算"


def test_schema_example_ex_1_3_evidence_has_page_and_source():
    """结论示例同时是 evidence 形态示例，不能少 source/page。"""
    for ex in _ex_1_3_examples():
        record_evidence = [e for e in ex.get("evidence", []) if e.get("source") != "external"]
        assert record_evidence, "须有病历侧 evidence"
        for e in record_evidence:
            assert e.get("source") and e.get("page")


# ── SKILL.md 是唯一结论口径出处，且必须已订正 ───────────────────────


def test_skill_states_the_three_step_test():
    skill = _text(PRINCIPLES)
    for marker in ("①", "②", "③"):
        assert marker in skill, "原则十一 B 须写成三步判据"
    assert "针对" in skill and "病史" in skill


def test_skill_no_longer_treats_the_investigator_conclusion_as_a_trigger():
    """被推翻的旧裁定原句（逐字）：

        依据应取病历里研究者自己的结论——"哮喘，**临床试验筛选失败**"：研究者已
        认定该哮喘构成排除。据此判 `不符合`（=排除被触发）是成立的

    这三句都不得再出现，否则读者会退回旧裁定。
    """
    skill = _text(SKILL)
    for gone in (
        "依据应取病历里研究者自己的结论",
        "研究者已\n认定该哮喘构成排除",
        "据此判 `不符合`（=排除被触发）是成立的",
    ):
        assert gone not in skill, f"旧裁定残留：{gone!r}"


def test_skill_explicitly_says_a_conclusion_is_not_a_treatment_record():
    skill = _text(PRINCIPLES)
    assert "不是治疗记录" in skill or "≠ 治疗记录" in skill, "必须显式写明「筛选失败/不适合入组」这类研究者结论不能当作 ② 的治疗记录，否则读者会退回旧裁定"


def test_skill_keeps_the_100_percent_counterargument():
    """「若在用任何全身性药物都算，这条会排掉 100% 的肿瘤候选者」——这段反证是规则的地基。"""
    assert "100%" in _text(PRINCIPLES)


# ── delegation 只引用，不复述结论 ────────────────────────────────────


def test_judge_delegation_does_not_assert_a_conclusion_for_ex_1_3():
    doc = _text(JUDGE_DELEGATION)
    assert _DEXAMETHASONE not in doc, "judge-delegation 曾断言「真正触发该条款的是地塞米松」——它只做了归类、跳过了②的针对性判断。委派模板不得自带结论。"


def test_qc_delegation_does_not_assert_a_conclusion_for_ex_1_3():
    doc = _text(QC_DELEGATION)
    assert "正确依据是研究者写的" not in doc
    assert _INVESTIGATOR_CONCLUSION_TRIGGER not in doc or "不是治疗记录" in doc, "qc-delegation 若还提「临床试验筛选失败」，必须同时说明它**不是**治疗记录"


def test_delegations_point_at_the_single_authority():
    for p in (JUDGE_DELEGATION, QC_DELEGATION):
        doc = _text(p)
        assert "原则十一" in doc, f"{p.name} 须指向 judgment-principles.md §原则十一，而不是自带一套说法"


def test_only_skill_and_schema_example_mention_a_conclusion_for_ex_1_3():
    """机械口径：结论词（`不符合`/`存疑`）+ EX-1-3 只允许出现在规则权威文件与 schema_example。"""
    for p in (JUDGE_DELEGATION, QC_DELEGATION):
        doc = _text(p)
        for line in doc.splitlines():
            if "EX-1-3" in line:
                assert not any(w in line for w in ("判 `不符合`", "判 `存疑`", 'conclusion"')), f"{p.name} 不得在提到 EX-1-3 的同一行断言结论：{line.strip()[:120]}"
