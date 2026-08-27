"""``skills=[]`` 的工具策略后果(criteria-token-saving-v1.2 Task 2 Step 1b)。

Task 2 的收益有两块,第二块是**副作用式修复**,容易被忽略,所以单独锁定:

1. **token**:子代理不再继承五个技能全文。基线 42,758 o200k token × 379 个 AI 轮次
   ≈ 16.2M 固定重复 token,占 34.4M 总量的 47%。
2. **OCR 根因**:技能的 `allowed-tools` 声明会**并集过滤**子代理工具集
   (`filter_tools_by_skill_allowed_tools`)。eligibility 场景下 4 个技能的并集里没有
   `parse_document`,于是子代理拿不到该工具 —— 这正是 MEMORY 记录的「OCR 子代理无法解析文档」
   的根因。`skills=[]` 后没有任何技能声明 `allowed-tools`,`allowed_tool_names_for_skills`
   返回 `None`(= 不过滤),`parse_document` **自动回归**。

本文件用最小 Skill/Tool 替身锁定该行为,并覆盖 `_merge_skill_allowlists` 的覆盖陷阱:
lead 若在 metadata 里写了非 None 的 `available_skills`,子代理的 `[]` 会被 lead 的 allowlist
**覆盖**(`task_tool.py` `_merge_skill_allowlists`:`parent is None` 才返回 child),
于是收窄失效、五技能重新灌回。eligibility 的 lead 必须保持 `available_skills=None`。
"""

from __future__ import annotations

from dataclasses import dataclass

from deerflow.skills.tool_policy import allowed_tool_names_for_skills, filter_tools_by_skill_allowed_tools
from deerflow.tools.builtins.task_tool import _merge_skill_allowlists


@dataclass
class FakeSkill:
    name: str
    allowed_tools: list[str] | None = None


@dataclass
class FakeTool:
    name: str


ELIGIBILITY_TOOLS = [
    FakeTool("read_file"),
    FakeTool("write_file"),
    FakeTool("str_replace"),
    FakeTool("bash"),
    FakeTool("parse_document"),
    FakeTool("parse_image_batch"),
    FakeTool("view_image"),
]


def _names(tools) -> set[str]:
    return {t.name for t in tools}


# --------------------------------------------------------------------------- #
# skills=[] → 不过滤 → parse_document 回归                                     #
# --------------------------------------------------------------------------- #


def test_no_skills_means_no_allowlist():
    """`skills=[]` 时 executor 传给 tool_policy 的是空列表 → 返回 None(不过滤)。"""
    assert allowed_tool_names_for_skills([]) is None


def test_no_skills_keeps_parse_document_in_toolset():
    kept = filter_tools_by_skill_allowed_tools(ELIGIBILITY_TOOLS, [])
    assert _names(kept) == _names(ELIGIBILITY_TOOLS)
    assert "parse_document" in _names(kept)
    assert "parse_image_batch" in _names(kept)


def test_skill_allowlist_union_can_drop_parse_document():
    """回归现场:这是 skills=[] 之前的实际状态,证明过滤确实会吃掉 parse_document。"""
    skills = [
        FakeSkill("criteria-parser", ["read_file", "write_file", "bash"]),
        FakeSkill("eligibility-judgment", ["read_file", "str_replace", "bash"]),
        FakeSkill("pdf-image-extractor", ["read_file", "write_file", "view_image"]),
        FakeSkill("patient-separator", ["read_file", "bash"]),
    ]
    kept = _names(filter_tools_by_skill_allowed_tools(ELIGIBILITY_TOOLS, skills))
    assert "parse_document" not in kept, "四技能并集不含 parse_document —— 这就是 OCR 根因"
    assert "parse_image_batch" not in kept
    assert "read_file" in kept


def test_single_skill_whitelist_still_filters():
    """收窄到 1 个技能仍会过滤:若该技能声明了 allowed-tools,需确认它包含所需工具。"""
    skills = [FakeSkill("criteria-parser", ["read_file", "write_file"])]
    kept = _names(filter_tools_by_skill_allowed_tools(ELIGIBILITY_TOOLS, skills))
    assert kept == {"read_file", "write_file"}
    assert "parse_document" not in kept


def test_skill_without_allowed_tools_declaration_does_not_filter():
    """遗留技能(未声明 allowed-tools)不触发过滤 —— 与 skills=[] 同效。"""
    skills = [FakeSkill("legacy-skill", None)]
    assert allowed_tool_names_for_skills(skills) is None
    assert _names(filter_tools_by_skill_allowed_tools(ELIGIBILITY_TOOLS, skills)) == _names(ELIGIBILITY_TOOLS)


def test_one_explicit_declaration_makes_legacy_skills_contribute_nothing():
    """混合场景:一旦有技能显式声明,未声明的技能不再"解锁全部"。"""
    skills = [FakeSkill("declares", ["read_file"]), FakeSkill("legacy", None)]
    assert allowed_tool_names_for_skills(skills) == {"read_file"}


# --------------------------------------------------------------------------- #
# lead allowlist 覆盖陷阱                                                      #
# --------------------------------------------------------------------------- #


def test_lead_none_lets_subagent_empty_list_win():
    """eligibility 要求的配置:lead 不限定 → 子代理的 [] 生效。"""
    assert _merge_skill_allowlists(None, []) == []


def test_lead_none_lets_subagent_none_stay_none():
    assert _merge_skill_allowlists(None, None) is None


def test_lead_allowlist_overrides_subagent_none():
    """子代理未限定时继承的是 lead 的 allowlist 副本,不是"全部技能"。"""
    merged = _merge_skill_allowlists(["criteria-parser", "eligibility-judgment"], None)
    assert merged == ["criteria-parser", "eligibility-judgment"]


def test_lead_allowlist_does_not_resurrect_skills_for_empty_child():
    """交集语义:child=[] 与任何 parent 求交仍是 [] —— 收窄不会被 lead 破坏。"""
    assert _merge_skill_allowlists(["criteria-parser"], []) == []


def test_intersection_drops_skills_lead_did_not_allow():
    merged = _merge_skill_allowlists(["criteria-parser"], ["criteria-parser", "eligibility-judgment"])
    assert merged == ["criteria-parser"]
