"""screening-report-generator 构建器回归测试。

保证报告始终由技能模板渲染（历史故障：代理手写 HTML/CSS，产出样式与模板完全不同）。
"""

import base64
import importlib.util
import json
import struct
import sys
import zlib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_DIR = REPO_ROOT / "skills" / "custom" / "screening-report-generator"
BUILDER_PATH = SKILL_DIR / "scripts" / "build_reports.py"

if not BUILDER_PATH.exists():  # skills/custom 为 gitignore 目录，清空检出时跳过
    pytest.skip("screening-report-generator 技能未安装", allow_module_level=True)


def _load_builder():
    spec = importlib.util.spec_from_file_location("screening_build_reports", BUILDER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


builder = _load_builder()


def _png_bytes() -> bytes:
    """最小合法 1x1 PNG，避免测试依赖真实图片资源。"""

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    idat = zlib.compress(b"\x00\xff\xff\xff")
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    """构造 user-data 目录：workspace/images + outputs/{criteria,judgments}.json。"""
    root = tmp_path / "user-data"
    ws = root / "workspace"
    outputs = root / "outputs"
    (ws / "images" / "M001").mkdir(parents=True)
    outputs.mkdir(parents=True)
    (ws / "images" / "M001" / "M001_page_001.jpg").write_bytes(_png_bytes())

    criteria = {
        "方案元数据": {"方案编号": "TEST-101", "方案标题": "测试方案", "来源": "入排标准.docx"},
        "解析说明": {"拆分原则": "AND 拆分", "日期处理": "保留时间窗"},
        "四分类": {
            "入选_可从病例获取": [
                {
                    "条件ID": "IN-2-1",
                    "来源标准": "入选标准 第2条",
                    "原文": "筛选时年龄≥18周岁的男性患者。",
                    "子条件": "筛选时年龄≥18周岁",
                    "逻辑关系": "AND",
                    "可从病例获取": True,
                    "转化条件": {"匹配字段": ["年龄"], "运算符": "≥", "阈值": 18, "单位": "周岁"},
                }
            ],
            "入选_不可从病例获取": [],
            "排除_可从病例获取": [
                {
                    "条件ID": "EX-3",
                    "来源标准": "排除标准 第3条",
                    "原文": "存在活动性感染。",
                    "子条件": "存在活动性感染",
                    "可从病例获取": True,
                }
            ],
            "排除_不可从病例获取": [],
        },
        "汇总统计": {"子条件总数": 2},
        "描述索引": {"IN-2": "年龄", "EX-3": "活动性感染"},
    }
    (outputs / "criteria_parsed.json").write_text(json.dumps(criteria, ensure_ascii=False), encoding="utf-8")

    judgments = {
        "patient_id": "M001",
        "patient_name": "张三",
        "protocol_id": "TEST-101",
        "documents": {
            "M001_doc": {
                "doc_label": "既往病历",
                "source_file": "M001.pdf",
                "judgments": {
                    "IN-2-1": {
                        "conclusion": "符合",
                        "reason": "病历记载年龄 83 岁。",
                        "evidence": [
                            {
                                "source": "M001",
                                "page": 1,
                                "quote": "年龄：83岁",
                                "hit": True,
                                "screenshot_ref": "images/M001/M001_page_001.jpg",
                            }
                        ],
                    },
                    "EX-3": {
                        "conclusion": "存疑",
                        "reason": "仅见炎症指标升高。",
                        "evidence": [
                            {
                                "source": "M001",
                                "page": 1,
                                "quote": "CRP 升高",
                                "screenshot_ref": "images/M001/M001_page_001.jpg",
                            }
                        ],
                    },
                },
                "summary": {"符合": 1, "不符合": 0, "存疑": 1, "无法判断": 0},
            }
        },
    }
    (outputs / "judgments_M001.json").write_text(json.dumps(judgments, ensure_ascii=False), encoding="utf-8")
    return root


def _build(root: Path, *extra: str) -> int:
    return builder.main(
        [
            "--criteria",
            str(root / "outputs" / "criteria_parsed.json"),
            "--judgments",
            str(root / "outputs" / "judgments_M001.json"),
            "--workspace",
            str(root / "workspace"),
            "--out-dir",
            str(root / "outputs"),
            *extra,
        ]
    )


def _data_of(path: Path) -> dict:
    return builder.extract_data(path.read_text(encoding="utf-8"))


def test_build_uses_skill_templates_and_passes_verify(workspace: Path):
    assert _build(workspace) == 0
    assert builder.main(["--verify", "--out-dir", str(workspace / "outputs")]) == 0

    for name in ("screening_report.html", "criteria_report.html"):
        html = (workspace / "outputs" / name).read_text(encoding="utf-8")
        template = (SKILL_DIR / "templates" / name).read_text(encoding="utf-8")
        # 模板骨架（<script id="data"> 之外的部分）必须逐字保留
        assert builder.DATA_BLOCK_RE.sub("", html) == builder.DATA_BLOCK_RE.sub("", template)
        for fingerprint in builder.TEMPLATE_FINGERPRINTS[name]:
            assert fingerprint in html


def test_screening_data_contract(workspace: Path):
    _build(workspace)
    data = _data_of(workspace / "outputs" / "screening_report.html")

    assert data["protocol"]["id"] == "TEST-101"
    assert data["ids"] == ["IN-2-1", "EX-3"]  # IN 在 EX 之前，编号自然序
    assert data["crit"]["IN-2-1"]["inc"] is True
    assert data["crit"]["EX-3"]["inc"] is False

    doc = data["docs"]["M001_doc"]
    assert doc["cnt"] == {"符合": 1, "不符合": 0, "存疑": 1, "无法判断": 0}
    # 英文键被归一化为模板期望的中文键
    assert doc["J"]["IN-2-1"]["结论"] == "符合"
    assert doc["J"]["IN-2-1"]["理由"].startswith("病历记载")
    ev = doc["J"]["IN-2-1"]["证据"][0]
    assert ev["原文摘录"] == "年龄：83岁"
    assert ev["页"] == 1
    assert ev["图"].startswith("#img")
    assert ev["原件"][0]["链接"] == "workspace/images/M001/M001_page_001.jpg"


def test_evidence_images_are_pooled_and_deduped(workspace: Path):
    _build(workspace)
    data = _data_of(workspace / "outputs" / "screening_report.html")
    # 两条证据引用同一页截图 → 池中只有 1 张
    assert len(data["imgs"]) == 1
    key, uri = next(iter(data["imgs"].items()))
    assert uri.startswith("data:image/")
    assert base64.b64decode(uri.split(",", 1)[1])[:8] == b"\x89PNG\r\n\x1a\n"
    refs = {ev["图"] for j in data["docs"]["M001_doc"]["J"].values() for ev in j["证据"]}
    assert refs == {key}


def test_no_images_keeps_links_only(workspace: Path):
    _build(workspace, "--no-images")
    data = _data_of(workspace / "outputs" / "screening_report.html")
    assert data["imgs"] == {}
    ev = data["docs"]["M001_doc"]["J"]["IN-2-1"]["证据"][0]
    assert "图" not in ev
    assert ev["原件"][0]["链接"].endswith("M001_page_001.jpg")


def test_criteria_report_embeds_parsed_json_verbatim(workspace: Path):
    _build(workspace)
    data = _data_of(workspace / "outputs" / "criteria_report.html")
    original = json.loads((workspace / "outputs" / "criteria_parsed.json").read_text(encoding="utf-8"))
    assert data == original  # 不允许多包一层（历史故障：页面空白）


def test_flat_judgments_structure_supported(workspace: Path):
    flat = {
        "patient_id": "M002",
        "patient_name": "李四",
        "judgments": {"IN-2-1": {"结论": "不符合", "理由": "年龄 16 岁", "证据": []}},
    }
    path = workspace / "outputs" / "judgments_M001.json"
    path.write_text(json.dumps(flat, ensure_ascii=False), encoding="utf-8")
    assert _build(workspace) == 0
    data = _data_of(workspace / "outputs" / "screening_report.html")
    doc = next(iter(data["docs"].values()))
    assert doc["J"]["IN-2-1"]["结论"] == "不符合"
    assert doc["cnt"]["不符合"] == 1


def test_verify_rejects_handwritten_html(workspace: Path):
    """手写 HTML 覆盖产出时，校验必须失败（本次故障的直接回归点）。"""
    _build(workspace)
    handwritten = "<!DOCTYPE html><html><head><style>body{background:#f5f7fb}.badge.maybe{background:#fef3c7}</style></head><body><table><tr><td>IN-2-1</td><td>符合</td></tr></table></body></html>"
    (workspace / "outputs" / "screening_report.html").write_text(handwritten, encoding="utf-8")
    assert builder.main(["--verify", "--out-dir", str(workspace / "outputs")]) == 1


def test_empty_criteria_exits_with_error(workspace: Path):
    path = workspace / "outputs" / "criteria_parsed.json"
    path.write_text(json.dumps({"四分类": {}}, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        _build(workspace)
    assert exc.value.code == 1


def test_script_close_tag_in_data_is_escaped(workspace: Path):
    path = workspace / "outputs" / "judgments_M001.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["documents"]["M001_doc"]["judgments"]["IN-2-1"]["reason"] = "含 </script> 的理由"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    assert _build(workspace) == 0
    html = (workspace / "outputs" / "screening_report.html").read_text(encoding="utf-8")
    body = builder.DATA_BLOCK_RE.search(html).group(2)
    assert "</script>" not in body
    data = _data_of(workspace / "outputs" / "screening_report.html")
    assert data["docs"]["M001_doc"]["J"]["IN-2-1"]["理由"] == "含 </script> 的理由"
