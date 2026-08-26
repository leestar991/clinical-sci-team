"""render_parse_prompt 回归测试:解析委派 prompt 的机械渲染。

会话 881e7ba8:主代理手写 19 份委派(~19k 字符),轮次/闸 total/点名条目靠手填;
每个子代理被要求自读 34KB parsing-rules.md,EX 重做前 4 步 ~200k token 花在学规则,
写产物时上下文耗尽 → 17 条占位符。本脚本:模板逐字渲染 + 变量机械注入 + 规则节内嵌,
子代理零自读规则、主代理零手写模板。模板契约同判定域 render_judge_prompt
(会话 9a93ccc9:手抄模板丢闸命令 → 子代理自创 schema)。
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_DIR = REPO_ROOT / "skills" / "custom" / "criteria-parser"
SCRIPT_PATH = SKILL_DIR / "scripts" / "render_parse_prompt.py"
DELEGATION = SKILL_DIR / "references" / "parse-delegation.md"
PARSING_RULES = SKILL_DIR / "references" / "parsing-rules.md"

if not SCRIPT_PATH.exists():  # skills/custom 为 gitignore 目录，清空检出时跳过
    pytest.skip("criteria-parser 技能未安装", allow_module_level=True)


def _load_module():
    spec = importlib.util.spec_from_file_location("render_parse_prompt", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


rp = _load_module()


@pytest.fixture()
def ws(tmp_path: Path) -> Path:
    meta = {
        "raw段行号": {"入选": {"start": 276, "end": 350}, "排除": {"start": 350, "end": 468}},
        "末条号": {"入选": 11, "排除": 20},
    }
    (tmp_path / "criteria_meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return tmp_path


def _run(ws: Path, *extra: str) -> int:
    return rp.main(["--workspace", str(ws), *extra])


def _out(ws: Path, name: str) -> str:
    return (ws / "prompts" / name).read_text(encoding="utf-8")


# ────────────────────────── kind=parse(默认,两轨) ──────────────────────────


def test_parse_renders_both_tracks_with_variables(ws: Path):
    """两轨 prompt 生成;meta 的行号/末条号机械注入(手填这两个数字正是 881e7ba8 的漂移点)。"""
    assert _run(ws) == 0
    in_md = _out(ws, "parse_IN.md")
    ex_md = _out(ws, "parse_EX.md")
    assert "read_file(start_line=276, end_line=350)" in in_md
    assert "顶层编号 1..11 连续" in in_md
    assert "read_file(start_line=350, end_line=468)" in ex_md
    assert "顶层编号 1..20 连续" in ex_md
    assert "入选标准" in in_md and "排除标准" in ex_md


def test_rules_sections_embedded_not_linked(ws: Path):
    """parsing-rules 的关键节内嵌进 prompt,子代理不再自读 34KB 全文(881e7ba8:EX 重做
    前 4 步 ~200k token 学规则,写产物时上下文耗尽 → 占位符产物)。"""
    assert _run(ws) == 0
    for name in ("parse_IN.md", "parse_EX.md"):
        md = _out(ws, name)
        assert "## 拆分原则（最小子颗粒度）" in md, f"{name} 缺拆分原则节"
        assert "## 条件转化规则" in md, f"{name} 缺转化规则节"
        assert "## 可获取性判定标准" in md, f"{name} 缺可获取性节"
        assert "无需再读 parsing-rules.md" in md


def test_no_leftover_placeholders(ws: Path):
    """白名单占位符必须全部替换——残留会把字面 `{raw段行号.入选.start}` 送进子代理。"""
    assert _run(ws) == 0
    for name in ("parse_IN.md", "parse_EX.md"):
        md = _out(ws, name)
        for needle in ("{raw段行号", "{末条号", "{PARSING_RULES}", "{GATE_PROBLEMS}", "{TRACK}", "{轨名}"):
            assert needle not in md, f"{name} 残留占位符 {needle}"


def test_gate_command_fingerprint(ws: Path):
    """指纹:结构闸命令必须在渲染产物里(防模板被删段后静默渲染半个模板)。"""
    assert _run(ws) == 0
    for name in ("parse_IN.md", "parse_EX.md"):
        assert "check_track_structure.py" in _out(ws, name)


def test_json_example_survives_nested_fences(ws: Path):
    """模板内嵌 ```json 示例块:围栏提取必须按深度配对,把 json 示例完整带进产物。"""
    assert _run(ws) == 0
    md = _out(ws, "parse_IN.md")
    assert '"四分类"' in md and '"描述索引"' in md


# ────────────────────────── kind=redo(单轨重做) ──────────────────────────


def test_redo_embeds_gate_problems_and_no_rm_rule(ws: Path):
    """重做 prompt:结构闸点名注入 + 禁 rm 重建铁律(881e7ba8:rm 初版后重建降级版)。"""
    gate = {"exit_code": 2, "problems": ["闸9 ['EX-3'] 的 `原文` 非连续子串", "闸4 EX-9 混用"]}
    (ws / "criteria_structure_gate_EX.json").write_text(json.dumps(gate, ensure_ascii=False), encoding="utf-8")
    assert _run(ws, "--kind", "redo", "--track", "EX") == 0
    md = _out(ws, "redo_EX.md")
    assert "闸9 ['EX-3']" in md and "闸4 EX-9" in md
    assert "禁止 rm/重建" in md and "BashWritePolicy" in md
    assert "criteria_parsed_EX.json" in md and "{GATE_PROBLEMS}" not in md


def test_redo_requires_gate_artifact(ws: Path):
    """redo 没有结构闸产物(先跑闸再重做)→ exit 2,不渲染。"""
    assert _run(ws, "--kind", "redo", "--track", "EX") == 2
    assert not (ws / "prompts" / "redo_EX.md").exists()


# ────────────────────────── 输入校验 ──────────────────────────


def test_missing_meta_raw_lines_exits_2(ws: Path):
    meta = {"末条号": {"入选": 11, "排除": 20}}  # 缺 raw段行号
    (ws / "criteria_meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    assert _run(ws) == 2
    assert not (ws / "prompts").exists() or not list((ws / "prompts").glob("*.md"))


def test_unknown_track_exits_2(ws: Path):
    assert _run(ws, "--kind", "redo", "--track", "XX") == 2


def test_dispatch_lines_on_stdout(ws: Path, capsys):
    """stdout 给出 prompt_file 路径与 task(prompt_file=…) 派发行,主代理照抄即可。"""
    assert _run(ws) == 0
    out = capsys.readouterr().out
    assert "prompt_file" in out and "parse_IN.md" in out and "parse_EX.md" in out
