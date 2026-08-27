"""取证素材包**被交付**的契约（不是"被装配"的契约）。

会话 `c2518bc7`（2026-08-10）：主代理按硬前置把两轨取证包装配好了
—— IN 9,471 字符 / EX 19,538 字符，落盘成功 —— 然后 **6 个 QC 子代理一次都没读过它**。

机制不是模型不听话，是两份文本互相矛盾：

* `criteria-qc-checklist.md`「取证方式」写着「取证的默认入口是 `criteria_qc_bundle_{TRACK}.md`」；
* 同一文件的「QC 委派模板」的输入清单里**没有这个文件**，却写着
  「只读这两个文件…禁止 ls/glob 探索」。

子代理拿到的是模板渲染出来的白名单，于是取证包**在白名单之外 = 禁止读**。
退化路径：整篇读 `criteria_parsed_{TRACK}.json` → 撞 50k 字符上限被截断 → 按行窗分页补读
8~13 次（其间还要 `bash wc -l` 问文件多长）→ 第三轮再补 20 次内联 `python3 -c` print 字段。
单轨第三轮 `bash + read_file` 由 17 反弹到 **35**。

`criteria_qc_bundle.py` 的功能测试在 `test_criteria_qc_bundle.py`；本文件守的是**交接**：
脚本再好，只要模板不把它写进白名单，收益就是 0。
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
CHECKLIST = REPO / "skills" / "custom" / "criteria-parser" / "references" / "criteria-qc-checklist.md"
SKILL = REPO / "skills" / "custom" / "criteria-parser" / "SKILL.md"

if not CHECKLIST.exists() or not SKILL.exists():  # skills/custom 为 gitignore 目录
    pytest.skip("criteria-parser 技能未安装", allow_module_level=True)

BUNDLE_PATH = "/mnt/user-data/workspace/criteria_qc_bundle_{TRACK}.md"


def _delegation_template() -> str:
    """「QC 委派模板（按轨）」小节的正文 —— 也就是真正会被渲染进子代理 prompt 的那段。"""
    text = CHECKLIST.read_text(encoding="utf-8")
    start = text.index("## QC 委派模板")
    end = text.index("\n## ", start + 1)
    return text[start:end]


class TestDelegationTemplateHandsOverTheBundle:
    def test_template_names_the_bundle_path(self):
        assert BUNDLE_PATH in _delegation_template(), "模板未给出取证包路径 —— 子代理的白名单里就没有它，等于禁止读"

    def test_bundle_is_the_first_listed_input(self):
        """顺序有意义：清单第一项是子代理实际会先读的那个。"""
        template = _delegation_template()
        inputs_start = template.index("输入（")
        inputs = template[inputs_start:]
        bundle_at = inputs.index(BUNDLE_PATH)
        parsed_at = inputs.index("criteria_parsed_{TRACK}.json")
        assert bundle_at < parsed_at, "取证包必须排在 criteria_parsed 之前，否则子代理会先整篇读结构化产物"

    def test_missing_bundle_makes_the_subagent_refuse(self):
        """与结构闸自检同构：唯一在本次会话中被 6/6 遵守的规则就是那条自检。"""
        template = _delegation_template()
        assert "criteria_qc_bundle" in template
        assert "拒绝执行" in template, "取证包缺失时必须拒工，否则子代理会自己分页读来补偿"

    def test_template_forbids_whole_file_and_paged_reads_of_parsed_json(self):
        template = _delegation_template()
        assert "禁止整篇读" in template or "禁止整篇或分页通读" in template
        assert "分页" in template, "必须显式禁止按行窗分页通读 —— 这是本次会话 8~13 次重复读的形态"

    def test_template_forbids_inline_python_field_printing(self):
        assert "python3 -c" in _delegation_template(), "必须显式禁止内联 python3 -c print 字段（本次第三轮 20 次）"

    def test_skill_requires_handover_not_just_assembly(self):
        """SKILL.md 只写「不装配就不许派 QC」不够 —— 本次会话装配了，但没交出去。

        SKILL.md 受 13,500 字节的精简契约约束（`test_skill_slimming_contract.py`），
        所以这里只要求它带住「照抄模板的输入清单」这个指针，完整措辞在委派模板与故障档案里。
        """
        skill = SKILL.read_text(encoding="utf-8")
        assert "criteria_qc_bundle" in skill
        assert "输入清单" in skill, "SKILL.md 必须要求把取证包写进委派 prompt 的输入清单"
        assert "白名单" in skill, "必须说明漏写的后果，否则读者以为只是风格建议"
