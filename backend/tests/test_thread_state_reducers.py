"""Unit tests for ThreadState reducers.

Regression coverage for issue #3123: todos list disappearing after streaming
completes because a downstream node's partial state update with `todos=None`
overwrites the previously accumulated value.
"""

from typing import get_type_hints

from deerflow.agents.thread_state import (
    ThreadState,
    merge_artifacts,
    merge_sandbox,
    merge_thread_data,
    merge_title,
    merge_todos,
    merge_viewed_images,
)


class TestMergeTodos:
    """Reducer for ThreadState.todos - keeps last non-None value."""

    def test_new_value_overrides_existing(self):
        existing = [{"id": 1, "text": "old", "done": False}]
        new = [{"id": 1, "text": "old", "done": True}]
        assert merge_todos(existing, new) == new

    def test_none_new_preserves_existing(self):
        """THE KEY FIX for #3123: a node that doesn't touch todos must NOT
        wipe them out by returning an implicit None."""
        existing = [{"id": 1, "text": "task", "done": False}]
        assert merge_todos(existing, None) == existing

    def test_none_existing_accepts_new(self):
        new = [{"id": 1, "text": "first todo"}]
        assert merge_todos(None, new) == new

    def test_both_none_returns_none(self):
        assert merge_todos(None, None) is None

    def test_empty_list_is_explicit_clear(self):
        """An explicit empty list means 'user cleared all todos' and must
        win over the previous list."""
        existing = [{"id": 1, "text": "task"}]
        assert merge_todos(existing, []) == []


class TestMergeArtifacts:
    """Sanity check for the existing artifacts reducer."""

    def test_dedupes_and_preserves_order(self):
        assert merge_artifacts(["a", "b"], ["b", "c"]) == ["a", "b", "c"]

    def test_none_new_preserves_existing(self):
        assert merge_artifacts(["a"], None) == ["a"]

    def test_none_existing_accepts_new(self):
        assert merge_artifacts(None, ["a"]) == ["a"]


class TestMergeViewedImages:
    """Sanity check for the existing viewed_images reducer."""

    def test_merges_dicts(self):
        existing = {"k1": {"base64": "x", "mime_type": "image/png"}}
        new = {"k2": {"base64": "y", "mime_type": "image/jpeg"}}
        merged = merge_viewed_images(existing, new)
        assert set(merged.keys()) == {"k1", "k2"}

    def test_empty_dict_clears(self):
        existing = {"k1": {"base64": "x", "mime_type": "image/png"}}
        assert merge_viewed_images(existing, {}) == {}


class TestMergeSandbox:
    """Reducer for ThreadState.sandbox - keeps last non-None value.

    Regression coverage for concurrent sandbox writes during parallel
    tool execution via LangGraph Send fan-out.
    """

    def test_new_value_overrides_existing(self):
        existing = {"sandbox_id": "local:thread-1"}
        new = {"sandbox_id": "local:thread-2"}
        assert merge_sandbox(existing, new) == new

    def test_none_new_preserves_existing(self):
        existing = {"sandbox_id": "local:thread-1"}
        assert merge_sandbox(existing, None) == existing

    def test_none_existing_accepts_new(self):
        new = {"sandbox_id": "local:thread-1"}
        assert merge_sandbox(None, new) == new

    def test_both_none_returns_none(self):
        assert merge_sandbox(None, None) is None

    def test_idempotent_writes(self):
        """Multiple parallel tool calls writing the same sandbox_id must not conflict."""
        existing = {"sandbox_id": "local:thread-1"}
        new = {"sandbox_id": "local:thread-1"}
        assert merge_sandbox(existing, new) == existing


class TestMergeThreadData:
    """Reducer for ThreadState.thread_data - keeps last non-None value."""

    def test_new_value_overrides_existing(self):
        existing = {"workspace_path": "/old", "uploads_path": "/old/up", "outputs_path": "/old/out"}
        new = {"workspace_path": "/new", "uploads_path": "/new/up", "outputs_path": "/new/out"}
        assert merge_thread_data(existing, new) == new

    def test_none_new_preserves_existing(self):
        existing = {"workspace_path": "/path", "uploads_path": "/up", "outputs_path": "/out"}
        assert merge_thread_data(existing, None) == existing

    def test_none_existing_accepts_new(self):
        new = {"workspace_path": "/path", "uploads_path": "/up", "outputs_path": "/out"}
        assert merge_thread_data(None, new) == new

    def test_both_none_returns_none(self):
        assert merge_thread_data(None, None) is None


class TestMergeTitle:
    """Reducer for ThreadState.title - keeps last non-None value."""

    def test_new_value_overrides_existing(self):
        assert merge_title("old title", "new title") == "new title"

    def test_none_new_preserves_existing(self):
        assert merge_title("existing title", None) == "existing title"

    def test_none_existing_accepts_new(self):
        assert merge_title(None, "new title") == "new title"

    def test_both_none_returns_none(self):
        assert merge_title(None, None) is None

    def test_empty_string_is_explicit(self):
        """An empty string is a valid explicit title (not None)."""
        assert merge_title("old", "") == ""


class TestThreadStateAnnotations:
    """Regression guards: ensure reducer wiring on ThreadState fields.

    These tests protect against silent regressions where a field's
    ``Annotated[..., reducer]`` is reverted to a plain type, which would
    re-introduce bugs even when the reducer functions themselves remain
    correct.
    """

    def test_sandbox_field_is_wired_to_merge_sandbox(self):
        """ThreadState.sandbox must use merge_sandbox.

        Without this Annotated binding, LangGraph uses a LastValue channel
        that raises InvalidUpdateError when parallel tool calls (Send fan-out)
        each trigger lazy sandbox initialization in the same step.
        """
        hints = get_type_hints(ThreadState, include_extras=True)
        sandbox_hint = hints["sandbox"]
        assert hasattr(sandbox_hint, "__metadata__"), "ThreadState.sandbox must be Annotated with a reducer"
        assert merge_sandbox in sandbox_hint.__metadata__, "ThreadState.sandbox must be wired to merge_sandbox reducer"

    def test_thread_data_field_is_wired_to_merge_thread_data(self):
        """ThreadState.thread_data must use merge_thread_data."""
        hints = get_type_hints(ThreadState, include_extras=True)
        hint = hints["thread_data"]
        assert hasattr(hint, "__metadata__"), "ThreadState.thread_data must be Annotated with a reducer"
        assert merge_thread_data in hint.__metadata__, "ThreadState.thread_data must be wired to merge_thread_data reducer"

    def test_title_field_is_wired_to_merge_title(self):
        """ThreadState.title must use merge_title."""
        hints = get_type_hints(ThreadState, include_extras=True)
        hint = hints["title"]
        assert hasattr(hint, "__metadata__"), "ThreadState.title must be Annotated with a reducer"
        assert merge_title in hint.__metadata__, "ThreadState.title must be wired to merge_title reducer"

    def test_todos_field_is_wired_to_merge_todos(self):
        """ThreadState.todos must use merge_todos.

        Without this Annotated binding, LangGraph falls back to last-value-wins
        behavior, and partial state updates that omit todos will silently clear
        previously streamed values.
        """
        hints = get_type_hints(ThreadState, include_extras=True)
        todos_hint = hints["todos"]
        assert hasattr(todos_hint, "__metadata__"), "ThreadState.todos must be Annotated with a reducer"
        assert merge_todos in todos_hint.__metadata__, "ThreadState.todos must be wired to merge_todos reducer (see #3123)"

    def test_artifacts_field_is_wired_to_merge_artifacts(self):
        """Sanity check that existing reducer wiring is preserved."""
        hints = get_type_hints(ThreadState, include_extras=True)
        assert merge_artifacts in hints["artifacts"].__metadata__
