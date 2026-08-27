"""`ocr_page_index.py` 回归测试：聚合后的页码 → 行区间索引。

防的是会话 `09eeaffb` 那条浪费链：页边界在聚合时是**已知的**，不落盘判定子代理就得自己
一轮轮 grep 重建 —— 该会话的 IN 轨子代理把一份 7,604 行的 `ocr_records.md` `read_file`
了 34 次（请求区间 69% 重复），到第 143 个 AI 回合才拼出页表，而那张表与本脚本的输出
逐行相同。读进来的正文被后续每一轮重新继承 → 反复压缩 → 撞 `recursion_limit`、零产物。

本测试锁三件事：
1. `start_line`/`end_line` 是 **1-based 闭区间**，且能直接喂给 `read_file`（切片对得上）；
2. 页码取自文件名 `_page_{NNN}`，**不信**正文里的「第 N 页」（后者可能缺失/错位）；
3. 无页块（页块起始行缺失，thread `1fee1395` 形态）→ `exit 2`，不静默产出空索引。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "skills" / "custom" / "patient-separator" / "scripts" / "ocr_page_index.py"

if not SCRIPT.exists():  # skills/custom 为 gitignore 目录，清空检出时跳过
    pytest.skip("patient-separator 技能未安装", allow_module_level=True)


def _load():
    spec = importlib.util.spec_from_file_location("ocr_page_index", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


idx = _load()

VIRTUAL = "/mnt/user-data/workspace/images"


def _page_block(source: str, page: int, body_lines: list[str], *, suffix: str = "") -> str:
    """一个页块：`（来源图片：…）` 起始行 + 正文。与 parse_image_batch 的写法同形。"""
    head = f"（来源图片：{VIRTUAL}/{source}/{source}_page_{page:03d}.jpg{suffix}）"
    return "\n".join([head, *body_lines])


def build_records(ws: Path, patient: str, source: str, pages: dict[int, list[str]], *, suffix: str = "") -> Path:
    out = ws / "patients" / patient / "ocr" / source
    out.mkdir(parents=True, exist_ok=True)
    text = "\n\n".join(_page_block(source, p, body, suffix=suffix) for p, body in sorted(pages.items()))
    records = out / "ocr_records.md"
    records.write_text(text + "\n", encoding="utf-8")
    return records


# ── 行区间口径 ──────────────────────────────────────────────────────


def test_ranges_are_1based_inclusive_and_slice_correctly(tmp_path):
    """索引给的区间用来切原文，必须正好切出该页 —— 这是它唯一的用途。"""
    records = build_records(
        tmp_path,
        "P001",
        "筛选期检查",
        {1: ["血常规", "Hb 121 g/L"], 2: ["尿常规", "PRO 阴性", "LEU 3+"], 3: ["生化", "ALT 11"]},
    )
    entry = idx.index_source(records)
    lines = records.read_text(encoding="utf-8").splitlines()

    assert entry["page_count"] == 3
    assert entry["has_page_blocks"] is True
    for blk in entry["pages"]:
        # 1-based 闭区间 → 切片是 [start-1:end]
        chunk = lines[blk["start_line"] - 1 : blk["end_line"]]
        assert chunk[0].startswith("（来源图片："), chunk[0]
        assert f"_page_{blk['page']:03d}" in chunk[0]
        assert len(chunk) == blk["lines"]
        # 该页正文不含别页的来源标注 → 区间没越界到下一页
        assert sum(1 for line in chunk if line.startswith("（来源图片：")) == 1


def test_first_page_starts_at_line_1_and_last_page_ends_at_eof(tmp_path):
    records = build_records(tmp_path, "P001", "S", {1: ["a"], 2: ["b"], 3: ["c"]})
    entry = idx.index_source(records)
    total = entry["total_lines"]

    assert entry["pages"][0]["start_line"] == 1
    assert entry["pages"][-1]["end_line"] == total
    # 区间连续无缝隙、无重叠（合起来正好覆盖全文）
    covered = [n for blk in entry["pages"] for n in range(blk["start_line"], blk["end_line"] + 1)]
    assert covered == list(range(1, total + 1))


def test_page_number_comes_from_filename_not_body_text(tmp_path):
    """正文的「第 N 页」可能缺失/错位，页码只认文件名 `_page_{NNN}`。"""
    records = build_records(
        tmp_path,
        "P001",
        "S",
        {
            1: ["第 7 页", "错位的页码文本"],  # 正文说第 7 页
            2: ["无页码行的一页"],  # 正文完全没有页码
        },
    )
    entry = idx.index_source(records)
    assert [blk["page"] for blk in entry["pages"]] == [1, 2]


def test_text_layer_suffix_still_parses(tmp_path):
    """`collect_text_pages.py` 写的是同前缀 + ` 文本层…` 后缀，必须一样能切。"""
    records = build_records(
        tmp_path,
        "P001",
        "S",
        {1: ["扫描页"], 2: ["文本层页"]},
        suffix=" 文本层，第 2 页，PDF 内嵌文本逐字导出，未经 OCR",
    )
    entry = idx.index_source(records)
    assert entry["page_count"] == 2
    assert [blk["page"] for blk in entry["pages"]] == [1, 2]


def test_index_paths_are_virtual_not_host(tmp_path):
    """会话 156a476e：`file`/`index_file` 是写进 `.json` 的数据，必须是虚拟路径。

    沙箱会把命令行里的 `/mnt/user-data/...` 重写成宿主机路径再执行，脚本读文件没问题，
    但 JSON 产物里的路径要被判定子代理（在容器语境里）回读，宿主机绝对路径换部署即失效。
    """
    build_records(tmp_path, "P1", "筛选期病历", {1: ["a"], 3: ["b"]})
    result = idx.build(tmp_path)
    patient = result["patients"][0]
    entry = patient["sources"]["筛选期病历"]
    assert entry["file"] == "/mnt/user-data/workspace/patients/P1/ocr/筛选期病历/ocr_records.md"
    assert patient["index_file"] == "/mnt/user-data/workspace/patients/P1/ocr_page_index.json"
    assert str(tmp_path) not in json.dumps(result, ensure_ascii=False)


def test_image_path_is_captured_for_screenshot_ref(tmp_path):
    """`image` 是 evidence[].screenshot_ref 的来源，必须原样带出。"""
    records = build_records(tmp_path, "P001", "筛选期病历", {3: ["入院记录"]})
    entry = idx.index_source(records)
    assert entry["pages"][0]["image"] == f"{VIRTUAL}/筛选期病历/筛选期病历_page_003.jpg"


# ── 多来源 / 多患者 ─────────────────────────────────────────────────


def test_build_indexes_every_source_of_every_patient(tmp_path):
    build_records(tmp_path, "P001", "筛选期病历", {1: ["a"], 2: ["b"]})
    build_records(tmp_path, "P001", "筛选期检查", {1: ["c"]})
    build_records(tmp_path, "P002", "筛选期病历", {1: ["d"]})

    result = idx.build(tmp_path)
    assert result["problems"] == []
    by_patient = {r["patient_id"]: r for r in result["patients"]}
    assert sorted(by_patient) == ["P001", "P002"]
    assert set(by_patient["P001"]["sources"]) == {"筛选期病历", "筛选期检查"}
    assert by_patient["P001"]["sources"]["筛选期病历"]["page_count"] == 2

    # 每位患者各落一份索引
    for pid in ("P001", "P002"):
        written = json.loads((tmp_path / "patients" / pid / "ocr_page_index.json").read_text(encoding="utf-8"))
        assert written["patient_id"] == pid
        assert written["sources"]


def test_patient_filter_only_touches_that_patient(tmp_path):
    build_records(tmp_path, "P001", "S", {1: ["a"]})
    build_records(tmp_path, "P002", "S", {1: ["b"]})

    idx.build(tmp_path, "P001")
    assert (tmp_path / "patients" / "P001" / "ocr_page_index.json").exists()
    assert not (tmp_path / "patients" / "P002" / "ocr_page_index.json").exists()


def test_rerun_is_idempotent(tmp_path):
    build_records(tmp_path, "P001", "S", {1: ["a"], 2: ["b"]})
    first = idx.build(tmp_path)
    written_once = (tmp_path / "patients" / "P001" / "ocr_page_index.json").read_text(encoding="utf-8")
    second = idx.build(tmp_path)
    written_twice = (tmp_path / "patients" / "P001" / "ocr_page_index.json").read_text(encoding="utf-8")

    assert written_once == written_twice
    assert first["patients"][0]["sources"] == second["patients"][0]["sources"]


# ── 失败形态 ────────────────────────────────────────────────────────


def test_missing_page_blocks_is_exit_2(tmp_path):
    """thread `1fee1395`：来源标注一行都没写 → 判定产物的 page/screenshot_ref 会整体为空。

    这必须报错而不是产出一份 `pages: []` 的索引 —— 后者会让下游以为"这份就是没有页码"。
    """
    out = tmp_path / "patients" / "P001" / "ocr" / "S"
    out.mkdir(parents=True)
    (out / "ocr_records.md").write_text("血常规\nHb 121\n尿常规\n", encoding="utf-8")

    assert idx.main(["--workspace", str(tmp_path)]) == 2
    result = idx.build(tmp_path)
    assert len(result["problems"]) == 1
    assert "解析不出任何页块" in result["problems"][0]
    # 索引仍落盘（便于排查），但明确标注 has_page_blocks=false
    entry = json.loads((tmp_path / "patients" / "P001" / "ocr_page_index.json").read_text(encoding="utf-8"))
    assert entry["sources"]["S"]["has_page_blocks"] is False
    assert entry["sources"]["S"]["pages"] == []


def test_no_patients_dir_reports_problem(tmp_path):
    result = idx.build(tmp_path)
    assert result["patients"] == []
    assert result["problems"] and "先跑按患者聚合" in result["problems"][0]


def test_exit_0_when_all_sources_have_page_blocks(tmp_path):
    build_records(tmp_path, "P001", "S", {1: ["a"], 2: ["b"]})
    assert idx.main(["--workspace", str(tmp_path)]) == 0


def test_source_without_records_file_is_skipped(tmp_path):
    """OCR 目录存在但还没聚合出 ocr_records.md → 不报错、不产空条目。"""
    (tmp_path / "patients" / "P001" / "ocr" / "空来源").mkdir(parents=True)
    build_records(tmp_path, "P001", "S", {1: ["a"]})

    result = idx.build(tmp_path)
    assert result["problems"] == []
    assert sorted(result["patients"][0]["sources"]) == ["S"]
