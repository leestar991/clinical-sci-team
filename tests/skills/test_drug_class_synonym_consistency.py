"""类别→药名桥接词表的**三处一致性**。

标准写**类别**（"仍需全身性药物治疗"），病历写**药名**（"地塞米松"）。不桥接，下游就永远对不上：
会话 `1fee1395` 的 EX-1-3 因此写出一句事实错误的缺失断言 —— reason 称"无全身性糖皮质激素或
生物制剂处方"，而 `ocr_records.md:61` 明写 `2025.04.09起地塞米松及青霉素治疗`。

同一张表出现在三处，任一处漏一个药名就是一个盲区：

1. `criteria-parser/SKILL.md` 的「内置高频量表/指标同义词对照」—— 解析方填 `同义词` 的依据；
2. `eligibility-judgment/scripts/uncertain_recheck.py` 的 `BUILTIN_SCALE_SYNONYMS` —— 反查兜底；
3. `eligibility-judgment/scripts/check_reason_alignment.py` 的 `_CLASS_TO_DRUGS` ——
   `false_absence_claim` 证伪缺失断言。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
JUDGMENT_SCRIPTS = REPO / "skills" / "custom" / "eligibility-judgment" / "scripts"
PARSER_SKILL = REPO / "skills" / "custom" / "criteria-parser" / "SKILL.md"
PARSER_SYNONYMS = REPO / "skills" / "custom" / "criteria-parser" / "references" / "synonym-table.md"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, JUDGMENT_SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


recheck = _load("uncertain_recheck")
alignment = _load("check_reason_alignment")

# 每个类别的核心药名。少一个就意味着：病历写了这个药，而闸看不见。
CORE: dict[str, tuple[str, ...]] = {
    "糖皮质激素": ("地塞米松", "泼尼松", "甲泼尼龙", "氢化可的松", "倍他米松"),
    "紫杉": ("多西他赛", "紫杉醇", "卡巴他赛"),
    "内分泌治疗": ("阿比特龙", "恩扎卢胺", "氘恩扎如胺", "瑞维鲁胺"),
}


def _recheck_terms(key_substr: str) -> set[str]:
    out: set[str] = set()
    for key, syns in recheck.BUILTIN_SCALE_SYNONYMS.items():
        if key_substr in key:
            out.update(syns)
    return out


def _alignment_terms(key_substr: str) -> set[str]:
    out: set[str] = set()
    for klass, drugs in alignment._CLASS_TO_DRUGS.items():
        if key_substr in klass:
            out.update(drugs)
    return out


def test_uncertain_recheck_covers_core_drugs():
    for key, drugs in CORE.items():
        terms = _recheck_terms(key)
        assert terms, f"uncertain_recheck 缺类别 {key}"
        missing = [d for d in drugs if d not in terms]
        assert not missing, f"uncertain_recheck 的 {key} 缺 {missing}"


def test_reason_alignment_covers_core_drugs():
    for key, drugs in CORE.items():
        terms = _alignment_terms(key)
        assert terms, f"check_reason_alignment 缺类别 {key}"
        missing = [d for d in drugs if d not in terms]
        assert not missing, f"check_reason_alignment 的 {key} 缺 {missing}"


def test_parser_synonym_table_documents_core_drugs():
    table = PARSER_SYNONYMS.read_text(encoding="utf-8")
    for key, drugs in CORE.items():
        missing = [d for d in drugs if d not in table]
        assert not missing, f"criteria-parser/references/synonym-table.md 的 {key} 缺 {missing}"


def test_two_scripts_agree_on_each_core_class():
    """两个脚本对同一类别的药名集合必须互相覆盖核心项，避免一边报一边不报。"""
    for key in CORE:
        a, b = _recheck_terms(key), _alignment_terms(key)
        for drug in CORE[key]:
            assert drug in a and drug in b, f"{drug} 只出现在一处（recheck={drug in a}, alignment={drug in b}）"


def test_parser_skill_points_at_the_synonym_table():
    """指针留在 SKILL.md（主代理与人从这里找路），**理由**随规则走。

    2026-08-10：解析规则本体搬入 `references/parsing-rules.md`，「为什么必须填具体药名」属于
    填写规则的一部分，跟着规则走；SKILL.md 只保留指向。两处分别断言，避免搬家被误判成删除。
    """
    skill = PARSER_SKILL.read_text(encoding="utf-8")
    assert "references/synonym-table.md" in skill, "SKILL.md 丢了词表指针，填 `同义词` 时无从查表"
    assert "failure-archive.md" in skill, "须留故障出处，避免被当成冗余而删掉"


def test_parsing_rules_explain_why_concrete_drug_names_are_required():
    """「类别→具体药名」这条理由必须和填写规则在一起 —— 解析子代理只会读到规则文件。"""
    rules = PARSER_SKILL.parent / "references" / "parsing-rules.md"
    assert rules.exists(), "parsing-rules.md 不存在：解析规则无处可读"
    body = rules.read_text(encoding="utf-8")
    assert "类别→具体药名" in body or "类别→药名" in body, "规则文件里缺「为什么必须填具体药名」"
    assert "synonym-table.md" in body, "规则文件里也要能指到词表"


def test_synonym_table_states_the_three_way_consistency_rule():
    table = PARSER_SYNONYMS.read_text(encoding="utf-8")
    assert "三处必须一致" in table
    assert "uncertain_recheck.py" in table and "check_reason_alignment.py" in table
