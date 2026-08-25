"""render_judge_prompt 回归测试：把委派模板机械渲染成每批一个 prompt 文件。

## 这个脚本在解决什么矛盾

模板必须**逐字**到达判定子代理（会话 `9a83ccc9`：主代理把 12.7k 字符模板转述成 1.8k 字符，
`check_judgment_structure.py` 整条命令消失，子代理自创 schema，产物无法进入合并），
但让主代理亲手抄一遍是按 token 计费的（会话 `247a535f`：三路判定派发 143.6 秒 /
15,265 输出 token，全会话最慢的一次 lead 调用，只为吐出三份固定模板）。

机械渲染同时满足两者：模板原文不经模型之手，占位符按白名单精确替换。

## 本文件锁定什么

1. **白名单精确替换**：`{id}` / `{SHARD}` / `{BATCH}` 等被替换；模板里的**字面**花括号
   （`{"op": "get"}` / `{conclusion,reason,evidence}` / `{符合:N, …}`）必须原样保留 ——
   这也是不能用 `str.format` 的原因。
2. **渲染即自检**：四条闸命令与 schema 指针缺失、占位符残留、参数非法 → `exit 2`
   且**零产物**（不留半套 prompt，否则主代理会派出一个残缺的任务）。
3. **模板锚点**：按内容锚点定位模板块而非块序号——文档增删段落时块序号会漂移，
   静默渲染出半个模板比直接失败危险得多。
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_DIR = REPO_ROOT / "skills" / "custom" / "eligibility-judgment"
SCRIPT_PATH = SKILL_DIR / "scripts" / "render_judge_prompt.py"
TEMPLATE_PATH = SKILL_DIR / "references" / "judge-delegation.md"

if not SCRIPT_PATH.exists():  # skills/custom 为 gitignore 目录，清空检出时跳过
    pytest.skip("eligibility-judgment 技能未安装", allow_module_level=True)


def _load_module():
    spec = importlib.util.spec_from_file_location("render_judge_prompt", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


render = _load_module()

DOC_KEYS = [
    "筛选期病历=/mnt/user-data/workspace/patients/P001/ocr/筛选期病历/ocr_records.md",
    "筛选期检查=/mnt/user-data/workspace/patients/P001/ocr/筛选期检查/ocr_records.md",
]

BATCH_PLAN = {
    "patient_id": "P001",
    "track": "IN",
    "batch_size": 12,
    "total_conditions": 28,
    "batch_count": 3,
    "batches": [
        {"batch": 1, "condition_ids": ["IN-1", "IN-2-1", "IN-2-2"], "count": 3},
        {"batch": 2, "condition_ids": ["IN-7-3", "IN-8"], "count": 2},
        {"batch": 3, "condition_ids": ["IN-11-1"], "count": 1},
    ],
}


@pytest.fixture
def plan_file(tmp_path):
    path = tmp_path / "judge_batches_P001_IN.json"
    path.write_text(json.dumps(BATCH_PLAN, ensure_ascii=False), encoding="utf-8")
    return path


def _run(plan_file, out_dir, *, track="IN", date="2026-08-18", doc_keys=None, extra=None, template=None):
    argv = [
        "--batches",
        str(plan_file),
        "--patient",
        "P001",
        "--track",
        track,
        "--judgment-date",
        date,
        "--out-dir",
        str(out_dir),
        "--template",
        str(template or TEMPLATE_PATH),
    ]
    for dk in DOC_KEYS if doc_keys is None else doc_keys:
        argv += ["--doc-key", dk]
    argv += extra or []
    return render.main(argv)


class TestPlaceholderSubstitution:
    def test_whitelisted_placeholders_are_replaced(self, plan_file, tmp_path):
        assert _run(plan_file, tmp_path / "out") == 0
        text = (tmp_path / "out" / "judge_prompt_P001_IN_b1.md").read_text(encoding="utf-8")

        assert "P001" in text
        assert "入选标准" in text
        assert "第 1/3 批" in text
        assert "IN-1 IN-2-1 IN-2-2" in text
        assert "2026-08-18" in text
        for leftover in ("{id}", "{SHARD}", "{BATCH}", "{分片名}", "{EVIDENCE_SOURCES}", "{JUDGMENT_DATE}", "{BATCH_IDS}", "{BATCH_COUNT}"):
            assert leftover not in text, f"占位符 {leftover} 未替换，子代理会拿到字面量"

    def test_literal_braces_in_the_rules_are_preserved(self, plan_file, tmp_path):
        """规则正文里的花括号不是占位符，替换掉会改变规则含义。"""
        assert _run(plan_file, tmp_path / "out") == 0
        text = (tmp_path / "out" / "judge_prompt_P001_IN_b1.md").read_text(encoding="utf-8")

        for literal in ('{"op": "get"}', "{conclusion,reason,evidence}", "{符合:N, 不符合:N, 存疑:N, 无法判断:N}", "{source, page, screenshot_ref, quote}"):
            assert literal in text, f"字面花括号 {literal} 被破坏"

    def test_each_batch_gets_its_own_ids_and_number(self, plan_file, tmp_path):
        assert _run(plan_file, tmp_path / "out") == 0
        out = tmp_path / "out"

        b2 = (out / "judge_prompt_P001_IN_b2.md").read_text(encoding="utf-8")
        assert "第 2/3 批" in b2
        assert "IN-7-3 IN-8" in b2
        assert "IN-2-1" not in b2.split("本批只判以下条件ID")[1].split("\n")[1]
        assert "_b2.json" in b2

    def test_evidence_sources_are_rendered_with_consistent_indentation(self, plan_file, tmp_path):
        """统一证据源判定：{EVIDENCE_SOURCES} 渲染为物料来源名清单（evidence[].source 的合法取值）。"""
        assert _run(plan_file, tmp_path / "out") == 0
        text = (tmp_path / "out" / "judge_prompt_P001_IN_b1.md").read_text(encoding="utf-8")

        rows = [line for line in text.splitlines() if line.strip().startswith('- "') and "ocr_records" not in line]
        assert len(rows) == 2
        assert len({len(line) - len(line.lstrip()) for line in rows}) == 1, "各行缩进必须一致"
        assert '"筛选期病历"' in rows[0]
        assert "统一证据源" in text

    def test_actual_ocr_paths_replace_the_mode_choice_lines(self, plan_file, tmp_path):
        """模板里「整份解析 / 分页聚合」二选一说明必须换成确定路径，否则子代理会自己 glob。"""
        assert _run(plan_file, tmp_path / "out", extra=["--page-index", "/mnt/user-data/workspace/patients/P001/ocr_page_index.json"]) == 0
        text = (tmp_path / "out" / "judge_prompt_P001_IN_b1.md").read_text(encoding="utf-8")

        inputs_section = text.split("输入（只允许读")[1].split("上下文纪律")[0]
        assert "· /mnt/user-data/workspace/patients/P001/ocr/筛选期病历/ocr_records.md" in inputs_section
        assert "整份解析（单患者" not in inputs_section
        assert "ocr_page_index.json" in inputs_section

    def test_ex_track_renders_the_exclusion_name_and_gate(self, plan_file, tmp_path):
        plan = dict(BATCH_PLAN, track="EX", batches=[{"batch": 1, "condition_ids": ["EX-1-1", "EX-2"], "count": 2}])
        path = tmp_path / "ex.json"
        path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")

        assert _run(path, tmp_path / "out", track="EX") == 0
        text = (tmp_path / "out" / "judge_prompt_P001_EX_b1.md").read_text(encoding="utf-8")
        assert "排除标准" in text
        assert "criteria_judge_EX.json" in text
        assert "exclusion_direction_check.py" in text


class TestRenderSelfChecks:
    def test_all_gate_commands_and_the_schema_pointer_survive(self, plan_file, tmp_path):
        assert _run(plan_file, tmp_path / "out") == 0
        text = (tmp_path / "out" / "judge_prompt_P001_IN_b1.md").read_text(encoding="utf-8")

        for fragment in render.REQUIRED_FRAGMENTS:
            assert fragment in text, f"关键片段 {fragment} 缺失——这是 9a83ccc9 的失败形态"

    def test_a_template_missing_a_gate_is_refused(self, plan_file, tmp_path):
        """删段的模板必须显式失败，而不是渲染出一个少一条闸的 prompt。"""
        stripped = TEMPLATE_PATH.read_text(encoding="utf-8").replace("check_judgment_structure.py", "（已删）")
        bad = tmp_path / "bad-template.md"
        bad.write_text(stripped, encoding="utf-8")

        assert _run(plan_file, tmp_path / "out", template=bad) == 2
        assert not (tmp_path / "out").exists()

    def test_missing_anchor_is_refused(self, plan_file, tmp_path):
        bad = tmp_path / "no-anchor.md"
        bad.write_text("# 文档\n\n```\n没有锚点的代码块\n```\n", encoding="utf-8")

        assert _run(plan_file, tmp_path / "out", template=bad) == 2

    def test_duplicate_anchor_is_refused(self, plan_file, tmp_path):
        doubled = TEMPLATE_PATH.read_text(encoding="utf-8")
        doubled += "\n```\n请按 /eligibility-judgment 技能规则，另一个块\n```\n"
        bad = tmp_path / "two-anchors.md"
        bad.write_text(doubled, encoding="utf-8")

        assert _run(plan_file, tmp_path / "out", template=bad) == 2

    def test_leftover_placeholder_is_detected(self):
        """残留占位符闸：白名单被改小（漏掉一个占位符）时必须失败，不能把字面量发给子代理。

        构造方式：模板里放一个白名单**不覆盖**的写法。`{ SHARD }`（带空格）不会被精确替换，
        但仍会被残留检测的正则命中——正是"白名单与检测器脱节"的形态。
        所有 required 片段都补齐，确保失败来自残留闸而不是删段闸。
        """
        fragments = " ".join(render.REQUIRED_FRAGMENTS)
        with pytest.raises(render.RenderBlocked, match="残留占位符"):
            render.render_one(
                f"请按 /eligibility-judgment 技能规则 {{id}} {fragments} 轨={{SHARD}}\n未替换的：{{JUDGMENT_DATE }}{{BATCH}}",
                track="IN",
                ocr_paths_block="",
                patient="P001",
                batch=1,
                batch_count=1,
                batch_ids=["IN-1"],
                judgment_date="2026-08-18",
                doc_pairs=[("k", "/mnt/user-data/x.md")],
            )

    def test_render_one_succeeds_when_nothing_is_left_over(self):
        """对照组：同一段模板补齐后必须通过，证明上面失败的原因是残留而非别的闸。"""
        fragments = " ".join(render.REQUIRED_FRAGMENTS)
        text = render.render_one(
            f"请按 /eligibility-judgment 技能规则 {{id}} {fragments} 轨={{SHARD}} 批={{BATCH}} 日={{JUDGMENT_DATE}}",
            track="IN",
            ocr_paths_block="",
            patient="P001",
            batch=1,
            batch_count=1,
            batch_ids=["IN-1"],
            judgment_date="2026-08-18",
            doc_pairs=[("k", "/mnt/user-data/x.md")],
        )
        assert "P001" in text and "轨=IN" in text and "批=1" in text and "日=2026-08-18" in text


class TestArgumentValidation:
    def test_doc_key_is_mandatory(self, plan_file, tmp_path):
        assert _run(plan_file, tmp_path / "out", doc_keys=[]) == 2

    def test_doc_key_needs_key_equals_path(self, plan_file, tmp_path):
        assert _run(plan_file, tmp_path / "out", doc_keys=["筛选期病历"]) == 2

    def test_doc_key_path_must_be_under_user_data(self, plan_file, tmp_path):
        assert _run(plan_file, tmp_path / "out", doc_keys=["a=/etc/passwd"]) == 2

    def test_duplicate_doc_key_is_refused(self, plan_file, tmp_path):
        assert _run(plan_file, tmp_path / "out", doc_keys=["a=/mnt/user-data/x.md", "a=/mnt/user-data/y.md"]) == 2

    def test_judgment_date_must_be_iso(self, plan_file, tmp_path):
        assert _run(plan_file, tmp_path / "out", date="今天") == 2

    def test_page_index_must_be_under_user_data(self, plan_file, tmp_path):
        assert _run(plan_file, tmp_path / "out", extra=["--page-index", "/etc/index.json"]) == 2

    def test_missing_batch_plan_is_refused(self, tmp_path):
        assert _run(tmp_path / "nope.json", tmp_path / "out") == 2

    def test_malformed_batch_plan_is_refused(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("{not json", encoding="utf-8")
        assert _run(path, tmp_path / "out") == 2

    def test_empty_batches_is_refused(self, tmp_path):
        path = tmp_path / "empty.json"
        path.write_text(json.dumps({"batches": []}), encoding="utf-8")
        assert _run(path, tmp_path / "out") == 2

    def test_batch_without_condition_ids_is_refused(self, tmp_path):
        path = tmp_path / "noids.json"
        path.write_text(json.dumps({"batches": [{"batch": 1, "condition_ids": []}]}), encoding="utf-8")
        assert _run(path, tmp_path / "out") == 2


class TestNoPartialOutput:
    def test_nothing_is_written_when_any_batch_fails(self, tmp_path):
        """一批不过就整体不产出：主代理不能拿到半套 prompt 去派任务。"""
        plan = {"batches": [{"batch": 1, "condition_ids": ["IN-1"]}, {"batch": 2, "condition_ids": []}]}
        path = tmp_path / "plan.json"
        path.write_text(json.dumps(plan), encoding="utf-8")
        out = tmp_path / "out"

        assert _run(path, out) == 2
        assert not out.exists()

    def test_dispatch_manifest_lists_every_batch(self, plan_file, tmp_path, capsys):
        assert _run(plan_file, tmp_path / "out") == 0
        stdout = capsys.readouterr().out

        assert stdout.count("prompt_file=") == 3
        assert stdout.count("expected_outputs=") == 3
        assert "judgments_draft_P001_IN_b1.json" in stdout
        assert "judge_prompt_P001_IN_b3.md" in stdout
