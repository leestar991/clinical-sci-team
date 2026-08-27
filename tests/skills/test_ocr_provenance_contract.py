"""`ocr_records.md` 页块起始行的**单一契约**回归测试。

背景：`ocr_records.md` 的每个页块靠首行的来源标注切分，那一行是判定产物里
`evidence[].page` / `screenshot_ref` 的唯一来源（`patient-separator/references/aggregate-ocr.md`）。
历史上同一件事出现过 4 种写法：

| 写法 | 出处 |
|---|---|
| `（来源图片：/Users/…/x_page_001.jpg）` | thread `4d1f95b4` / `345f2bf4` / `69612125` |
| `来源图片：/Users/…/x_page_001.jpg`（无括号） | thread `9a83ccc9` |
| `<!-- source-image: x_page_001.jpg -->` | thread `dfbb4554` |
| `（来源文本层：{txt_path}…` | `collect_text_pages.py` |

再加上 thread `1fee1395` 干脆一行都没有（`parse_image_batch` 接管落盘后无人写），
下游要认 5 种情况。本测试把契约锁成**一个**正则，并逐个写入方核对：

- `（来源图片：{虚拟路径}）` —— 扫描页，由 `parse_image_batch` 写；
- `（来源图片：{虚拟路径} 文本层…）` —— PDF 内嵌文本层页，由 `collect_text_pages.py` 写；
  同前缀，便于下游一次切分，`文本层` 标识如实反映该页未经 OCR。

路径一律 `/mnt/user-data/...` 虚拟路径：历史产物写的是宿主机 `/Users/...`，换部署即失效。
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SKILLS = REPO / "skills" / "custom"
EXTRACTOR = SKILLS / "pdf-image-extractor"
AGGREGATE_DOC = SKILLS / "patient-separator" / "references" / "aggregate-ocr.md"
TEXTIN_TOOL = REPO / "backend" / "packages" / "community" / "cellflow_community" / "textin" / "tools.py"

# 唯一契约。`（来源图片：` 前缀 + `）` 收尾，中间是虚拟路径（可带 ` 文本层…` 等后缀说明）。
PROVENANCE_PREFIX = "（来源图片："
PROVENANCE_RE = re.compile(r"^（来源图片：(?P<path>/mnt/user-data/[^）]+?)(?: 文本层[^）]*)?）$")
# 允许在 tmp_path 上跑脚本级用例时放宽路径前缀，只校验形状。
PROVENANCE_SHAPE_RE = re.compile(r"^（来源图片：[^）]+）$")


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, EXTRACTOR / "scripts" / f"{name}.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ctp = _load("collect_text_pages")


def _first_line(text: str) -> str:
    return text.split("\n", 1)[0]


# ── 契约本身 ────────────────────────────────────────────────────────


def test_canonical_regex_accepts_both_page_kinds():
    scanned = "（来源图片：/mnt/user-data/workspace/images/M018/M018_page_001.jpg）"
    text_layer = (
        "（来源图片：/mnt/user-data/workspace/images/M018/M018_page_004.txt 文本层，第 4 页，"
        "PDF 内嵌文本逐字导出，未经 OCR；无 key-fields 速览，判定请读正文）"
    )
    assert PROVENANCE_RE.match(scanned)
    assert PROVENANCE_RE.match(text_layer)
    assert PROVENANCE_RE.match(scanned).group("path").endswith("M018_page_001.jpg")


def test_canonical_regex_rejects_the_four_historical_variants():
    for bad in (
        "来源图片：/mnt/user-data/workspace/images/M018/M018_page_001.jpg",  # 9a83ccc9：无括号
        "<!-- source-image: M018_page_001.jpg -->",  # dfbb4554
        "（来源文本层：/mnt/user-data/workspace/images/M018/M018_page_004.txt）",  # 旧 collect_text_pages
        "（来源图片：/Users/louli/…/images/M018/M018_page_001.jpg）",  # 宿主机绝对路径
    ):
        assert not PROVENANCE_RE.match(bad), f"契约不应接受历史变体：{bad}"


# ── 写入方 1：collect_text_pages.py（文本层页） ─────────────────────


def test_collect_text_pages_render_matches_contract(tmp_path: Path):
    txt = tmp_path / "S_page_004.txt"
    head = _first_line(ctp.render(txt, 4, "检测结论：KRAS p.(G13D) 26.29%。", tmp_path))
    assert head.startswith(PROVENANCE_PREFIX), f"文本层页首行必须用统一前缀：{head!r}"
    assert PROVENANCE_SHAPE_RE.match(head), f"形状不符：{head!r}"
    assert "文本层" in head, "须保留文本层标识——这页确实没经过 OCR"
    assert "未经 OCR" in head


def test_collect_text_pages_render_keeps_full_path_not_just_stem():
    """只写 stem 会让下游拿不到原文件位置（aggregate-ocr 的旧回退分支就是这个毛病）。"""
    ws = Path("/mnt/user-data/workspace")
    txt = ws / "images" / "M018" / "M018_page_004.txt"
    head = _first_line(ctp.render(txt, 4, "正文", ws))
    assert PROVENANCE_RE.match(head), f"文本层页首行须含完整虚拟路径：{head!r}"


def test_collect_text_pages_render_rewrites_host_workspace_to_virtual(tmp_path: Path):
    """会话 156a476e：沙箱把 --workspace 重写成宿主机路径后执行，来源标注仍必须是虚拟路径。"""
    ws = tmp_path / "user-data" / "workspace"
    ws.mkdir(parents=True)
    txt = ws / "images" / "S" / "S_page_004.txt"
    head = _first_line(ctp.render(txt, 4, "正文", ws))
    assert PROVENANCE_RE.match(head), f"宿主机 workspace 须换算回虚拟路径：{head!r}"
    assert str(tmp_path) not in head, "宿主机绝对路径不得泄漏进来源标注"


def test_collect_text_pages_render_body_is_verbatim(tmp_path: Path):
    body = "第 4 页。检测结论：KRAS p.(G13D) 26.29%。"
    out = ctp.render(tmp_path / "S_page_004.txt", 4, body, tmp_path)
    assert body in out, "正文必须逐字保留，标注只能加在前面"


# ── 写入方 2：parse_image_batch（扫描页，backend 工具） ──────────────


def test_textin_tool_declares_the_same_prefix():
    """工具侧的常量与本契约同源；改名/改格式必须同时改这里，否则测试红。

    工具行为本身由 `backend/tests/test_textin_parse_image_batch.py` 锁定，这里只防
    「两边各自演化」的漂移。
    """
    src = TEXTIN_TOOL.read_text(encoding="utf-8")
    assert f'_PROVENANCE_PREFIX = "{PROVENANCE_PREFIX}"' in src
    assert 'return f"{_PROVENANCE_PREFIX}{src_path}）"' in src


# ── 写入方 3：aggregate-ocr.md 的回退分支与页块规范 ──────────────────


def test_aggregate_doc_fallback_uses_full_virtual_path():
    """`.txt` 回退分支旧写法是 `（来源图片：{stem} 文本层）`——只有 stem，没有路径。"""
    doc = AGGREGATE_DOC.read_text(encoding="utf-8")
    assert "（来源图片：{stem} 文本层）" not in doc, "回退分支不得只写 stem"


def test_aggregate_doc_documents_virtual_path_not_host_path():
    doc = AGGREGATE_DOC.read_text(encoding="utf-8")
    assert "该页原图绝对路径" not in doc, "页块规范应写虚拟路径，不是宿主机绝对路径"
    assert "/mnt/user-data/" in doc


def test_aggregate_doc_states_the_tool_writes_the_header():
    """契约的执行者已从「OCR 子代理手写」变成「parse_image_batch 写」，文档必须点明。"""
    doc = AGGREGATE_DOC.read_text(encoding="utf-8")
    assert "parse_image_batch" in doc


# ── 写入方约束：技能文档不得再要求模型手写/改写 ──────────────────────


def test_extractor_skill_forbids_rewriting_the_provenance_line():
    skill = (EXTRACTOR / "SKILL.md").read_text(encoding="utf-8")
    assert "parse_image_batch" in skill, "SKILL 须说明来源标注由工具写入"
    assert "不得重写" in skill or "不得改写" in skill
