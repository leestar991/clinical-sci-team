"""`locate_criteria_sections.py` 的回归测试。

核心防线：thread `ec24d087` —— PDF 转出的方案 .md 每页一个 `\f`，`grep -n` 与
`read_file`（内部用 `str.splitlines()`）的行号错位，导致提取只拿到排除 1..11
（真实 20 条），而自检脚本在同一错误坐标系里数源末条号也得 11，`n == N` 空过。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "skills" / "custom" / "criteria-parser" / "scripts" / "locate_criteria_sections.py"


def _load():
    spec = importlib.util.spec_from_file_location("locate_criteria_sections", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mod = _load()


# ── 构造夹具 ────────────────────────────────────────────────────────


def build_protocol(
    in_items: int = 11,
    ex_items: int = 20,
    *,
    form_feed_every: int = 0,
    with_toc: bool = True,
    preamble: int = 0,
    supplements: tuple[str, ...] = ("3.6 研究周期",),
    terminator: str = "5 药物与治疗",
) -> str:
    """生成一份最小可用的试验方案 .md。

    form_feed_every>0 时每隔若干行插入一个 `\f`（模拟 PDF 分页），用来制造
    `grep -n` 与 `splitlines()` 的坐标系偏移。
    """
    lines: list[str] = []
    # 真实方案正文在数千行处，而 raw.md 仅数百行 —— 用填充还原这个量级差，
    # 否则「段行号越出 raw.md」这一故障条件无法复现。
    lines += [f"前言段落第 {i} 句，与入排标准无关。" for i in range(1, preamble + 1)]
    if with_toc:
        lines += [
            "目录",
            "4.1  入选标准 ............................................ 52",
            "4.2  排除标准 ............................................ 54",
            f"{terminator} ............................................ 58",
            "",
        ]
    for sup in supplements:
        lines += [sup, "所有受试者将接受筛选期、治疗期与随访期。", ""]
    lines += ["4.1 入选标准", ""]
    for i in range(1, in_items + 1):
        lines += [f"{i}.  入选条件第 {i} 条正文。", ""]
    lines += ["4.2 排除标准", ""]
    for i in range(1, ex_items + 1):
        lines += [f"{i}.  排除条件第 {i} 条正文。", ""]
    lines += [terminator, "", "试验药：示例片", ""]

    if form_feed_every:
        out: list[str] = []
        for n, ln in enumerate(lines, 1):
            out.append(ln)
            if n % form_feed_every == 0:
                out.append("\x0c方案编号：XS-03-II201  版本号/版本日期：V1.2")
        lines = out
    return "\n".join(lines) + "\n"


def build_raw(in_items: int = 11, ex_items: int = 20, *, supplements: tuple[str, ...] = ("3.6 研究周期",)) -> str:
    lines = [
        "# 方案基本信息",
        "- 方案编号：XS-03-II201",
        "- 来源：试验方案.pdf 第4章（4.1 入选标准 / 4.2 排除标准）",
        "",
        "# 4.1 入选标准",
        "",
    ]
    for i in range(1, in_items + 1):
        lines += [f"{i}.  入选条件第 {i} 条正文。", ""]
    lines += ["# 4.2 排除标准", ""]
    for i in range(1, ex_items + 1):
        lines += [f"{i}.  排除条件第 {i} 条正文。", ""]
    for sup in supplements:
        lines += [f"# {sup}", "所有受试者将接受筛选期、治疗期与随访期。", ""]
    return "\n".join(lines) + "\n"


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    (tmp_path / "workspace").mkdir()
    (tmp_path / "uploads").mkdir()
    return tmp_path


def write_protocol(ws: Path, text: str) -> Path:
    p = ws / "uploads" / "试验方案.md"
    p.write_text(text, encoding="utf-8")
    return p


def write_raw(ws: Path, text: str) -> Path:
    p = ws / "workspace" / "eligibility_criteria_raw.md"
    p.write_text(text, encoding="utf-8")
    return p


def run(argv: list[str]) -> int:
    return mod.main(argv)


# ── last_contiguous ────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("seq", "expected"),
    [
        ([1, 2, 3], 3),
        ([], 0),
        ([2, 3], 0),  # 缺第 1 条 → 0
        ([1, 2, 3, 3, 4], 4),  # 跨页重复编号
        ([1, 2, 9], 2),  # 尾部杂散
        ([1, 2, 3, 5, 6], 3),  # 中间缺号
        (list(range(1, 21)), 20),
    ],
)
def test_last_contiguous(seq, expected):
    assert mod.last_contiguous(seq) == expected


# ── 坐标系：这是 ec24d087 的根因 ────────────────────────────────────


def test_no_form_feed_two_coordinate_systems_agree(ws):
    p = write_protocol(ws, build_protocol(form_feed_every=0))
    loc = mod.locate(p)
    assert loc["断行符扫描"]["坐标系偏移"] == 0
    assert loc["末条号"] == {"入选": 11, "排除": 20}


def test_form_feed_shifts_coordinates_and_is_reported(ws):
    """有 `\f` 时必须报告偏移量，且段行号仍在 splitlines 坐标系里正确。"""
    p = write_protocol(ws, build_protocol(form_feed_every=7))
    loc = mod.locate(p)
    br = loc["断行符扫描"]
    assert br["异常断行符"], "必须检出 \\f"
    assert "\\f 换页" in next(iter(br["异常断行符"]))
    assert br["坐标系偏移"] > 0, "splitlines 行数必须多于 split('\\n')"
    # 关键：末条号仍是真值，没被坐标系错位污染
    assert loc["末条号"] == {"入选": 11, "排除": 20}


def test_section_lines_feed_read_file_correctly(ws):
    """段行号必须能直接喂 read_file（sandbox/tools.py 用 splitlines）。"""
    text = build_protocol(form_feed_every=5)
    p = write_protocol(ws, text)
    loc = mod.locate(p)
    lines = text.splitlines()  # 与 read_file 实现一致

    for track, expect_max in (("入选", 11), ("排除", 20)):
        seg = loc["段行号"][track]
        got = "\n".join(lines[seg["start"] - 1 : seg["end"] - 1])
        nums = mod.item_numbers(got.split("\n"))
        assert mod.last_contiguous(nums) == expect_max, f"{track} 段行号取到的内容不完整"


def test_grep_style_line_numbers_would_be_wrong(ws):
    """反证：用 split('\\n') 坐标（等价 grep -n）去切，结果必然不完整。"""
    text = build_protocol(form_feed_every=5)
    p = write_protocol(ws, text)
    loc = mod.locate(p)
    seg = loc["排除"] if "排除" in loc else loc["段行号"]["排除"]
    n_lines = text.split("\n")  # grep -n 坐标系
    got = "\n".join(n_lines[seg["start"] - 1 : seg["end"] - 1])
    nums = mod.item_numbers(got.split("\n"))
    assert mod.last_contiguous(nums) != 20, "若两坐标系等价则本测试无意义"


def test_scan_breaks_detects_each_exotic_char():
    for ch in ("\x0b", "\x0c", "\x1c", "\x85", "\u2028", "\u2029"):
        res = mod.scan_breaks(f"a{ch}b\nc\n")
        assert res["异常断行符"], f"{ch!r} 未被检出"
        assert res["坐标系偏移"] >= 1


# ── 章节定位 ────────────────────────────────────────────────────────


def test_toc_lines_are_skipped(ws):
    p = write_protocol(ws, build_protocol(with_toc=True))
    loc = mod.locate(p)
    # 目录里的 4.1 在文件极前部；正文的应在补充章节之后
    assert loc["段行号"]["入选"]["start"] > 5


def test_numbered_item_not_mistaken_for_heading(ws):
    """`5.  排除条件…` 是条目，不能被当成 `5 药物与治疗` 那种章节标题而提前截断。"""
    p = write_protocol(ws, build_protocol(ex_items=20))
    loc = mod.locate(p)
    assert loc["末条号"]["排除"] == 20
    assert loc["章节标题"]["终点"].startswith("5 药物与治疗")


def test_terminator_missing_falls_back_to_eof(ws):
    text = build_protocol(terminator="附录A 参考文献")
    p = write_protocol(ws, text)
    loc = mod.locate(p)
    assert loc["末条号"]["排除"] == 20


def test_supplement_sections_are_collected(ws):
    p = write_protocol(ws, build_protocol(supplements=("3.6 研究周期", "6.2 筛选期")))
    loc = mod.locate(p)
    titles = [s["标题"] for s in loc["补充章节"]]
    assert "研究周期" in titles
    assert "筛选期" in titles


def test_missing_section_titles_blocked(ws):
    p = write_protocol(ws, "1. 无关内容\n\n2 章节\n")
    with pytest.raises(mod.LocateBlocked, match="未能在.*定位章节标题"):
        mod.locate(p)


def test_exclusion_before_inclusion_blocked(ws):
    p = write_protocol(ws, "4.2 排除标准\n\n1.  a\n\n4.1 入选标准\n\n1.  b\n")
    with pytest.raises(mod.LocateBlocked, match="不在入选标准"):
        mod.locate(p)


# ── --verify-raw 自检 ───────────────────────────────────────────────


def test_verify_raw_passes_on_complete_extraction(ws):
    write_protocol(ws, build_protocol())
    write_raw(ws, build_raw())
    assert (
        run(
            [
                "--protocol",
                str(ws / "uploads" / "试验方案.md"),
                "--workspace",
                str(ws / "workspace"),
                "--verify-raw",
                str(ws / "workspace" / "eligibility_criteria_raw.md"),
            ]
        )
        == 0
    )


def test_verify_raw_catches_truncated_exclusions(ws):
    """ec24d087 的原始症状：排除只提取到 11 条，真实 20 条。"""
    p = write_protocol(ws, build_protocol(ex_items=20, form_feed_every=6))
    raw = write_raw(ws, build_raw(ex_items=11))
    loc = mod.locate(p)
    problems = mod.verify_raw(loc, raw)
    assert any("排除标准丢条" in x and "1..20" in x and "只到 1..11" in x for x in problems), problems
    assert any("[12, 13, 14, 15, 16, 17, 18, 19, 20]" in x for x in problems), problems


def test_verify_raw_catches_truncated_inclusions(ws):
    p = write_protocol(ws, build_protocol(in_items=11))
    raw = write_raw(ws, build_raw(in_items=8))
    problems = mod.verify_raw(mod.locate(p), raw)
    assert any("入选标准丢条" in x for x in problems), problems


def test_verify_raw_catches_missing_supplement_section(ws):
    """用户报告的第二个症状：研究周期等章节没取到。"""
    p = write_protocol(ws, build_protocol(supplements=("3.6 研究周期",)))
    raw = write_raw(ws, build_raw(supplements=()))
    problems = mod.verify_raw(mod.locate(p), raw)
    assert any("补充章节" in x and "研究周期" in x for x in problems), problems


def test_verify_raw_reports_gap_in_middle(ws):
    p = write_protocol(ws, build_protocol(ex_items=5))
    raw_text = build_raw(ex_items=5).replace("3.  排除条件第 3 条正文。", "")
    raw = write_raw(ws, raw_text)
    problems = mod.verify_raw(mod.locate(p), raw)
    assert any("排除标准丢条" in x for x in problems), problems


def test_verify_raw_missing_file(ws):
    p = write_protocol(ws, build_protocol())
    problems = mod.verify_raw(mod.locate(p), ws / "workspace" / "nope.md")
    assert any("提取结果不存在" in x for x in problems)


def test_verify_raw_empty_file(ws):
    p = write_protocol(ws, build_protocol())
    raw = write_raw(ws, "   \n\n")
    problems = mod.verify_raw(mod.locate(p), raw)
    assert any("为空文件" in x for x in problems)


def test_verify_raw_without_titles_blocked(ws):
    p = write_protocol(ws, build_protocol())
    raw = write_raw(ws, "# 方案基本信息\n只有基本信息，没有标准章节\n")
    with pytest.raises(mod.LocateBlocked, match="找不到"):
        mod.verify_raw(mod.locate(p), raw)


def test_prose_mention_of_both_titles_not_treated_as_heading(ws):
    """`- 来源：…（4.1 入选标准 / 4.2 排除标准）` 不能被当成章节标题。"""
    p = write_protocol(ws, build_protocol())
    raw = write_raw(ws, build_raw())
    # build_raw 第 3 行就是这种正文提及；能正常分段说明已被排除
    assert mod.verify_raw(mod.locate(p), raw) == []


# ── CLI / 落盘 ──────────────────────────────────────────────────────


def test_cli_writes_meta_and_preserves_existing_fields(ws):
    write_protocol(ws, build_protocol())
    meta = ws / "workspace" / "criteria_meta.json"
    meta.write_text(
        json.dumps({"方案元数据": {"方案编号": "XS-03-II201"}}, ensure_ascii=False),
        encoding="utf-8",
    )
    assert run(["--protocol", str(ws / "uploads" / "试验方案.md"), "--workspace", str(ws / "workspace")]) == 0
    d = json.loads(meta.read_text(encoding="utf-8"))
    assert d["方案元数据"]["方案编号"] == "XS-03-II201", "既有字段不得被覆盖"
    assert d["末条号"] == {"入选": 11, "排除": 20}
    assert d["段行号"]["排除"]["end"] > d["段行号"]["排除"]["start"]
    assert "splitlines" in d["行号坐标系"]


def test_cli_tolerates_corrupt_existing_meta(ws):
    write_protocol(ws, build_protocol())
    meta = ws / "workspace" / "criteria_meta.json"
    meta.write_text("{ not json", encoding="utf-8")
    assert run(["--protocol", str(ws / "uploads" / "试验方案.md"), "--workspace", str(ws / "workspace")]) == 0
    assert json.loads(meta.read_text(encoding="utf-8"))["末条号"]["排除"] == 20


def test_cli_exit_2_on_incomplete_extraction(ws):
    write_protocol(ws, build_protocol(ex_items=20))
    write_raw(ws, build_raw(ex_items=11))
    assert (
        run(
            [
                "--protocol",
                str(ws / "uploads" / "试验方案.md"),
                "--workspace",
                str(ws / "workspace"),
                "--verify-raw",
                str(ws / "workspace" / "eligibility_criteria_raw.md"),
            ]
        )
        == 2
    )


def test_cli_exit_2_on_missing_protocol(ws):
    assert run(["--protocol", str(ws / "uploads" / "缺失.md")]) == 2


def test_cli_json_output(ws, capsys):
    write_protocol(ws, build_protocol(form_feed_every=6))
    write_raw(ws, build_raw(ex_items=11))
    rc = run(
        [
            "--protocol",
            str(ws / "uploads" / "试验方案.md"),
            "--verify-raw",
            str(ws / "workspace" / "eligibility_criteria_raw.md"),
            "--json",
        ]
    )
    assert rc == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["末条号"]["排除"] == 20
    assert payload["断行符扫描"]["坐标系偏移"] > 0
    assert any("排除标准丢条" in p for p in payload["problems"])


def test_summarize_warns_about_exotic_breaks(ws):
    p = write_protocol(ws, build_protocol(form_feed_every=6))
    loc = mod.locate(p)
    text = mod.summarize(loc, [], None)
    assert "异常断行符" in text
    assert "grep -n" in text and "read_file" in text
    assert "✅ 自检通过" in text


# ── raw段行号：thread `6e5ac7c1` 的坐标混用陷阱 ─────────────────────
#
# `段行号` 属 uploads/试验方案.md（数千行），`raw段行号` 属 raw.md（数百行）。
# 用前者 read_file 后者 → 越界切片 → read_file 静默返回空串（不报错、也不是
# `(empty)`，因为 `if not content` 的兜底在切片之前）→ 子代理凭空编造 92% 条目。


def test_raw_section_lines_point_into_raw_md(ws):
    p = write_protocol(ws, build_protocol(form_feed_every=6))
    raw = write_raw(ws, build_raw())
    loc = mod.locate(p)
    mod.verify_raw(loc, raw)
    r = loc["raw段行号"]
    total = loc["raw总行数"]
    for track in ("入选", "排除"):
        assert 1 <= r[track]["start"] < r[track]["end"] <= total + 1, f"{track} 越出 raw.md 范围"


def test_raw_section_lines_feed_read_file_on_raw_md(ws):
    """按 raw段行号 切 raw.md 必须取到完整条目；按 段行号 切必然为空。"""
    p = write_protocol(ws, build_protocol(form_feed_every=6, in_items=11, ex_items=20, preamble=400))
    raw_text = build_raw(in_items=11, ex_items=20)
    raw = write_raw(ws, raw_text)
    loc = mod.locate(p)
    mod.verify_raw(loc, raw)
    lines = raw_text.splitlines()  # 与 read_file 实现一致

    for track, expect in (("入选", 11), ("排除", 20)):
        seg = loc["raw段行号"][track]
        got = "\n".join(lines[seg["start"] - 1 : seg["end"] - 1])
        assert got.strip(), f"{track} 用 raw段行号 读到空内容"
        assert mod.last_contiguous(mod.item_numbers(got.split("\n"))) == expect

        # 反证：用 段行号（试验方案.md 坐标）切 raw.md → 空串
        bad = loc["段行号"][track]
        assert bad["start"] > len(lines), "夹具需保证 段行号 确实越出 raw.md"
        assert "\n".join(lines[bad["start"] - 1 : bad["end"] - 1]) == "", "越界切片应为空串"


def test_meta_records_both_coordinate_systems(ws):
    write_protocol(ws, build_protocol())
    write_raw(ws, build_raw())
    rc = run(
        [
            "--protocol",
            str(ws / "uploads" / "试验方案.md"),
            "--workspace",
            str(ws / "workspace"),
            "--verify-raw",
            str(ws / "workspace" / "eligibility_criteria_raw.md"),
        ]
    )
    assert rc == 0
    d = json.loads((ws / "workspace" / "criteria_meta.json").read_text(encoding="utf-8"))
    assert "试验方案.md" in d["段行号归属文件"]
    assert "eligibility_criteria_raw.md" in d["raw段行号归属文件"]
    assert d["raw段行号"]["入选"]["start"] < d["raw段行号"]["排除"]["start"]
    assert d["raw总行数"] > 0
    # 两套坐标必须显著不同，否则混用不会暴露
    assert d["段行号"]["入选"]["start"] != d["raw段行号"]["入选"]["start"]


def test_protocol_far_larger_than_raw_is_the_realistic_shape(ws):
    """真实形态：方案数千行、raw.md 数百行 —— 段行号必然越出 raw.md。"""
    p = write_protocol(ws, build_protocol(preamble=400))
    raw = write_raw(ws, build_raw())
    loc = mod.locate(p)
    mod.verify_raw(loc, raw)
    assert loc["段行号"]["入选"]["start"] > loc["raw总行数"]
    assert loc["raw段行号"]["入选"]["start"] <= loc["raw总行数"]


def test_raw_section_lines_absent_without_verify(ws):
    """不做 --verify-raw 时不应凭空写出 raw段行号。"""
    write_protocol(ws, build_protocol())
    assert run(["--protocol", str(ws / "uploads" / "试验方案.md"), "--workspace", str(ws / "workspace")]) == 0
    d = json.loads((ws / "workspace" / "criteria_meta.json").read_text(encoding="utf-8"))
    assert "raw段行号" not in d


def test_raw_section_lines_empty_when_titles_unparseable():
    assert mod.raw_section_lines("# 只有一个标题\n正文\n") == {}


def test_summarize_warns_about_coordinate_mixing(ws):
    p = write_protocol(ws, build_protocol())
    raw = write_raw(ws, build_raw())
    loc = mod.locate(p)
    mod.verify_raw(loc, raw)
    text = mod.summarize(loc, [], None)
    assert "raw段行号" in text and "双轨解析用这一套" in text
    assert "越界" in text and "空串" in text
