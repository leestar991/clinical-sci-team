"""pdf-image-extractor 编排脚本回归测试：上传归类预检 + OCR 覆盖率自检。

锁定三次真实故障的判据：
- thread d1ce04c0：0 字节 sidecar 被当成待 OCR 输入 → 流程卡在读空文件；
- thread 6f0f0504：同一份 PDF 整份 + 逐页 + 单页PDF 重复解析，OCR 产出为空；
- 覆盖率分母误用 manifest.total_pages（mixed 型 PDF 的文本层页无需 OCR）。
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "skills" / "custom" / "pdf-image-extractor" / "scripts"

if not (SCRIPTS / "classify_uploads.py").exists():  # skills/custom 为 gitignore 目录
    pytest.skip("pdf-image-extractor 技能未安装", allow_module_level=True)


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


classify_uploads = _load("classify_uploads")
ocr_coverage = _load("ocr_coverage")


# --- classify_uploads ------------------------------------------------------


@pytest.fixture()
def uploads(tmp_path: Path) -> Path:
    """复现真实上传目录：扫描件 PDF + 0 字节 sidecar + docx + 有内容 sidecar。"""
    up = tmp_path / "uploads"
    up.mkdir()
    (up / "M016（ZALO）.pdf").write_bytes(b"x" * 7_721_565)
    (up / "M016（ZALO）.md").write_text("", encoding="utf-8")  # 中间件转换失败
    (up / "入排标准.docx").write_bytes(b"y" * 92_744)
    (up / "入排标准.md").write_text("z" * 6_848, encoding="utf-8")
    return up


def test_zero_byte_sidecar_is_ignored_not_ocr_target(uploads: Path):
    r = classify_uploads.classify(uploads)
    ignored = {i["file"]: i["reason"] for i in r["ignored"]}
    assert "M016（ZALO）.md" in ignored
    assert "size=0" in ignored["M016（ZALO）.md"]
    # 绝不能出现在任何待处理列表里
    assert all("M016（ZALO）.md" != i.get("file") for i in r["non_pdf"])
    assert all("M016（ZALO）.md" != i.get("pdf") for i in r["scan"] + r["mixed"] + r["text"])


def test_nonempty_sidecar_is_ignored_but_counted_as_md_size(uploads: Path):
    r = classify_uploads.classify(uploads)
    ignored = {i["file"] for i in r["ignored"]}
    assert "入排标准.md" in ignored  # 不作为独立输入
    docx = next(i for i in r["non_pdf"] if i["file"] == "入排标准.docx")
    assert docx["sidecar_md"] == "入排标准.md"
    assert docx["sidecar_md_size"] == 6848


def test_scan_type_and_route_is_pending_not_defaulted(uploads: Path):
    """⛔ 不得预设默认路线：默认值会让'未确认'看起来像'已确认'（历史故障：模型凭空写
    route_reason='用户已确认单患者模式' 直接推进）。"""
    r = classify_uploads.classify(uploads)
    assert [i["pdf"] for i in r["scan"]] == ["M016（ZALO）.pdf"]
    entry = r["scan"][0]
    assert entry["md_size"] == 0
    assert entry["ocr_route"] is None
    assert entry["route_reason"] is None


def test_protocol_role_hint_from_filename(uploads: Path):
    r = classify_uploads.classify(uploads)
    assert next(i for i in r["non_pdf"] if i["file"] == "入排标准.docx")["role"] == "protocol_criteria"


def test_mixed_and_text_thresholds(tmp_path: Path):
    up = tmp_path / "uploads"
    up.mkdir()
    (up / "mix.pdf").write_bytes(b"x" * 2100)
    (up / "mix.md").write_text("y" * 100, encoding="utf-8")  # 21x → mixed
    (up / "txt.pdf").write_bytes(b"x" * 1000)
    (up / "txt.md").write_text("y" * 100, encoding="utf-8")  # 10x → text
    r = classify_uploads.classify(up)
    assert [i["pdf"] for i in r["mixed"]] == ["mix.pdf"]
    assert [i["pdf"] for i in r["text"]] == ["txt.pdf"]
    assert r["text"][0]["handled_by"] == "read_md"  # 直接读 sidecar，不 OCR
    assert "ocr_route" not in r["text"][0]


def test_manifest_pages_backfilled_and_scanned_is_the_denominator(uploads: Path, tmp_path: Path):
    images = tmp_path / "images" / "M016（ZALO）"
    images.mkdir(parents=True)
    (images / "M016（ZALO）_manifest.json").write_text(
        json.dumps(
            {
                "total_pages": 10,
                "text_pages": 4,
                "scanned_pages": 6,
                "pages": [{"filename": f"M016（ZALO）_page_{i:03d}.jpg", "type": "scanned" if i > 4 else "text"} for i in range(1, 11)],
            }
        ),
        encoding="utf-8",
    )
    r = classify_uploads.classify(uploads, tmp_path / "images")
    entry = r["scan"][0]
    assert entry["total_pages"] == 10
    assert entry["scanned_pages"] == 6  # 只有扫描页需要 OCR


def test_rerun_preserves_manual_route_and_role(uploads: Path, tmp_path: Path):
    """重跑归类不得覆盖已人工/LLM 判定的 ocr_route 与 role（否则降级决策会被冲掉）。"""
    out = tmp_path / "pdf_classification.json"
    classify_uploads.main(["--uploads", str(uploads), "--out", str(out)])
    data = json.loads(out.read_text(encoding="utf-8"))
    data["scan"][0]["ocr_route"] = "B"
    data["scan"][0]["route_reason"] = "用户选择模式2：单患者逐页图像 OCR"
    next(i for i in data["non_pdf"] if i["file"] == "入排标准.docx")["handled_by"] = "phase1_criteria_extract"
    out.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    classify_uploads.main(["--uploads", str(uploads), "--out", str(out)])
    again = json.loads(out.read_text(encoding="utf-8"))
    assert again["scan"][0]["ocr_route"] == "B"
    assert "模式2" in again["scan"][0]["route_reason"]
    assert next(i for i in again["non_pdf"] if i["file"] == "入排标准.docx")["handled_by"] == "phase1_criteria_extract"


# --- ocr_coverage ----------------------------------------------------------


def _workspace(tmp_path: Path, route: str | None, pages: list[tuple[int, str]]) -> Path:
    ws = tmp_path / "workspace"
    (ws / "images" / "S1").mkdir(parents=True)
    (ws / "ocr" / "S1").mkdir(parents=True)
    (ws / "pdf_classification.json").write_text(
        json.dumps({"scan": [{"pdf": "S1.pdf", "source_name": "S1", "ocr_route": route}], "mixed": []}),
        encoding="utf-8",
    )
    (ws / "images" / "S1" / "S1_manifest.json").write_text(
        json.dumps(
            {
                "total_pages": len(pages),
                "scanned_pages": sum(1 for _, t in pages if t == "scanned"),
                "pages": [{"filename": f"S1_page_{i:03d}.jpg", "type": t} for i, t in pages],
            }
        ),
        encoding="utf-8",
    )
    return ws


def test_route_b_denominator_excludes_text_pages(tmp_path: Path):
    """分母必须是 scanned 页；用 total_pages 会永远判不覆盖（历史故障）。"""
    ws = _workspace(tmp_path, "B", [(1, "text"), (2, "text"), (3, "scanned"), (4, "scanned")])
    for i in (3, 4):
        (ws / "ocr" / "S1" / f"S1_page_{i:03d}.md").write_text("x", encoding="utf-8")
    r = ocr_coverage.check(ws)
    s = r["sources"][0]
    assert (s["route"], s["need"], s["done"], s["covered"]) == ("B", 2, 2, True)
    assert r["all_covered"] is True


def test_route_b_reports_missing_pages(tmp_path: Path):
    ws = _workspace(tmp_path, "B", [(1, "scanned"), (2, "scanned"), (3, "scanned")])
    (ws / "ocr" / "S1" / "S1_page_001.md").write_text("x", encoding="utf-8")
    r = ocr_coverage.check(ws)
    assert r["sources"][0]["missing"] == ["S1_page_002", "S1_page_003"]
    assert r["all_covered"] is False


def test_route_a_only_needs_full_md(tmp_path: Path):
    ws = _workspace(tmp_path, "A", [(i, "scanned") for i in range(1, 11)])
    r = ocr_coverage.check(ws)
    assert r["sources"][0]["covered"] is False  # 尚未产出 _full.md
    (ws / "ocr" / "S1" / "S1_full.md").write_text("x", encoding="utf-8")
    r2 = ocr_coverage.check(ws)
    assert r2["sources"][0]["covered"] is True and r2["all_covered"] is True


def test_duplicate_parse_detected(tmp_path: Path):
    """复现 6f0f0504：12 次 parse 调用、0 份 OCR 产出。"""
    ws = _workspace(tmp_path, "B", [(i, "scanned") for i in range(1, 11)])
    for i in range(12):
        d = ws / "parsed" / f"hash{i:02d}"
        d.mkdir(parents=True)
        (d / "index.json").write_text("{}", encoding="utf-8")
    r = ocr_coverage.check(ws)
    assert (r["parse_calls"], r["ocr_outputs"]) == (12, 0)
    assert r["duplicate_parse_suspected"] is True
    assert r["all_covered"] is False


def test_no_duplicate_when_calls_match_outputs(tmp_path: Path):
    ws = _workspace(tmp_path, "A", [(1, "scanned")])
    (ws / "ocr" / "S1" / "S1_full.md").write_text("x", encoding="utf-8")
    d = ws / "parsed" / "hash01"
    d.mkdir(parents=True)
    (d / "index.json").write_text("{}", encoding="utf-8")
    r = ocr_coverage.check(ws)
    assert r["duplicate_parse_suspected"] is False and r["all_covered"] is True


def test_pending_route_is_not_treated_as_a(tmp_path: Path):
    """ocr_route 为空时必须报"未选择"，绝不能当成路线 A 判成已覆盖。"""
    ws = _workspace(tmp_path, None, [(1, "scanned"), (2, "scanned")])
    r = ocr_coverage.check(ws)
    s = r["sources"][0]
    assert s["route"] is None
    assert s["covered"] is False
    assert "ocr_route 未选择" in s["missing"][0]
    assert r["all_covered"] is False
    assert "未选择" in ocr_coverage.summarize(r)


def test_pending_route_ignores_existing_full_md(tmp_path: Path):
    """即便已存在 _full.md，未选择路线时也不得判为覆盖完成。"""
    ws = _workspace(tmp_path, None, [(1, "scanned")])
    (ws / "ocr" / "S1" / "S1_full.md").write_text("x", encoding="utf-8")
    assert ocr_coverage.check(ws)["all_covered"] is False


def test_cli_writes_json_report(tmp_path: Path):
    ws = _workspace(tmp_path, "B", [(1, "scanned")])
    out = tmp_path / "nested" / "coverage.json"
    assert ocr_coverage.main(["--workspace", str(ws), "--json", str(out)]) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["sources"][0]["missing"] == ["S1_page_001"]
