"""`collect_text_pages.py` + `ocr_coverage.py` 文本层页覆盖的回归测试。

核心防线：thread `69612125` —— `pdf_to_image.py --text-mode auto` 对文本层页只写 `.txt`
不渲染图片，OCR 子代理只处理图片，`.txt` 无人认领；而 `ocr_coverage.py` 当时把分母定为
仅 `type == "scanned"` 的页，于是 26 页里 11 页文本层内容（含 `KRAS ... p.(G13D)` 基因
检测报告）静默丢失，却报 `covered=True ✅ 覆盖完整`。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "skills" / "custom" / "pdf-image-extractor" / "scripts"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ctp = _load("collect_text_pages")
cov = _load("ocr_coverage")


# ── 夹具 ────────────────────────────────────────────────────────────


def build_source(
    ws: Path,
    source: str,
    *,
    scanned: int = 3,
    text: int = 2,
    route: str | None = "B",
    ocr_done: tuple[str, ...] | None = None,
    text_content: str = "检测结论：KRAS p.(G13D) 26.29%。",
    write_manifest: bool = True,
    empty_txt: tuple[int, ...] = (),
    missing_txt: tuple[int, ...] = (),
) -> None:
    """造出 images/{source}/ 的分页产物 + manifest + pdf_classification.json 条目。"""
    img = ws / "images" / source
    img.mkdir(parents=True, exist_ok=True)
    pages = []
    for i in range(1, scanned + 1):
        name = f"{source}_page_{i:03d}.jpg"
        (img / name).write_bytes(b"\xff\xd8\xff")
        pages.append({"page": i, "filename": name, "format": "jpg", "type": "scanned", "text_chars": 0})
    for j in range(scanned + 1, scanned + text + 1):
        name = f"{source}_page_{j:03d}.txt"
        if j not in missing_txt:
            (img / name).write_text("" if j in empty_txt else f"第 {j} 页。{text_content}", encoding="utf-8")
        pages.append({"page": j, "filename": name, "format": "txt", "type": "text", "text_chars": 1200})
    if write_manifest:
        (img / f"{source}_manifest.json").write_text(
            json.dumps(
                {
                    "source": f"{source}.pdf",
                    "stem": source,
                    "total_pages": scanned + text,
                    "text_mode": "auto",
                    "text_pages": text,
                    "scanned_pages": scanned,
                    "pages": pages,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    ocr = ws / "ocr" / source
    ocr.mkdir(parents=True, exist_ok=True)
    for stem in ocr_done or ():
        (ocr / f"{stem}.md").write_text("（来源图片：x）\n\n正文\n", encoding="utf-8")

    cls_path = ws / "pdf_classification.json"
    cls = json.loads(cls_path.read_text(encoding="utf-8")) if cls_path.exists() else {"scan": [], "mixed": []}
    cls["mixed"].append({"pdf": f"{source}.pdf", "source_name": source, "ocr_route": route})
    cls_path.write_text(json.dumps(cls, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    return tmp_path


def scanned_stems(source: str, n: int) -> tuple[str, ...]:
    return tuple(f"{source}_page_{i:03d}" for i in range(1, n + 1))


# ── ocr_coverage：分母必须含文本层页 ────────────────────────────────


def test_coverage_denominator_includes_text_pages(ws):
    """ec 原始症状：只算 scanned 会把缺 11 页报成完整覆盖。"""
    build_source(ws, "筛选期检查", scanned=15, text=11, ocr_done=scanned_stems("筛选期检查", 15))
    rep = cov.check(ws)
    s = next(x for x in rep["sources"] if x["source"] == "筛选期检查")
    assert s["need"] == 26, "分母必须是全部页，不是仅 scanned"
    assert s["need_scanned"] == 15 and s["need_text"] == 11
    assert s["done"] == 15
    assert s["covered"] is False, "缺 11 页文本层不得报覆盖完整"
    assert len(s["missing_text"]) == 11
    assert s["missing_scanned"] == []
    assert rep["all_covered"] is False


def test_coverage_separates_two_remediation_paths(ws):
    """扫描页缺 → 派 OCR；文本层页缺 → 跑归集脚本。两者不能混为一谈。"""
    build_source(ws, "S", scanned=3, text=2, ocr_done=("S_page_001",))
    rep = cov.check(ws)
    text = cov.summarize(rep)
    assert "missing_scanned=['S_page_002', 'S_page_003']" in text
    assert "missing_text=['S_page_004', 'S_page_005']" in text
    assert "collect_text_pages.py" in text
    assert "禁止" in text and "view_image" in text, "必须写明文本层页不要派 OCR"


def test_coverage_passes_when_text_pages_collected(ws):
    build_source(ws, "S", scanned=3, text=2, ocr_done=(*scanned_stems("S", 3), "S_page_004", "S_page_005"))
    rep = cov.check(ws)
    s = rep["sources"][0]
    assert s["need"] == 5 and s["done"] == 5 and s["covered"] is True
    assert "✅ 覆盖完整" in cov.summarize(rep)


def test_coverage_pure_scan_source_unaffected(ws):
    """全扫描页的 source 行为不变（不得因本次改动引入回归）。"""
    build_source(ws, "病历", scanned=13, text=0, ocr_done=scanned_stems("病历", 13))
    s = cov.check(ws)["sources"][0]
    assert s["need"] == 13 and s["need_text"] == 0 and s["covered"] is True


def test_coverage_falls_back_to_txt_glob_without_manifest(ws):
    build_source(ws, "S", scanned=2, text=2, write_manifest=False, ocr_done=scanned_stems("S", 2))
    s = cov.check(ws)["sources"][0]
    assert s["need_text"] == 2, "无 manifest 时应退化为扫目录 .txt"
    assert s["covered"] is False


def test_coverage_route_a_ignores_text_pages(ws):
    build_source(ws, "S", scanned=2, text=2, route="A")
    (ws / "ocr" / "S" / "S_full.md").write_text("整份", encoding="utf-8")
    s = cov.check(ws)["sources"][0]
    assert s["route"] == "A" and s["covered"] is True


# ── collect_text_pages ─────────────────────────────────────────────


def test_collect_writes_md_for_each_text_page(ws):
    build_source(ws, "S", scanned=3, text=2, ocr_done=scanned_stems("S", 3))
    rep = ctp.collect(ws)
    assert rep["total_written"] == 2
    for j in (4, 5):
        p = ws / "ocr" / "S" / f"S_page_{j:03d}.md"
        assert p.exists()
        body = p.read_text(encoding="utf-8")
        assert "来源文本层" in body and f"第 {j} 页" in body
        assert "未经 OCR" in body
        assert "KRAS p.(G13D) 26.29%" in body, "正文必须逐字进来"


def test_collect_does_not_fabricate_key_fields(ws):
    """脚本不得生成 key-fields —— 那需要语义理解，等于编造。"""
    build_source(ws, "S", scanned=1, text=1)
    ctp.collect(ws)
    body = (ws / "ocr" / "S" / "S_page_002.md").read_text(encoding="utf-8")
    assert "key-fields:" not in body
    assert "无 key-fields 速览" in body, "须显式说明缺速览，避免下游误以为漏了"


def test_collect_is_idempotent(ws):
    build_source(ws, "S", scanned=1, text=2)
    assert ctp.collect(ws)["total_written"] == 2
    second = ctp.collect(ws)
    assert second["total_written"] == 0
    assert second["total_skipped"] == 2


def test_collect_overwrites_empty_existing_md(ws):
    build_source(ws, "S", scanned=1, text=1)
    dst = ws / "ocr" / "S"
    dst.mkdir(parents=True, exist_ok=True)
    (dst / "S_page_002.md").write_text("   \n", encoding="utf-8")
    assert ctp.collect(ws)["total_written"] == 1


def test_collect_reports_missing_txt(ws):
    build_source(ws, "S", scanned=1, text=2, missing_txt=(3,))
    rep = ctp.collect(ws)
    assert any("文本层文件缺失" in p for p in rep["problems"])
    assert rep["total_written"] == 1


def test_collect_reports_empty_txt_and_suggests_render(ws):
    """`.txt` 为空说明文本层探测有误，该页应改按扫描页渲染 + OCR。"""
    build_source(ws, "S", scanned=1, text=1, empty_txt=(2,))
    rep = ctp.collect(ws)
    assert any("为空" in p and "渲染后 OCR" in p for p in rep["problems"])


def test_collect_skips_route_a(ws):
    build_source(ws, "S", scanned=1, text=2, route="A")
    rep = ctp.collect(ws)
    assert rep["total_written"] == 0
    assert "路线 A" in rep["sources"][0]["note"]
    assert not (ws / "ocr" / "S" / "S_page_002.md").exists()


def test_collect_only_named_source(ws):
    build_source(ws, "A1", scanned=1, text=1)
    build_source(ws, "B1", scanned=1, text=1)
    ctp.collect(ws, only="A1")
    assert (ws / "ocr" / "A1" / "A1_page_002.md").exists()
    assert not (ws / "ocr" / "B1" / "B1_page_002.md").exists()


def test_collect_ignores_stray_txt_not_in_manifest(ws):
    """只认 manifest 里 type==text 的页，不去猜目录里其它 .txt。"""
    build_source(ws, "S", scanned=1, text=1)
    (ws / "images" / "S" / "随手放的笔记.txt").write_text("无关内容", encoding="utf-8")
    ctp.collect(ws)
    assert not (ws / "ocr" / "S" / "随手放的笔记.md").exists()


def test_collect_tolerates_missing_manifest(ws):
    build_source(ws, "S", scanned=1, text=1, write_manifest=False)
    assert ctp.collect(ws)["total_written"] == 0, "无 manifest 时不猜测，交由覆盖率脚本报缺口"


def test_collect_cli_exit_codes(ws):
    build_source(ws, "S", scanned=1, text=1)
    assert ctp.main(["--workspace", str(ws)]) == 0
    build_source(ws, "T", scanned=1, text=1, missing_txt=(2,))
    assert ctp.main(["--workspace", str(ws)]) == 2


def test_collect_cli_json(ws, capsys):
    build_source(ws, "S", scanned=1, text=2)
    assert ctp.main(["--workspace", str(ws), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["total_written"] == 2


# ── 端到端：归集后覆盖率转为通过 ────────────────────────────────────


def test_end_to_end_collect_then_covered(ws):
    build_source(ws, "筛选期检查", scanned=15, text=11, ocr_done=scanned_stems("筛选期检查", 15))
    assert cov.check(ws)["all_covered"] is False
    assert ctp.main(["--workspace", str(ws)]) == 0
    rep = cov.check(ws)
    s = rep["sources"][0]
    assert s["done"] == 26 and s["covered"] is True
    assert rep["all_covered"] is True
