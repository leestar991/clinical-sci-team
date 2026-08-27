"""``read_file`` line-range contract.

Why this file exists — session ``c2518bc7`` (EX 轨解析 task ``call_01_9CNnoJ…``) read
``parsing-rules.md`` **9 times** in one task. The transcript shows the mechanism:

    AI#1  read_file parsing-rules.md [whole]        -> truncated at max_chars
    AI#2  read_file parsing-rules.md [60:None]      -> SAME header block again
    AI#3  read_file parsing-rules.md [100:None]     -> tool error
    AI#4  read_file parsing-rules.md [100:None]     -> SAME header block again
    AI#5  read_file parsing-rules.md [1:10]         -> "the file seems to be repeating"
    AI#6  bash wc -l parsing-rules.md               -> 428
    AI#7  read_file parsing-rules.md [200:428]      -> finally the right window

The cause is one line in ``read_file_tool``::

    if start_line is not None and end_line is not None:

A read that supplies only ``start_line`` silently drops the range and returns the
**head** of the file, which the truncation marker then cuts — so the model sees the
identical opening block, concludes the file is broken, and probes with bash. Every
such probe re-sends the whole accumulated subagent context (billed input ≈
AI steps / 2 × accumulated content), which is why a display bug costs millions of
tokens rather than one wasted call.

The contract pinned here: a half-open range is honoured, the applied window is
stated so the model can compute the next one without a ``wc -l`` round-trip, and an
out-of-range start says so instead of returning the head.
"""

from pathlib import Path
from types import SimpleNamespace

from deerflow.sandbox.local.local_sandbox import LocalSandbox
from deerflow.sandbox.tools import read_file_tool

TOTAL_LINES = 428


def _local_runtime(tmp_path: Path) -> SimpleNamespace:
    for sub in ("workspace", "uploads", "outputs"):
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    thread_data = {
        "workspace_path": str(tmp_path / "workspace"),
        "uploads_path": str(tmp_path / "uploads"),
        "outputs_path": str(tmp_path / "outputs"),
    }
    return SimpleNamespace(
        state={"sandbox": {"sandbox_id": "local:t1"}, "thread_data": thread_data},
        context={"thread_id": "t1"},
    )


def _rules_file(tmp_path: Path, monkeypatch) -> SimpleNamespace:
    runtime = _local_runtime(tmp_path)
    body = "\n".join(f"line-{i:03d}" for i in range(1, TOTAL_LINES + 1))
    (tmp_path / "workspace" / "parsing-rules.md").write_text(body, encoding="utf-8")
    monkeypatch.setattr("deerflow.sandbox.tools.ensure_sandbox_initialized", lambda runtime: LocalSandbox("t1"))
    monkeypatch.setattr("deerflow.sandbox.tools.ensure_thread_directories_exist", lambda runtime: None)
    return runtime


def _read(runtime, **kwargs) -> str:
    return read_file_tool.func(runtime=runtime, description="read rules", path="/mnt/user-data/workspace/parsing-rules.md", **kwargs)


def test_start_line_without_end_line_reads_to_end_of_file(tmp_path, monkeypatch) -> None:
    """``[60:]`` must start at line 60 — not silently re-serve the file head."""
    runtime = _rules_file(tmp_path, monkeypatch)

    result = _read(runtime, start_line=60)

    assert "line-060" in result, result
    assert f"line-{TOTAL_LINES}" in result, result
    assert "line-001" not in result, "start_line was ignored — the model sees the same head block again"
    assert "line-059" not in result, result


def test_end_line_without_start_line_reads_from_first_line(tmp_path, monkeypatch) -> None:
    runtime = _rules_file(tmp_path, monkeypatch)

    result = _read(runtime, end_line=10)

    assert "line-001" in result, result
    assert "line-010" in result, result
    assert "line-011" not in result, result


def test_ranged_read_states_the_applied_window_and_total_lines(tmp_path, monkeypatch) -> None:
    """The model must be able to compute the next window without a ``wc -l`` probe.

    In the observed session the model spent an AI step on ``bash wc -l`` purely to
    learn the file length before it could ask for a valid closed range.
    """
    runtime = _rules_file(tmp_path, monkeypatch)

    result = _read(runtime, start_line=200, end_line=260)

    assert "200" in result and "260" in result, result
    assert str(TOTAL_LINES) in result, "total line count missing — model cannot size the next window"
    assert "line-200" in result and "line-260" in result, result


def test_end_line_beyond_eof_is_clamped_not_an_error(tmp_path, monkeypatch) -> None:
    runtime = _rules_file(tmp_path, monkeypatch)

    result = _read(runtime, start_line=420, end_line=9999)

    assert "line-428" in result, result
    assert not result.startswith("Error"), result


def test_start_line_beyond_eof_reports_the_file_length(tmp_path, monkeypatch) -> None:
    """Returning the head here is what taught the model the file was "repeating"."""
    runtime = _rules_file(tmp_path, monkeypatch)

    result = _read(runtime, start_line=9999)

    assert "line-001" not in result, "out-of-range start silently fell back to the file head"
    assert str(TOTAL_LINES) in result, result


def test_whole_file_read_has_no_window_header(tmp_path, monkeypatch) -> None:
    """No range requested → byte-identical content, so dedup/read-marks are unaffected."""
    runtime = _rules_file(tmp_path, monkeypatch)

    result = _read(runtime)

    assert result.startswith("line-001"), result


def test_truncated_whole_file_read_offers_a_closed_range(tmp_path, monkeypatch) -> None:
    """The truncation hint must name a *closed* range and the file length.

    The old marker said only "Use start_line/end_line to read a specific range",
    which is precisely the advice the model followed into ``[60:None]``.
    """
    runtime = _rules_file(tmp_path, monkeypatch)
    monkeypatch.setattr("deerflow.sandbox.tools._read_file_max_chars", lambda: 400)

    result = _read(runtime)

    assert "truncated" in result, result
    assert str(TOTAL_LINES) in result, "truncation hint must state the total line count"
    assert "end_line" in result and "start_line" in result, result
