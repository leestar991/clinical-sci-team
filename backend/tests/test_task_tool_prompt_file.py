"""``task(prompt_file=...)`` — hand the assignment over by path instead of by value.

Two rules pull in opposite directions, and this parameter is how they are satisfied at once.

**Verbatim is mandatory.** Session `9a83ccc9`: the lead re-told the ~12.7k-char
judge-delegation template as a 1.8k-char paraphrase, dropped the
`check_judgment_structure.py` gate command, and the subagent invented its own output
schema — semantically correct judgments that could not enter the merge.

**But retyping it is billed.** Session `247a535f`: the three-way judgment dispatch was the
slowest lead call of the whole run — 143.6s and 15,265 output tokens for one AIMessage
carrying three ~7.5k-char prompts of fixed template text.

`prompt_file` resolves both: a skill script renders the template mechanically and the lead
passes a path. No model ever re-emits the bytes, which is *more* faithful than hand-copying.

Validation lives at the tool boundary for the same reason `expected_outputs` does — a bad
declaration must cost zero subagent allowance — and the `/mnt/user-data/` restriction for
the same reason too: a path outside it would turn delegation into a host-file read.
"""

from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest


@pytest.fixture
def task_tool_module():
    return importlib.import_module("deerflow.tools.builtins.task_tool")


@pytest.fixture
def runtime():
    return SimpleNamespace(context={"thread_id": "t-247a535f"}, state={})


def _patch_reader(monkeypatch, files: dict[str, str]):
    """Stub ``read_current_file_content`` with an in-memory sandbox filesystem."""
    import deerflow.sandbox.tools as sandbox_tools

    def fake_read(_runtime, path: str) -> str:
        if path not in files:
            raise FileNotFoundError(path)
        return files[path]

    monkeypatch.setattr(sandbox_tools, "read_current_file_content", fake_read)


RENDERED = "请按 /eligibility-judgment 技能规则，对患者 P001 的**入选标准**逐条判定（本批 = 第 1/3 批）。"
PROMPT_PATH = "/mnt/user-data/workspace/patients/P001/prompts/judge_prompt_P001_IN_b1.md"


class TestExactlyOneSourceIsRequired:
    def test_inline_prompt_passes_through(self, task_tool_module, runtime):
        text, error = task_tool_module._resolve_prompt_source(runtime, "判定 IN 轨 b1 批", None)
        assert error is None
        assert text == "判定 IN 轨 b1 批"

    def test_neither_is_rejected(self, task_tool_module, runtime):
        text, error = task_tool_module._resolve_prompt_source(runtime, None, None)
        assert text is None
        assert "requires a prompt" in error

    def test_blank_prompt_counts_as_absent(self, task_tool_module, runtime):
        text, error = task_tool_module._resolve_prompt_source(runtime, "   \n ", None)
        assert text is None
        assert "requires a prompt" in error

    def test_both_is_rejected(self, task_tool_module, runtime, monkeypatch):
        _patch_reader(monkeypatch, {PROMPT_PATH: RENDERED})
        text, error = task_tool_module._resolve_prompt_source(runtime, "inline", PROMPT_PATH)
        assert text is None
        assert "not both" in error


class TestPromptFileIsReadAtTheToolBoundary:
    def test_contents_become_the_prompt(self, task_tool_module, runtime, monkeypatch):
        _patch_reader(monkeypatch, {PROMPT_PATH: RENDERED})
        text, error = task_tool_module._resolve_prompt_source(runtime, None, PROMPT_PATH)
        assert error is None
        assert text == RENDERED

    def test_surrounding_whitespace_in_the_path_is_tolerated(self, task_tool_module, runtime, monkeypatch):
        _patch_reader(monkeypatch, {PROMPT_PATH: RENDERED})
        text, error = task_tool_module._resolve_prompt_source(runtime, None, f"  {PROMPT_PATH}  ")
        assert error is None
        assert text == RENDERED

    def test_missing_file_names_the_path_and_the_fix(self, task_tool_module, runtime, monkeypatch):
        _patch_reader(monkeypatch, {})
        text, error = task_tool_module._resolve_prompt_source(runtime, None, PROMPT_PATH)
        assert text is None
        assert PROMPT_PATH in error
        assert "Render the assignment" in error

    def test_empty_file_is_refused(self, task_tool_module, runtime, monkeypatch):
        """An empty assignment is worse than none: the subagent would invent one."""
        _patch_reader(monkeypatch, {PROMPT_PATH: "   \n\n  "})
        text, error = task_tool_module._resolve_prompt_source(runtime, None, PROMPT_PATH)
        assert text is None
        assert "is empty" in error

    def test_read_errors_are_returned_not_raised(self, task_tool_module, runtime, monkeypatch):
        import deerflow.sandbox.tools as sandbox_tools

        def boom(_runtime, _path):
            raise RuntimeError("sandbox unavailable")

        monkeypatch.setattr(sandbox_tools, "read_current_file_content", boom)
        text, error = task_tool_module._resolve_prompt_source(runtime, None, PROMPT_PATH)
        assert text is None
        assert "could not read prompt_file" in error
        assert "sandbox unavailable" in error


class TestPathRestrictions:
    @pytest.mark.parametrize(
        "path",
        [
            "/etc/passwd",
            "/mnt/skills/custom/eligibility-judgment/references/judge-delegation.md",
            "relative/path.md",
            "/tmp/prompt.md",
        ],
    )
    def test_paths_outside_user_data_are_refused(self, task_tool_module, runtime, path):
        text, error = task_tool_module._resolve_prompt_source(runtime, None, path)
        assert text is None
        assert "must be an absolute path under /mnt/user-data/" in error

    def test_skills_path_is_refused_even_though_it_is_readable(self, task_tool_module, runtime, monkeypatch):
        """A skill file holds the *template*, with placeholders unresolved — not an assignment."""
        skill_path = "/mnt/skills/custom/eligibility-judgment/references/judge-delegation.md"
        _patch_reader(monkeypatch, {skill_path: "模板 {id} {SHARD}"})
        text, error = task_tool_module._resolve_prompt_source(runtime, None, skill_path)
        assert text is None
        assert "/mnt/user-data/" in error

    def test_traversal_is_refused(self, task_tool_module, runtime):
        text, error = task_tool_module._resolve_prompt_source(runtime, None, "/mnt/user-data/../../etc/passwd")
        assert text is None
        assert "must not contain '..'" in error

    def test_dotdot_inside_a_filename_is_allowed(self, task_tool_module, runtime, monkeypatch):
        """Only a whole ``..`` path segment is traversal; ``a..b.md`` is a legal name."""
        path = "/mnt/user-data/workspace/a..b.md"
        _patch_reader(monkeypatch, {path: RENDERED})
        text, error = task_tool_module._resolve_prompt_source(runtime, None, path)
        assert error is None
        assert text == RENDERED


class TestSizeCap:
    def test_a_template_sized_prompt_fits(self, task_tool_module, runtime, monkeypatch):
        """The real rendered template is ~12.7k chars — comfortably inside the cap."""
        _patch_reader(monkeypatch, {PROMPT_PATH: "判" * 13_000})
        _text, error = task_tool_module._resolve_prompt_source(runtime, None, PROMPT_PATH)
        assert error is None

    def test_a_data_dump_is_refused(self, task_tool_module, runtime, monkeypatch):
        """Pointing at an OCR dump would blow the subagent's context on message one."""
        oversized = "x" * (task_tool_module.PROMPT_FILE_MAX_CHARS + 1)
        _patch_reader(monkeypatch, {PROMPT_PATH: oversized})
        text, error = task_tool_module._resolve_prompt_source(runtime, None, PROMPT_PATH)
        assert text is None
        assert "over the" in error
        assert "not a data payload" in error


class TestToolSchema:
    """The parameter has to be visible to the model, or the renderer path is unreachable.

    Read via ``model_fields`` rather than ``model_json_schema()``: the schema includes the
    injected ``runtime`` argument, which pydantic cannot render as JSON schema.
    """

    def _fields(self, task_tool_module):
        return task_tool_module.task_tool.args_schema.model_fields

    def test_prompt_file_is_exposed_to_the_model(self, task_tool_module):
        fields = self._fields(task_tool_module)
        assert "prompt_file" in fields
        assert "verbatim" in fields["prompt_file"].description

    def test_prompt_file_description_points_at_the_rendering_workflow(self, task_tool_module):
        description = self._fields(task_tool_module)["prompt_file"].description
        assert "skill script" in description
        assert "/mnt/user-data/" in description

    def test_prompt_is_no_longer_required(self, task_tool_module):
        """``prompt`` became optional so ``prompt_file`` can stand in for it."""
        fields = self._fields(task_tool_module)
        assert not fields["prompt"].is_required()
        assert not fields["prompt_file"].is_required()

    def test_description_and_subagent_type_stay_required(self, task_tool_module):
        fields = self._fields(task_tool_module)
        assert fields["description"].is_required()
        assert fields["subagent_type"].is_required()
