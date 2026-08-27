"""改判子代理必须看得见对象级编辑工具（Phase 2 / Task 11）。

skill 规则把改判/修订的唯一允许写入工具改成了 `apply_json_patches` 的 pointer + op 形态。
规则指向一个 agent **看不见**的工具就是死锁：`judgment-repair.md` 明确允许把改判派给
`general-purpose` **或 `data-extractor`**，而后者的 `tools` 是显式白名单，不含该工具时子代理
只剩 `write_file` —— 恰好是规则严禁、且会静默丢条目的那条路（thread `5d987e97`）。

`quality-control` 是**故意**不给改写工具的：检查的人不能同时宣布改好了
（`judgment-repair.md` 委派模板第 3 条）。本文件把这条有意的不对称也锁住，免得后人"顺手补齐"。
"""

from __future__ import annotations

import importlib

import pytest

REPAIR_CAPABLE = ("data_extractor", "bash_agent")

_CONFIG_NAMES = {
    "data_extractor": "DATA_EXTRACTOR_CONFIG",
    "bash_agent": "BASH_AGENT_CONFIG",
    "quality_control": "QUALITY_CONTROL_CONFIG",
    "general_purpose": "GENERAL_PURPOSE_CONFIG",
}


def _agent_config(module_name: str):
    module = importlib.import_module(f"deerflow.subagents.builtins.{module_name}")
    return getattr(module, _CONFIG_NAMES[module_name])


@pytest.mark.parametrize("module_name", REPAIR_CAPABLE)
def test_repair_capable_agents_can_edit_json_objects(module_name: str):
    config = _agent_config(module_name)
    assert config.tools is not None, "该 agent 用显式白名单，None 会变成继承父工具"
    assert "apply_json_patches" in config.tools, f"{module_name} 看不到 apply_json_patches，改判规则会指向一个不存在的工具"


@pytest.mark.parametrize("module_name", REPAIR_CAPABLE)
def test_repair_capable_agents_keep_str_replace_for_broken_json(module_name: str):
    """语法坏掉的 JSON 无法 `json.loads`，对象级编辑会直接拒绝，只能靠 `str_replace` 就地修。"""
    config = _agent_config(module_name)
    assert "str_replace" in config.tools


def test_quality_control_still_has_no_write_or_repair_tool():
    """QC 不得能改它自己在检查的产物——这条不对称是有意的，不要"顺手补齐"。"""
    config = _agent_config("quality_control")
    assert "apply_json_patches" not in config.tools
    assert "str_replace" not in config.tools


def test_general_purpose_inherits_parent_tools():
    """`general-purpose` 用 `tools=None` 继承父工具，因此天然可见（无需改白名单）。"""
    config = _agent_config("general_purpose")
    assert config.tools is None
