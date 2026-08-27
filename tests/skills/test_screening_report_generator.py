"""screening-report-generator 构建器回归测试。

保证报告始终由技能模板渲染（历史故障：代理手写 HTML/CSS，产出样式与模板完全不同）。
"""

import base64
import importlib.util
import json
import shutil
import struct
import subprocess
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
            "入选_可从病例获取": {
                "IN-2-1": {
                    "条件ID": "IN-2-1",
                    "来源标准": "入选标准 第2条",
                    "原文": "筛选时年龄≥18周岁的男性患者。",
                    "子条件": "筛选时年龄≥18周岁",
                    "逻辑关系": "AND",
                    "可从病例获取": True,
                    "转化条件": {"匹配字段": ["年龄"], "运算符": "≥", "阈值": 18, "单位": "周岁"},
                }
            },
            "入选_不可从病例获取": {},
            "排除_可从病例获取": {
                "EX-3": {
                    "条件ID": "EX-3",
                    "来源标准": "排除标准 第3条",
                    "原文": "存在活动性感染。",
                    "子条件": "存在活动性感染",
                    "可从病例获取": True,
                }
            },
            "排除_不可从病例获取": {},
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
                # 主条件组级汇总：由 /eligibility-judgment 的 judge_pack.py merge-judgments 产出，
                # 报告只渲染、不重算（折叠口径的唯一真相源在判定侧）。
                "criteria_rollup": {
                    "IN-2": {
                        "conclusion": "符合",
                        "track": "IN",
                        "rule": "单条",
                        "members": ["IN-2-1"],
                        "decided_by": ["IN-2-1"],
                        "counts": {"符合": 1, "不符合": 0, "存疑": 0, "无法判断": 0},
                    },
                    "EX-3": {
                        "conclusion": "存疑",
                        "track": "EX",
                        "rule": "单条",
                        "members": ["EX-3"],
                        "decided_by": ["EX-3"],
                        "counts": {"符合": 0, "不符合": 0, "存疑": 1, "无法判断": 0},
                    },
                },
                "rollup_summary": {"符合": 1, "不符合": 0, "存疑": 1, "无法判断": 0},
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


def test_unified_top_level_judgments_are_first_class(workspace: Path, capsys):
    """统一判定产物(顶层 judgments,无 documents)为第一公民输入:doc 键取 patient_id、
    顶层 criteria_rollup 直接作主条件结论,无任何 fallback/降级告警。"""
    path = workspace / "outputs" / "judgments_M001.json"
    payload = {
        "patient_id": "M003",
        "patient_name": "王五",
        "judgment_date": "2026-08-25",
        "judgments": {
            "IN-2-1": {"conclusion": "符合", "reason": "年龄 40 岁。", "evidence": []},
            "EX-3": {"conclusion": "存疑", "reason": "未查见明确活动性感染。", "evidence": []},
        },
        "summary": {"符合": 1, "不符合": 0, "存疑": 1, "无法判断": 0},
        "criteria_rollup": {
            "IN-2": {
                "conclusion": "符合", "rule": "单条", "members": ["IN-2-1"], "decided_by": ["IN-2-1"],
                "counts": {"符合": 1, "不符合": 0, "存疑": 0, "无法判断": 0},
            },
            "EX-3": {
                "conclusion": "存疑", "rule": "单条", "members": ["EX-3"], "decided_by": ["EX-3"],
                "counts": {"符合": 0, "不符合": 0, "存疑": 1, "无法判断": 0},
            },
        },
        "rollup_summary": {"符合": 1, "不符合": 0, "存疑": 1, "无法判断": 0},
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    assert _build(workspace) == 0
    err = capsys.readouterr().err
    assert "缺 `criteria_rollup`" not in err, "第一公民路径不得触发降级告警"
    assert builder.main(["--verify", "--out-dir", str(workspace / "outputs")]) == 0
    data = _data_of(workspace / "outputs" / "screening_report.html")
    (doc_key, doc), = data["docs"].items()
    assert doc_key == "M003", "第一公民路径 doc 键取 patient_id"
    assert "王五" in doc["标签"], "标签取 patient_name"
    assert doc["J"]["IN-2-1"]["结论"] == "符合"
    assert doc["R"]["IN-2"]["结论"] == "符合"
    assert doc["R"]["EX-3"]["结论"] == "存疑"
    assert data["merged"]["IN-2-1"]["结论"] == "符合"
    assert data["merged"]["EX-3"]["结论"] == "存疑"


def test_unified_top_level_judgments_are_first_class(workspace: Path, capsys):
    """统一判定产物(顶层 judgments,无 documents)为第一公民输入:doc 键取 patient_id、
    顶层 criteria_rollup 直接作主条件结论,无任何 fallback/降级告警。"""
    path = workspace / "outputs" / "judgments_M001.json"
    payload = {
        "patient_id": "M003",
        "patient_name": "王五",
        "judgment_date": "2026-08-25",
        "judgments": {
            "IN-2-1": {"conclusion": "符合", "reason": "年龄 40 岁。", "evidence": []},
            "EX-3": {"conclusion": "存疑", "reason": "未查见明确活动性感染。", "evidence": []},
        },
        "summary": {"符合": 1, "不符合": 0, "存疑": 1, "无法判断": 0},
        "criteria_rollup": {
            "IN-2": {
                "conclusion": "符合", "rule": "单条", "members": ["IN-2-1"], "decided_by": ["IN-2-1"],
                "counts": {"符合": 1, "不符合": 0, "存疑": 0, "无法判断": 0},
            },
            "EX-3": {
                "conclusion": "存疑", "rule": "单条", "members": ["EX-3"], "decided_by": ["EX-3"],
                "counts": {"符合": 0, "不符合": 0, "存疑": 1, "无法判断": 0},
            },
        },
        "rollup_summary": {"符合": 1, "不符合": 0, "存疑": 1, "无法判断": 0},
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    assert _build(workspace) == 0
    err = capsys.readouterr().err
    assert "缺 `criteria_rollup`" not in err, "第一公民路径不得触发降级告警"
    assert "缺 documents" not in err
    assert builder.main(["--verify", "--out-dir", str(workspace / "outputs")]) == 0
    data = _data_of(workspace / "outputs" / "screening_report.html")
    (doc_key, doc), = data["docs"].items()
    assert doc_key == "M003", "第一公民路径 doc 键取 patient_id"
    assert "王五" in doc["标签"], "标签取 patient_name"
    assert doc["J"]["IN-2-1"]["结论"] == "符合"
    assert doc["R"]["IN-2"]["结论"] == "符合"
    assert doc["R"]["EX-3"]["结论"] == "存疑"
    assert data["merged"]["IN-2-1"]["结论"] == "符合"
    assert data["merged"]["EX-3"]["结论"] == "存疑"


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


# --- 主条件层（两级表格的父行） --------------------------------------------
#
# 报告此前只有子条件行：IN-10 拆成 7 条就是 7 行独立结论，读者拿不到「第 10 条整体达标吗」。
# 主条件结论来自判定产物的 criteria_rollup，报告只渲染、不重算。


def _strip_rollup(workspace: Path) -> dict:
    """把判定文件退回到「没有 criteria_rollup」的老形态（降级路径的输入）。"""
    path = workspace / "outputs" / "judgments_M001.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    doc = payload["documents"]["M001_doc"]
    doc.pop("criteria_rollup", None)
    doc.pop("rollup_summary", None)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return payload


def _add_sibling_sub_condition(workspace: Path) -> None:
    """给 IN-2 再加一个子条件，让「主条件口径」与「子条件口径」的计数不再相同。

    真实会话 9a83ccc9：30 条入排条款拆成 64 个子条件，头部按子条件计（64）、筛选标签按主条件计
    （30），两个数字并排出现且都没标口径，读者无法判断哪个是「实际入排条款数」。
    """
    crit_path = workspace / "outputs" / "criteria_parsed.json"
    criteria = json.loads(crit_path.read_text(encoding="utf-8"))
    criteria["四分类"]["入选_可从病例获取"]["IN-2-2"] = {
        "条件ID": "IN-2-2",
        "来源标准": "入选标准 第2条",
        "原文": "筛选时年龄≥18周岁的男性患者。",
        "子条件": "性别为男性",
        "逻辑关系": "AND",
        "可从病例获取": True,
    }
    crit_path.write_text(json.dumps(criteria, ensure_ascii=False), encoding="utf-8")

    jud_path = workspace / "outputs" / "judgments_M001.json"
    payload = json.loads(jud_path.read_text(encoding="utf-8"))
    doc = payload["documents"]["M001_doc"]
    doc["judgments"]["IN-2-2"] = {"conclusion": "符合", "reason": "病历记载性别男。", "evidence": []}
    doc["summary"] = {"符合": 2, "不符合": 0, "存疑": 1, "无法判断": 0}
    doc["criteria_rollup"]["IN-2"] = {
        "conclusion": "符合",
        "track": "IN",
        "rule": "AND",
        "members": ["IN-2-1", "IN-2-2"],
        "decided_by": ["IN-2-1", "IN-2-2"],
        "counts": {"符合": 2, "不符合": 0, "存疑": 0, "无法判断": 0},
    }
    jud_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _add_second_document(workspace: Path) -> None:
    """加第二份物料：IN-2-1 判「不符合」、EX-3 判「无法判断」，制造跨物料结论冲突。

    真实判定产物的 documents 以物料名为键（如「筛选期病历」/「筛选期检查」），
    且不带 doc_label/source_file —— 第二份物料刻意保持该形态，同时覆盖
    「标签缺省回退物料名」与「跨物料折叠」两条路径。
    """
    jud_path = workspace / "outputs" / "judgments_M001.json"
    payload = json.loads(jud_path.read_text(encoding="utf-8"))
    payload["documents"]["M002_doc"] = {
        "judgments": {
            "IN-2-1": {
                "conclusion": "不符合",
                "reason": "第二份物料记载年龄 16 岁。",
                "evidence": [
                    {
                        "source": "M002",
                        "page": 3,
                        "quote": "年龄：16岁",
                        "hit": True,
                        "screenshot_ref": "images/M001/M001_page_001.jpg",
                    }
                ],
            },
            "EX-3": {"conclusion": "无法判断", "reason": "第二份物料未见感染相关记录。", "evidence": []},
        },
        "summary": {"符合": 0, "不符合": 1, "存疑": 0, "无法判断": 1},
        "criteria_rollup": {
            "IN-2": {
                "conclusion": "不符合",
                "rule": "单条",
                "members": ["IN-2-1"],
                "decided_by": ["IN-2-1"],
                "counts": {"符合": 0, "不符合": 1, "存疑": 0, "无法判断": 0},
            },
            "EX-3": {
                "conclusion": "无法判断",
                "rule": "单条",
                "members": ["EX-3"],
                "decided_by": ["EX-3"],
                "counts": {"符合": 0, "不符合": 0, "存疑": 0, "无法判断": 1},
            },
        },
        "rollup_summary": {"符合": 0, "不符合": 1, "存疑": 0, "无法判断": 1},
    }
    jud_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


@pytest.mark.parametrize(
    "cid,expected",
    [("IN-2-1", "IN-2"), ("IN-10-12", "IN-10"), ("EX-3", "EX-3"), ("备注", "备注")],
)
def test_parent_of(cid: str, expected: str):
    assert builder.parent_of(cid) == expected


def test_parents_index_is_built_from_criteria_side(workspace: Path):
    _build(workspace)
    data = _data_of(workspace / "outputs" / "screening_report.html")
    parents = data["parents"]
    assert [p["pid"] for p in parents] == ["IN-2", "EX-3"]  # IN 先 EX 后
    assert parents[0]["members"] == ["IN-2-1"]
    assert parents[0]["inc"] is True
    assert parents[1]["inc"] is False
    # 描述取自 criteria_parsed.json 的 描述索引
    assert parents[0]["desc"] == "年龄"
    assert parents[1]["desc"] == "活动性感染"


def test_parent_desc_falls_back_to_first_sub_condition(workspace: Path):
    path = workspace / "outputs" / "criteria_parsed.json"
    criteria = json.loads(path.read_text(encoding="utf-8"))
    criteria.pop("描述索引")
    path.write_text(json.dumps(criteria, ensure_ascii=False), encoding="utf-8")
    _build(workspace)
    data = _data_of(workspace / "outputs" / "screening_report.html")
    assert data["parents"][0]["desc"] == "筛选时年龄≥18周岁"


def test_rollup_conclusions_are_passed_through_verbatim(workspace: Path):
    _build(workspace)
    data = _data_of(workspace / "outputs" / "screening_report.html")
    doc = data["docs"]["M001_doc"]
    assert doc["R"]["IN-2"]["结论"] == "符合"
    assert doc["R"]["IN-2"]["依据"] == ["IN-2-1"]
    assert doc["R"]["EX-3"]["结论"] == "存疑"
    assert doc["R"]["EX-3"]["计数"] == {"符合": 0, "不符合": 0, "存疑": 1, "无法判断": 0}
    # 主条件口径计数（供筛选标签用），与子条件口径 cnt 并存
    assert doc["rcnt"] == {"符合": 1, "不符合": 0, "存疑": 1, "无法判断": 0}
    assert builder.main(["--verify", "--out-dir", str(workspace / "outputs")]) == 0


def test_missing_rollup_degrades_to_not_rolled_up_without_failing(workspace: Path, capsys):
    """老判定文件（无 criteria_rollup）仍要能出报告，但必须出声、且不自己折叠。"""
    _strip_rollup(workspace)
    assert _build(workspace) == 0
    err = capsys.readouterr().err
    assert "criteria_rollup" in err and "merge-judgments" in err

    data = _data_of(workspace / "outputs" / "screening_report.html")
    doc = data["docs"]["M001_doc"]
    assert doc["R"]["IN-2"]["结论"] == builder.NOT_ROLLED_UP
    assert doc["R"]["EX-3"]["结论"] == builder.NOT_ROLLED_UP
    # 降级只给子条件计数，绝不猜主条件结论
    assert doc["R"]["IN-2"]["计数"] == {"符合": 1, "不符合": 0, "存疑": 0, "无法判断": 0}
    assert data["parents"]  # 父行仍然渲染


def test_verify_flags_not_rolled_up_as_advisory_not_failure(workspace: Path, capsys):
    _strip_rollup(workspace)
    _build(workspace)
    capsys.readouterr()
    assert builder.main(["--verify", "--out-dir", str(workspace / "outputs")]) == 0
    out = capsys.readouterr().out
    assert "⚠️" in out and builder.NOT_ROLLED_UP in out
    assert "❌" not in out


def test_verify_rejects_illegal_rollup_conclusion(workspace: Path):
    path = workspace / "outputs" / "judgments_M001.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["documents"]["M001_doc"]["criteria_rollup"]["IN-2"]["conclusion"] = "大概符合"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    _build(workspace)
    data = _data_of(workspace / "outputs" / "screening_report.html")
    # 非法枚举被归一化为「未汇总」而非原样带进报告
    assert data["docs"]["M001_doc"]["R"]["IN-2"]["结论"] == builder.NOT_ROLLED_UP


def test_verify_fails_when_parents_missing(workspace: Path, capsys):
    """手工删掉 parents（或用旧构建器产出的报告）→ 校验必须失败。"""
    _build(workspace)
    report = workspace / "outputs" / "screening_report.html"
    html = report.read_text(encoding="utf-8")
    data = builder.extract_data(html)
    data["parents"] = []
    report.write_text(builder.inject_data(html, data), encoding="utf-8")
    assert builder.main(["--verify", "--out-dir", str(workspace / "outputs")]) == 1
    assert "parents（主条件）非空" in capsys.readouterr().out


def test_verify_fails_when_parent_members_are_not_in_ids(workspace: Path):
    _build(workspace)
    report = workspace / "outputs" / "screening_report.html"
    html = report.read_text(encoding="utf-8")
    data = builder.extract_data(html)
    data["parents"][0]["members"].append("IN-99-1")
    report.write_text(builder.inject_data(html, data), encoding="utf-8")
    assert builder.main(["--verify", "--out-dir", str(workspace / "outputs")]) == 1


def test_judgment_side_rollup_warnings_are_surfaced(workspace: Path, capsys):
    path = workspace / "outputs" / "judgments_M001.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["rollup_warnings"] = ["M001_doc: 或组 IN-5-OR（IN-5）缺 `或组语义`，已按轨前缀 IN 推断"]
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    assert _build(workspace) == 0
    assert "或组语义" in capsys.readouterr().err


# --- 模板 JS 渲染回归（用 node + 最小 DOM stub 真跑一遍内联脚本）-------------
#
# 数据注入对了、模板 JS 却渲染不出来，同样是「报告打开是空的」——而此前没有任何测试
# 执行过模板里的 JS。这里把真实产出的内联脚本喂给 node，断言两级表格的行为。

RENDER_HARNESS = r"""
const fs = require('fs');
const html = fs.readFileSync(process.argv[2], 'utf8');
const dataMatch = html.match(/<script id="data" type="application\/json">([\s\S]*?)<\/script>/);
const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]);
const code = scripts[scripts.length - 1];

const els = {};
function El(id) {
  return {
    id,
    textContent: id === 'data' ? dataMatch[1].replace(/<\\\//g, '</') : '',
    innerHTML: '', value: '', dataset: {},
    classList: {add() {}, remove() {}, toggle() {}},
    _handlers: {},
    addEventListener(ev, fn) { this._handlers[ev] = fn; },
    querySelectorAll() { return {forEach() {}}; },
  };
}
const document = {
  getElementById(id) { return (els[id] = els[id] || El(id)); },
  addEventListener() {},
};
new Function('document', code)(document);

const pids = h => [...h.matchAll(/data-pid="([^"]+)"/g)].map(m => m[1]);
const subs = h => [...h.matchAll(/<td class="id-cell">([A-Z]+-[\d-]+)/g)].map(m => m[1]);
const out = {};
out.parents = pids(els['tbody'].innerHTML);
// 主条件行：pnum 之后紧跟一枚折叠结论徽章（djudge-v）
out.verdicts = [...els['tbody'].innerHTML.matchAll(
  /<span class="pnum">([^<]+)<\/span>\s*<span class="djudge-v (\w+)">([^<]+)</g
)].map(m => ({pid: m[1], cls: m[2], verdict: m[3]}));
out.subrowsCollapsed = subs(els['tbody'].innerHTML);
out.tabs = els['tabs'].innerHTML.replace(/<[^>]+>/g, '|');
out.summaryBar = els['summaryBar'].innerHTML.replace(/<[^>]+>/g, '').replace(/\s+/g, ' ');

els['tbody']._handlers['click']({target: {closest: s => s === 'tr.prow' ? {dataset: {pid: process.argv[3]}} : null}});
out.subrowsAfterToggle = subs(els['tbody'].innerHTML);
// 子条件行：verdict-cell 内一枚折叠结论徽章
out.subVerdicts = [...els['tbody'].innerHTML.matchAll(
  /<td class="id-cell">([A-Z]+-[\d-]+)[\s\S]*?<td class="verdict-cell"><span class="djudge-v (\w+)">([^<]+)</g
)].map(m => ({id: m[1], cls: m[2], verdict: m[3]}));
out.evidenceRendered = els['tbody'].innerHTML.includes('ev-card');
out.evGroups = (els['tbody'].innerHTML.match(/ev-group-doc"/g) || []).length;

els['q'].value = process.argv[4];
els['q']._handlers['input']();
out.searchParents = pids(els['tbody'].innerHTML);
out.searchSubrows = subs(els['tbody'].innerHTML);

els['q'].value = '';
els['tabs']._handlers['click']({target: {closest: () => ({dataset: {f: process.argv[5]}})}});
out.filterParents = pids(els['tbody'].innerHTML);
console.log(JSON.stringify(out));
"""


def _render(workspace: Path, toggle_pid: str, query: str, filter_value: str) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("未安装 node，跳过模板 JS 渲染回归")
    harness = workspace / "render_harness.js"
    harness.write_text(RENDER_HARNESS, encoding="utf-8")
    proc = subprocess.run(
        [node, str(harness), str(workspace / "outputs" / "screening_report.html"), toggle_pid, query, filter_value],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"模板 JS 执行失败：\n{proc.stderr}"
    return json.loads(proc.stdout)


def test_template_renders_two_level_table(workspace: Path):
    _build(workspace)
    r = _render(workspace, "IN-2", "83", "存疑")
    # 默认只渲染主条件行，子条件折叠
    assert r["parents"] == ["IN-2", "EX-3"]
    assert r["subrowsCollapsed"] == []
    # 主条件结论徽标取自 criteria_rollup，颜色类与结论对应
    assert r["verdicts"] == [
        {"pid": "IN-2", "cls": "ok", "verdict": "符合"},
        {"pid": "EX-3", "cls": "may", "verdict": "存疑"},
    ]
    # 筛选标签按主条件计数（2 条主条件，不是 2 条子条件的巧合——见下一个断言）
    assert "全部(2)" in r["tabs"] and "符合(1)" in r["tabs"] and "存疑(1)" in r["tabs"]


def test_template_expands_parent_on_click(workspace: Path):
    _build(workspace)
    r = _render(workspace, "IN-2", "", "all")
    assert r["subrowsAfterToggle"] == ["IN-2-1"]
    assert r["evidenceRendered"] is True  # 证据卡片渲染未受两级改造影响
    # 子条件行判定 = 一枚折叠结论徽章
    assert r["subVerdicts"] == [{"id": "IN-2-1", "cls": "ok", "verdict": "符合"}]


def test_template_search_auto_expands_matching_parent(workspace: Path):
    _build(workspace)
    r = _render(workspace, "EX-3", "83", "all")
    # 搜索命中子条件理由（"病历记载年龄 83 岁"）→ 只留该主条件并自动展开
    assert r["searchParents"] == ["IN-2"]
    assert r["searchSubrows"] == ["IN-2-1"]


def test_template_filters_by_parent_conclusion(workspace: Path):
    _build(workspace)
    assert _render(workspace, "IN-2", "", "存疑")["filterParents"] == ["EX-3"]
    assert _render(workspace, "IN-2", "", "符合")["filterParents"] == ["IN-2"]


def test_template_renders_not_rolled_up_parents(workspace: Path):
    """老判定文件（无 criteria_rollup）时父行仍渲染，结论显示「未汇总」并单列一个筛选档。"""
    _strip_rollup(workspace)
    _build(workspace)
    r = _render(workspace, "IN-2", "", "all")
    assert r["parents"] == ["IN-2", "EX-3"]
    assert [v["verdict"] for v in r["verdicts"]] == [builder.NOT_ROLLED_UP, builder.NOT_ROLLED_UP]
    assert f"{builder.NOT_ROLLED_UP}(2)" in r["tabs"]


# --- 头部统计与筛选标签必须同口径（会话 9a83ccc9 issue 3）--------------------


def test_summary_bar_counts_folded_verdicts_not_materials(workspace: Path):
    """统一汇总条按**折叠后的子条件结论**计数，判定数 = 子条件数（不是 子条件×物料）。

    故障：30 条入排条款拆成 64 个子条件后，头部显示 38/1/9/16（子条件），筛选标签显示
    16/1/5/8（主条件），两个口径并排且都没标注，读者无法判断哪个对应「实际入排条款数」。
    跨物料合并后口径统一为：每个条件一个折叠结论（不符合>符合>存疑>无法判断）。
    """
    _add_sibling_sub_condition(workspace)
    _build(workspace)
    data = _data_of(workspace / "outputs" / "screening_report.html")
    doc = data["docs"]["M001_doc"]
    assert doc["cnt"] == {"符合": 2, "不符合": 0, "存疑": 1, "无法判断": 0}  # 子条件口径 3 条
    assert doc["rcnt"] == {"符合": 1, "不符合": 0, "存疑": 1, "无法判断": 0}  # 主条件口径 2 条

    r = _render(workspace, "IN-2", "", "all")
    # 汇总条：判定数 = 子条件数（3 条，不是 3×1 或 3×N 物料），四类计数按折叠结论
    assert "主条件 2" in r["summaryBar"], r["summaryBar"]
    assert "子条件 3" in r["summaryBar"], r["summaryBar"]
    assert "判定 3" in r["summaryBar"], r["summaryBar"]
    assert "符合 2" in r["summaryBar"], r["summaryBar"]  # IN-2-1/IN-2-2 符合、EX-3 存疑
    assert "存疑 1" in r["summaryBar"], r["summaryBar"]
    # 不再出现按物料独立计数细目（每份患者资料不单独统计）
    assert "M001_doc" not in r["summaryBar"], r["summaryBar"]
    # 筛选标签同主条件口径
    assert "全部(2)" in r["tabs"] and "符合(1)" in r["tabs"]


def test_summary_bar_unrolled_parents_do_not_inflate_counts(workspace: Path):
    """缺 criteria_rollup 时主条件行显示「未汇总」，但不新增计数档、不改变判定口径。"""
    _strip_rollup(workspace)
    _build(workspace)
    r = _render(workspace, "IN-2", "", "all")
    assert "判定 2" in r["summaryBar"], r["summaryBar"]
    assert "符合 1" in r["summaryBar"], r["summaryBar"]  # IN-2-1 符合、EX-3 存疑（子条件口径）
    assert "存疑 1" in r["summaryBar"], r["summaryBar"]
    assert "未汇总(2)" in r["tabs"]  # 筛选标签仍保留「未汇总」档


def test_description_index_keyed_by_sub_condition_is_warned(workspace: Path, capsys):
    """`描述索引` 被写成按子条件ID 索引时必须出声。

    故障：会话 9a83ccc9 的 描述索引 有 64 个键但全是子条件ID，主条件行因此回退成
    「第一个子条件的文本」——`IN-2` 显示「年龄 ≥ 18 周岁」，静默丢掉了 ≤70 岁的上限。
    """
    path = workspace / "outputs" / "criteria_parsed.json"
    criteria = json.loads(path.read_text(encoding="utf-8"))
    criteria["描述索引"] = {"IN-2-1": "年龄 ≥ 18 周岁", "EX-3": "活动性感染"}
    path.write_text(json.dumps(criteria, ensure_ascii=False), encoding="utf-8")
    assert _build(workspace) == 0
    err = capsys.readouterr().err
    assert "描述索引" in err and "IN-2" in err


# --- 跨物料合并：一条条件一个折叠结论（不符合>符合>存疑>无法判断）----------------
#
# 同一患者的多份病历物料是共享证据材料，不再按物料各自出一套判定。多份物料有证据
# 就都匹配、全部展示，但同一条条件的结论按优先级折叠为唯一值，不允许「符合 + 不符合」
# 等矛盾结论共存。


def test_fold_conclusion_priority():
    fold = builder.fold_conclusion
    assert fold(["符合", "无法判断"]) == "符合"  # 符合 > 无法判断
    assert fold(["不符合", "符合"]) == "不符合"  # 不符合 > 符合
    assert fold(["存疑", "无法判断"]) == "存疑"  # 存疑 > 无法判断
    assert fold(["符合", "存疑"]) == "符合"  # 符合 > 存疑
    assert fold(["不符合", "存疑"]) == "不符合"  # 不符合 > 存疑
    assert fold(["不符合", "符合", "存疑", "无法判断"]) == "不符合"  # 全量：不符合胜出
    assert fold(["无法判断"]) == "无法判断"
    assert fold([]) == "无法判断"


def test_merged_folds_conclusions_across_documents(workspace: Path):
    _add_second_document(workspace)
    assert _build(workspace) == 0
    data = _data_of(workspace / "outputs" / "screening_report.html")
    merged = data["merged"]
    # 子条件：不符合 > 符合
    assert merged["IN-2-1"]["结论"] == "不符合"
    assert merged["IN-2-1"]["判定"] == {"M001_doc": "符合", "M002_doc": "不符合"}
    # 子条件：存疑 > 无法判断
    assert merged["EX-3"]["结论"] == "存疑"
    assert merged["EX-3"]["判定"] == {"M001_doc": "存疑", "M002_doc": "无法判断"}
    # 主条件结论同优先级折叠
    parents = {p["pid"]: p for p in data["parents"]}
    assert parents["IN-2"]["结论"] == "不符合"
    assert parents["EX-3"]["结论"] == "存疑"
    assert parents["IN-2"]["规则"] == "单条"  # 规则沿用判定侧 rollup，不重算


def test_doc_label_defaults_to_doc_key(workspace: Path):
    """真实判定产物 documents 无 doc_label —— 标签必须回退物料名，徽章/证据分组才能区分物料。"""
    _add_second_document(workspace)
    assert _build(workspace) == 0
    data = _data_of(workspace / "outputs" / "screening_report.html")
    # fixture 第一份物料有 doc_label；第二份没有 → 回退 doc_key（M002_doc）
    assert data["docs"]["M002_doc"]["标签"] == "M002_doc（M001）"
    assert data["docs"]["M002_doc"]["名"] == "M002_doc"
    assert data["docs"]["M001_doc"]["标签"] == "既往病历（M001）"


def test_parent_conclusion_unrolled_when_all_materials_missing_rollup(workspace: Path):
    """全部物料的 criteria_rollup 缺失 → 主条件结论「未汇总」，绝不自己折叠。"""
    _strip_rollup(workspace)
    assert _build(workspace) == 0
    data = _data_of(workspace / "outputs" / "screening_report.html")
    parents = {p["pid"]: p for p in data["parents"]}
    assert parents["IN-2"]["结论"] == builder.NOT_ROLLED_UP
    assert parents["EX-3"]["结论"] == builder.NOT_ROLLED_UP


def test_verify_rejects_merged_contradicting_judgments(workspace: Path):
    """数据块的折叠结论若与各物料判定折叠结果矛盾（手工篡改），校验必须失败。"""
    _add_second_document(workspace)
    assert _build(workspace) == 0
    report = workspace / "outputs" / "screening_report.html"
    html = report.read_text(encoding="utf-8")
    data = builder.extract_data(html)
    data["merged"]["IN-2-1"]["结论"] = "符合"  # 判定里有「不符合」，折叠应为「不符合」
    report.write_text(builder.inject_data(html, data), encoding="utf-8")
    assert builder.main(["--verify", "--out-dir", str(workspace / "outputs")]) == 1


def test_template_renders_single_folded_verdict_per_row(workspace: Path):
    """多物料合并视图：每条条件一枚折叠结论徽章，证据/理由合并全部物料。"""
    _add_second_document(workspace)
    assert _build(workspace) == 0
    r = _render(workspace, "IN-2", "", "all")
    # 主条件行：一枚徽章，结论为跨物料折叠结果
    assert r["verdicts"] == [
        {"pid": "IN-2", "cls": "no", "verdict": "不符合"},
        {"pid": "EX-3", "cls": "may", "verdict": "存疑"},
    ]
    # 子条件行：一枚徽章（不再按物料各显示一枚）
    assert r["subVerdicts"] == [{"id": "IN-2-1", "cls": "no", "verdict": "不符合"}]
    # 证据按物料分组合并：两份物料各有证据 → 2 个分组
    assert r["evGroups"] == 2
    # 汇总条判定口径 = 子条件数（2 条），每条件一个折叠结论
    assert "判定 2" in r["summaryBar"], r["summaryBar"]
    assert "不符合 1" in r["summaryBar"], r["summaryBar"]
    assert "存疑 1" in r["summaryBar"], r["summaryBar"]
