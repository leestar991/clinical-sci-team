"""extract_criteria 回归测试:章节原文机械落盘(提取区块 → 切片 → 组装 → 自检)。

会话 bc8a9bc7(2026-08-26):locate 已机械给出段行号,但「按行号切原文落盘
eligibility_criteria_raw.md」由主代理 write_file 逐字重写——11,441 输出 token、
215s(墙钟 40%),纯机械复制付了 LLM 生成价。本脚本接管该步;这些测试保证:
切片逐字(不编不漏)、块清单缺漏被 verify 闸拦住、失败不落盘、成功写回执。
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "skills" / "custom" / "criteria-parser" / "scripts" / "extract_criteria.py"

if not SCRIPT_PATH.exists():  # skills/custom 为 gitignore 目录，清空检出时跳过
    pytest.skip("criteria-parser 技能未安装", allow_module_level=True)


def _load_module():
    spec = importlib.util.spec_from_file_location("extract_criteria", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ec = _load_module()


SOURCE = """# 试验方案

##### 3研究设计
本研究为开放标签、多中心设计。

###### 4.1入选标准
受试者入组须满足以下所有条件：
1. 年龄≥18周岁。
2. ECOG 0-1分。
3. 预期生存≥3个月。

### 4.2排除标准
1. 存在活动性感染。
2. 妊娠期或哺乳期患者。
"""


def _source_lines() -> list[str]:
    return SOURCE.splitlines()


def _line_of(marker: str) -> int:
    for i, ln in enumerate(_source_lines(), start=1):
        if marker in ln:
            return i
    raise AssertionError(f"fixture 源里找不到 {marker!r}")


@pytest.fixture()
def ws(tmp_path: Path) -> Path:
    (tmp_path / "source.md").write_text(SOURCE, encoding="utf-8")
    return tmp_path


def _full_meta() -> dict:
    design = _line_of("3研究设计")
    incl = _line_of("4.1入选标准")
    ex = _line_of("4.2排除标准")
    total = len(_source_lines())
    return {
        "段行号": {"入选": {"start": incl, "end": ex - 1}, "排除": {"start": ex, "end": total}},
        "末条号": {"入选": 3, "排除": 2},
        "补充章节": [{"行号": design, "编号": "3", "标题": "研究设计"}],
        "方案元数据": {"方案编号": "T-101", "方案标题": "测试方案"},
        "提取区块": [
            {"标题": "研究设计与周期", "start": design, "end": incl - 2},
            {"标题": "入选标准与排除标准", "start": incl - 1, "end": total},
        ],
    }


def _write_meta(ws: Path, meta: dict) -> Path:
    p = ws / "criteria_meta.json"
    p.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def _run(ws: Path, meta: dict, *extra: str) -> int:
    meta_path = _write_meta(ws, meta)
    return ec.main(
        [
            "--meta", str(meta_path),
            "--source", str(ws / "source.md"),
            "--out", str(ws / "eligibility_criteria_raw.md"),
            *extra,
        ]
    )


# ────────────────────────── 正常路径 ──────────────────────────


def test_blocks_are_sliced_verbatim_from_source(ws: Path):
    """块内容必须逐字等于源文件行切片（splitlines 坐标，1-based 闭区间）。"""
    assert _run(ws, _full_meta()) == 0
    src_lines = _source_lines()
    raw = (ws / "eligibility_criteria_raw.md").read_text(encoding="utf-8")
    raw_lines = raw.splitlines()
    for block in _full_meta()["提取区块"]:
        assert f"# {block['标题']}" in raw_lines, f"块标题 {block['标题']} 缺失"
        sliced = src_lines[block["start"] - 1 : block["end"]]
        got = raw_lines[raw_lines.index(f"# {block['标题']}") + 1 : raw_lines.index(f"# {block['标题']}") + 1 + len(sliced)]
        assert got == sliced, f"块 {block['标题']} 内容与源切片不一致"


def test_metadata_section_rendered_first(ws: Path):
    """meta.方案元数据 → raw 头部「# 方案基本信息」节,键值都在。"""
    assert _run(ws, _full_meta()) == 0
    raw = (ws / "eligibility_criteria_raw.md").read_text(encoding="utf-8")
    assert raw.splitlines()[0] == "# 方案基本信息"
    assert "T-101" in raw and "测试方案" in raw
    assert raw.index("方案基本信息") < raw.index("# 研究设计与周期")


def test_receipt_fields_written_back_to_meta(ws: Path):
    """成功后 meta 写回 raw段行号 / raw总行数 / raw段行号归属文件(解析轨用这套坐标)。"""
    assert _run(ws, _full_meta()) == 0
    meta = json.loads((ws / "criteria_meta.json").read_text(encoding="utf-8"))
    assert meta["raw段行号"]["入选"]["start"] > 0
    assert meta["raw总行数"] == len((ws / "eligibility_criteria_raw.md").read_text(encoding="utf-8").splitlines())
    assert "eligibility_criteria_raw.md" in meta["raw段行号归属文件"]
    # 既有字段不被覆盖
    assert meta["末条号"] == {"入选": 3, "排除": 2}


# ────────────────────────── 输入校验 ──────────────────────────


def test_missing_blocks_list_exits_2(ws: Path):
    """meta 无「提取区块」→ exit 2。块边界是语义工作,由定位阶段写进 meta,本脚本不推导。"""
    meta = _full_meta()
    del meta["提取区块"]
    assert _run(ws, meta) == 2
    assert not (ws / "eligibility_criteria_raw.md").exists()


def test_out_of_range_end_exits_2(ws: Path):
    meta = _full_meta()
    meta["提取区块"][1]["end"] = len(_source_lines()) + 10
    assert _run(ws, meta) == 2


def test_start_after_end_exits_2(ws: Path):
    meta = _full_meta()
    meta["提取区块"][0]["start"], meta["提取区块"][0]["end"] = 5, 3
    assert _run(ws, meta) == 2


def test_overlapping_blocks_exits_2(ws: Path):
    """块区间重叠 → 同一段原文会被抄两遍,拒绝。"""
    meta = _full_meta()
    meta["提取区块"][0]["end"] = meta["提取区块"][1]["end"]
    assert _run(ws, meta) == 2


# ────────────────────────── verify 闸(复用 locate 的源基线核对) ──────────────────────────


def test_verify_gate_rejects_truncated_inclusion_block(ws: Path):
    """入选块切短(只含条目 1..2,源末条号 3)→ locate.verify_raw 报丢条 → exit 2。"""
    meta = _full_meta()
    meta["提取区块"][1]["end"] = _line_of("2. ECOG")  # 截掉第 3 条
    assert _run(ws, meta) == 2
    assert not (ws / "eligibility_criteria_raw.md").exists(), "校验失败不得落盘半成品"


def test_verify_gate_rejects_block_list_missing_supplement_section(ws: Path):
    """补充章节列了「研究设计」但提取区块没切它 → verify 报缺 → exit 2。"""
    meta = _full_meta()
    meta["提取区块"] = [meta["提取区块"][1]]  # 只留入选/排除块
    assert _run(ws, meta) == 2
