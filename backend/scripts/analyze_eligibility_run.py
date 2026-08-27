#!/usr/bin/env python3
"""Per-subagent token / step / read accounting for an eligibility-screener thread.

Why this exists: the baseline claim "34.4M tokens, 62 minutes" is only actionable if
it can be re-derived mechanically before and after each optimization. Reading a
conversation by hand cannot tell you that 63.6% of externalised ``read_file`` bytes
were byte-identical, or that 379 AI turns each re-inherited 42,758 tokens of skill
text. This script answers those from persisted run data only — it never replays a run
and never calls a model.

Data sources (read-only):
  * ``RunStore.list_by_thread``  — run-level token totals (lead / subagent / middleware).
  * ``RunEventStore.list_events`` — ``subagent.start`` / ``.step`` / ``.end`` per task.

Trust the events over the run row. The run's token columns are written at
finalization, so a snapshot of an unfinished run reports 0 while its events already
carry real usage. Session ``2d628340`` lost 17.2M that way — 11/11 tasks ``completed``,
``runs.status='running'``, ``runs.total_tokens=0``, report printed 628k. The script
therefore always prints ``from_tasks`` next to the run-row total and emits
``token_accounting_warnings`` when the two disagree or a run is non-terminal. It does
NOT filter by status: every run row is summed, exactly as before.

Waste metrics (added for the gate-loop optimization work):
  * ``empty_ai_steps`` — AI turns whose text is blank. Reported alongside
    ``empty_ai_steps_no_tool_calls`` because a turn that emits only tool calls
    legitimately has no prose; only the latter is unambiguous waste.
  * ``gate_script_calls`` — ``*.py`` scripts referenced by ``bash`` commands, i.e.
    "how many times did this task re-run the same gate". Split into
    ``gate_script_execs`` (ran it) and ``gate_script_inspects`` (read its source to work
    out what it wanted): session ``a7c19ea1`` was 51 vs 20, and a mixed count of 56 hid
    the second problem entirely — a gate whose failure message does not state its contract.
  * ``tool_error_steps`` / ``lead_tool_error_results`` / ``tool_error_total`` — failed tool
    calls, subagent and lead. Sandbox tools report failures as a payload rather than an
    exception, and not always as an ``"Error:"`` prefix: a non-zero ``bash`` exit, a Python
    traceback and an argparse usage dump all count. Matching only the prefix on only the
    subagent channel found 4 of the 11 failures in ``a7c19ea1``.
  * ``failed_tasks`` — subagent tasks that ended ``failed``.

Attribution note: per-task tokens come from ``subagent.end.metadata.usage``. The
``RunJournal`` folds subagent usage into a run-level scalar and drops ``task_id``
(``journal.py`` ``record_external_llm_usage_records``), and the parent journal is not
attached to subagent runs (``executor.py`` passes only ``SubagentTokenCollector``), so
the terminal event is the only place per-task usage survives. Runs recorded before that
field was persisted report ``usage_missing`` rather than 0 — a silently-zeroed task
would understate the total while looking complete.

Usage:
    PYTHONPATH=. uv run python scripts/analyze_eligibility_run.py <thread_id> \
        [--output report.json] [--baseline baseline.json]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

_SUBAGENT_EVENT_TYPES = ["subagent.start", "subagent.step", "subagent.end"]

# ── Metrics for the subagent-context / artifact-gate work (thread 88df83a8) ──────
# ``middleware:summarize`` is how often a subagent's context was compacted — the failure
# being tracked started with four compactions inside a single judgment task. The event
# carries ``changes.task_id``, so it can be attributed to the task whose context was
# compacted (lead-agent compactions carry ``task_id=None``).
_MIDDLEWARE_EVENT_TYPE = "middleware:summarize"

# Written by ``SubagentExecutor._verify_expected_outputs`` into the failure message. Kept
# as a literal (not imported) so this script keeps working against archived reports whose
# harness version differs.
_ARTIFACT_GATE_MARKER = "required outputs missing/empty"

# The lead agent's own tool results. Only subagent steps used to be read, so every failure
# the LEAD hit was invisible: 6 of the 11 in session ``a7c19ea1`` were the lead's, including
# four path-guard false positives and two writes rejected against a read-only skills path.
_LEAD_TOOL_RESULT_EVENT_TYPE = "llm.tool.result"

_ANALYZED_EVENT_TYPES = [*_SUBAGENT_EVENT_TYPES, _MIDDLEWARE_EVENT_TYPE, _LEAD_TOOL_RESULT_EVENT_TYPE]

# ``RunStatus`` values that mean "this run is still moving" (``runtime/runs/schemas.py``).
# Their token columns are written at finalization, so a snapshot taken mid-run reports 0
# while the events already carry real usage — the exact way session 2d628340 lost 17.2M.
_NONTERMINAL_RUN_STATUSES = frozenset({"pending", "running"})

# Sandbox tools report failures as an ``"Error: ..."`` payload rather than raising, so the
# persisted step text is the only place a tool misuse survives.
_TOOL_ERROR_PREFIX = "Error:"

# Failure shapes that do NOT start with ``Error:``. Matching only the prefix under-reported
# session ``a7c19ea1`` by more than half — it found 4 of 11 real failures, missing every
# ``bash`` non-zero exit (the tool returns the script's own output, then an exit line), the
# Python traceback from a corrupt JSON read, and the argparse usage dump from a missing
# ``--qc`` value. Anchored to line starts so the words cannot match prose inside a file the
# agent happened to read: ``Traceback (most recent call last):`` is a real traceback header,
# whereas the same words in a paragraph are not.
#
# Two anchors, because the shapes sit in different places. A tool that *reports* a failure
# leads with it, so ``Error:`` and a traceback header are matched at the very start of the
# payload only — ``Error:`` on a later line is usually a gate quoting an instruction
# ("若技能规定的退避重试后仍 `Error:`") or a file the agent read. An exit-status line, by
# contrast, is appended after the command's own stdout and must be matched anywhere, but
# anchored to its own line so an echoed "EXIT=2" inside a report does not count.
_TOOL_FAILURE_LEADING_RE = re.compile(r"^\s*(?:Error:|Traceback \(most recent call last\):)", re.IGNORECASE)
_TOOL_FAILURE_STATUS_RE = re.compile(
    r"^[ \t]*(?:Std ?Error:[ \t]*$|EXIT(?: CODE)?[ \t]*[=:][ \t]*[1-9]|Exit Code:[ \t]*[1-9])",
    re.MULTILINE | re.IGNORECASE,
)

# Any ``*.py`` token inside a bash command. Used to answer "how many times did this task
# re-run the same gate script", which is the headline metric of the judgment-phase loop.
_SCRIPT_TOKEN_RE = re.compile(r"[\w./+-]+\.py")

# Running a gate versus reading its source. Session ``a7c19ea1`` mixed both into one
# ``gate_script_calls=56``, which hid a second, independent problem: 51 of those were real
# executions, but 20 were ``sed``/``grep`` over the checker's own source — the subagent
# reverse-engineering what the gate wanted because the failure message did not say. Those
# two counts call for different fixes (fewer round trips vs. a clearer contract), so a
# single number cannot drive either.
_SCRIPT_EXEC_RE = re.compile(r"(?:python[0-9.]*|uv run(?:\s+python[0-9.]*)?)\s+(?:-\w+\s+)*\S*?([\w.+-]+\.py)")
# The text between the reader and its target may not be scanned for shell separators: a
# ``grep -n "闸10\|upstream" .../check_track_structure.py`` carries ``\|`` *inside* its
# quoted pattern, so excluding ``|`` never reaches the filename. Stop at a newline instead,
# and require the reader to open the line (or follow a pipe/``;``/``&&``) so a filename
# merely appearing after some other command is not attributed to it.
_SCRIPT_INSPECT_RE = re.compile(
    r"(?:^|[|;&]|\$\()\s*(?:sudo\s+)?(?:sed|grep|egrep|fgrep|rg|awk|cat|head|tail|nl|wc|less|more|od|xxd)\b[^\n]*?([\w.+-]+\.py)",
    re.MULTILINE,
)


def _tool_result_failed(text: Any) -> bool:
    """Whether a persisted tool result records a failure."""
    if not isinstance(text, str):
        return False
    return bool(_TOOL_FAILURE_LEADING_RE.match(text) or _TOOL_FAILURE_STATUS_RE.search(text))


# Metrics compared against a baseline. Keep this list short and outcome-shaped:
# a report that prints everything gets skimmed and nothing gets checked.
COMPARED_METRICS = (
    "total_tokens",
    "subagent_tokens",
    "subagent_tokens_from_tasks",
    "lead_agent_tokens",
    "ai_steps",
    "tool_steps",
    "empty_ai_steps",
    "read_file_calls",
    # `duplicate_read_calls` is NOT compared: it ignores the line range, so it counts every
    # re-read of a DIFFERENT window as waste and a delta on it means nothing. (Its task
    # dimension is fine — it is summed per task.) These two split it by fix.
    "dedupable_read_calls",
    "range_overlap_lines",
    "skill_md_reads",
    "gate_script_call_total",
    "tool_error_steps",
    "task_count",
    "failed_tasks",
    # Added for the subagent-context / artifact-gate work (thread 88df83a8). All three are
    # "did the guard change behaviour" metrics, not cost metrics:
    #   subagent_compactions   — how often a subagent's context was compacted. The failure
    #                            began with four compactions inside one judgment task, so a
    #                            rise here says the read discipline is not holding.
    #   whole_file_reread_calls— repeat WHOLE-file reads of one path in one task. This is
    #                            what drives the compactions; ReadFilePolicyMiddleware
    #                            should hold it at 0 in block mode.
    #   artifact_gate_failures — tasks that claimed completion without their declared
    #                            artifact. Non-zero is not automatically bad (the retry may
    #                            have recovered), but it must never be silent again.
    "subagent_compactions",
    "whole_file_reread_calls",
    "artifact_gate_failures",
    "active_seconds",
)


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _bash_command(args: Any) -> str | None:
    """Extract a bash command string from a persisted tool call's ``args``.

    ``step_events._bounded_tool_call`` replaces oversized ``args`` with a truncated
    JSON *string* and flags ``args_truncated``, so a dict-only reader would silently
    stop counting exactly the long gate invocations we care about.
    """
    if isinstance(args, dict):
        command = args.get("command")
        return command if isinstance(command, str) else None
    if isinstance(args, str):
        return args
    return None


def _gate_scripts_in_command(command: str) -> list[str]:
    """Return the basenames of ``*.py`` scripts referenced by *command*."""
    return [token.rsplit("/", 1)[-1] for token in _SCRIPT_TOKEN_RE.findall(command)]


def _gate_script_execs_in_command(command: str) -> list[str]:
    """Basenames of ``*.py`` scripts *run* by *command* (see ``_SCRIPT_EXEC_RE``)."""
    return [token.rsplit("/", 1)[-1] for token in _SCRIPT_EXEC_RE.findall(command)]


def _gate_script_inspects_in_command(command: str) -> list[str]:
    """Basenames of ``*.py`` scripts whose *source* *command* reads.

    High counts here mean the gate's failure message does not state its contract, so the
    agent is reading the implementation to infer it — a different defect from re-running.
    """
    return [token.rsplit("/", 1)[-1] for token in _SCRIPT_INSPECT_RE.findall(command)]


def _read_path(args: Any) -> str | None:
    """Extract the path argument of a read-like tool call."""
    if not isinstance(args, dict):
        return None
    for key in ("path", "file_path", "filename"):
        value = args.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _read_range(args: Any) -> tuple[int, int] | None:
    """The ``(start_line, end_line)`` of a ranged read, or ``None`` for a whole-file read.

    ``_int`` coerces junk to 0, so presence is checked before coercion — a missing
    ``start_line`` must read as "whole file", not as line 0.
    """
    if not isinstance(args, dict):
        return None
    if args.get("start_line") is None or args.get("end_line") is None:
        return None
    start, end = _int(args.get("start_line")), _int(args.get("end_line"))
    if start <= 0 or end < start:
        return None
    return (start, end)


class _TaskAccumulator:
    """Folds one subagent task's events into a flat record."""

    def __init__(self, task_id: str):
        self.task_id = task_id
        self.description: str | None = None
        self.status = "unfinished"
        self.ai_steps = 0
        self.tool_steps = 0
        self.empty_ai_steps = 0
        self.empty_ai_steps_no_tool_calls = 0
        self.tool_error_steps = 0
        self.gate_script_calls: Counter[str] = Counter()
        self.gate_script_execs: Counter[str] = Counter()
        self.gate_script_inspects: Counter[str] = Counter()
        self.tools: Counter[str] = Counter()
        self.read_paths: Counter[str] = Counter()
        # Keyed by (path, range) because that is the granularity ReadFileDedupMiddleware
        # actually keys on. Counting by path alone reports every ranged re-read of a
        # DIFFERENT window as a "recoverable duplicate" — which is how session `93d8a2c6`
        # came out showing 170 recoverable duplicates while dedup could only ever hit 6.
        self.read_keys: Counter[tuple[str, tuple[int, int] | None]] = Counter()
        # Requested vs distinct line coverage per path, for the overlap metric.
        self.read_ranges: dict[str, list[tuple[int, int]]] = {}
        # Whole-file (no start_line/end_line) reads per path. The 2nd+ of these is what
        # ReadFilePolicyMiddleware refuses, and what drove this task's compaction count.
        self.whole_file_reads: Counter[str] = Counter()
        # ``middleware:summarize`` events attributed to this task.
        self.compactions = 0
        # Terminal failure whose error names the artifact post-condition.
        self.artifact_gate_failed = False
        self.usage: dict[str, Any] | None = None
        self.first_seq: int | None = None
        self.last_seq: int | None = None
        self.started_at: str | None = None
        self.ended_at: str | None = None

    def add(self, event: dict) -> None:
        seq = event.get("seq")
        if isinstance(seq, int):
            self.first_seq = seq if self.first_seq is None else min(self.first_seq, seq)
            self.last_seq = seq if self.last_seq is None else max(self.last_seq, seq)

        etype = event.get("event_type")
        content = event.get("content") if isinstance(event.get("content"), dict) else {}
        metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}

        if etype == "subagent.start":
            self.description = content.get("description")
            self.started_at = event.get("created_at")
            return

        if etype == _MIDDLEWARE_EVENT_TYPE:
            # Compaction is attributed per task via ``changes.task_id`` (written by
            # ``DeerFlowSummarizationMiddleware._record_summarize_event``). Lead-agent
            # compactions carry ``task_id=None`` and are counted separately at run level.
            self.compactions += 1
            return

        if etype == "subagent.step":
            # A "step" is a persisted message, NOT a model call: a single AI turn is
            # followed by one tool message per tool call. Counting steps as LLM calls
            # is how "965 steps" gets misread as 965 model invocations.
            if content.get("kind") == "tool":
                self.tool_steps += 1
                text = content.get("text")
                if _tool_result_failed(text):
                    self.tool_error_steps += 1
                name = content.get("tool_name")
                if isinstance(name, str) and name:
                    self.tools[name] += 1
                return
            self.ai_steps += 1
            tool_calls = [call for call in (content.get("tool_calls") or []) if isinstance(call, dict)]
            # Two empty-step populations, deliberately separate. A turn that emits only
            # tool calls legitimately has no prose, so folding it into "empty" would
            # label normal tool use as waste; a turn with neither text nor tool calls
            # advanced the loop while producing nothing and still paid full input cost.
            text = content.get("text")
            if not (isinstance(text, str) and text.strip()):
                self.empty_ai_steps += 1
                if not tool_calls:
                    self.empty_ai_steps_no_tool_calls += 1
            for call in tool_calls:
                name = call.get("name")
                if not isinstance(name, str) or not name:
                    continue
                if name in ("read_file", "view_image"):
                    path = _read_path(call.get("args"))
                    if path:
                        self.read_paths[path] += 1
                        rng = _read_range(call.get("args"))
                        self.read_keys[(path, rng)] += 1
                        if rng is not None:
                            self.read_ranges.setdefault(path, []).append(rng)
                        elif name == "read_file":
                            # No range = whole file. Only read_file is governed by
                            # ReadFilePolicyMiddleware; view_image has no ranged form.
                            self.whole_file_reads[path] += 1
                elif name == "bash":
                    command = _bash_command(call.get("args"))
                    if command:
                        self.gate_script_calls.update(_gate_scripts_in_command(command))
                        self.gate_script_execs.update(_gate_script_execs_in_command(command))
                        self.gate_script_inspects.update(_gate_script_inspects_in_command(command))
            return

        if etype == "subagent.end":
            self.status = content.get("status") or "unknown"
            self.ended_at = event.get("created_at")
            usage = metadata.get("usage")
            if isinstance(usage, dict) and usage:
                self.usage = usage
            # The artifact post-condition writes its marker into the failure text. Detected
            # by marker rather than by status alone, so it stays distinguishable from an
            # ordinary failure in the same column.
            failure_text = " ".join(str(content.get(key) or "") for key in ("error", "result"))
            if _ARTIFACT_GATE_MARKER in failure_text or _ARTIFACT_GATE_MARKER in str(metadata.get("error") or ""):
                self.artifact_gate_failed = True

    def range_line_counts(self) -> tuple[int, int]:
        """``(lines requested, distinct lines covered)`` across this task's ranged reads.

        The gap between the two is how many lines were re-sent under a different window.
        Measured per path so two files' line numbers cannot cancel out.
        """
        requested = covered = 0
        for ranges in self.read_ranges.values():
            requested += sum(end - start + 1 for start, end in ranges)
            lines: set[int] = set()
            for start, end in ranges:
                lines.update(range(start, end + 1))
            covered += len(lines)
        return requested, covered

    def to_dict(self) -> dict:
        dup = {path: n for path, n in sorted(self.read_paths.items()) if n > 1}
        requested, covered = self.range_line_counts()
        total = _int(self.usage.get("total_tokens")) if self.usage else None
        return {
            "task_id": self.task_id,
            "description": self.description,
            "status": self.status,
            "step_count": self.ai_steps + self.tool_steps,
            "ai_step_count": self.ai_steps,
            "tool_step_count": self.tool_steps,
            "empty_ai_steps": self.empty_ai_steps,
            "empty_ai_steps_no_tool_calls": self.empty_ai_steps_no_tool_calls,
            "tool_error_steps": self.tool_error_steps,
            "gate_script_calls": dict(sorted(self.gate_script_calls.items())),
            # Split of the same references: run it, or read its source to infer its contract.
            "gate_script_execs": dict(sorted(self.gate_script_execs.items())),
            "gate_script_inspects": dict(sorted(self.gate_script_inspects.items())),
            "tools": dict(sorted(self.tools.items())),
            # Two independent sources, deliberately not merged:
            #   read_file_calls  — executed tool invocations (tool steps carry the
            #                      tool NAME but no arguments).
            #   read_paths_seen  — path references on the AI side (tool_calls carry
            #                      the ARGS but only for turns that were persisted).
            # Dividing one by the other would silently mix the two populations.
            "read_file_calls": self.tools.get("read_file", 0),
            "read_paths_seen": sum(self.read_paths.values()),
            "unique_read_paths": len(self.read_paths),
            "duplicate_read_paths": dup,
            # The only duplicates a version-aware read cache can suppress: SAME path AND
            # SAME line range, inside one task. Re-reading a different window of the same
            # file is new content, not waste.
            "dedupable_read_calls": sum(n - 1 for n in self.read_keys.values() if n > 1),
            "ranged_read_calls": sum(1 for (_p, rng), n in self.read_keys.items() if rng is not None for _ in range(n)),
            "whole_file_read_calls": sum(n for (_p, rng), n in self.read_keys.items() if rng is None),
            # Overlap is the OTHER kind of read waste: windows that re-send lines already
            # sent under a different range. Reported separately because the fix differs —
            # dedup cannot touch it, only the reading strategy can.
            "range_lines_requested": requested,
            "range_lines_distinct": covered,
            "range_overlap_lines": requested - covered,
            # Repeat WHOLE-file reads of one path within this task: the 2nd+ read of each
            # path. This is the population ReadFilePolicyMiddleware refuses in block mode,
            # and the driver behind this task's compaction count.
            "whole_file_reread_calls": sum(n - 1 for n in self.whole_file_reads.values() if n > 1),
            "whole_file_reread_paths": {path: n for path, n in sorted(self.whole_file_reads.items()) if n > 1},
            "compactions": self.compactions,
            "artifact_gate_failed": self.artifact_gate_failed,
            "input_tokens": _int(self.usage.get("input_tokens")) if self.usage else None,
            "output_tokens": _int(self.usage.get("output_tokens")) if self.usage else None,
            "total_tokens": total,
            "usage_missing": self.usage is None,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "first_seq": self.first_seq,
            "last_seq": self.last_seq,
        }


def _run_summary(row: dict) -> dict:
    return {
        "run_id": row.get("run_id"),
        "status": row.get("status"),
        "total_tokens": _int(row.get("total_tokens")),
        "input_tokens": _int(row.get("total_input_tokens")),
        "output_tokens": _int(row.get("total_output_tokens")),
        "lead_agent_tokens": _int(row.get("lead_agent_tokens")),
        "subagent_tokens": _int(row.get("subagent_tokens")),
        "middleware_tokens": _int(row.get("middleware_tokens")),
        "llm_call_count": _int(row.get("llm_call_count")),
        "message_count": _int(row.get("message_count")),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def _active_seconds(runs: list[dict]) -> float:
    """Sum of per-run wall time. Runs of one thread are sequential, so summing is
    closer to "time the user waited" than max(updated) - min(created), which would
    also count idle gaps between runs."""
    from datetime import datetime

    total = 0.0
    for row in runs:
        start, end = row.get("created_at"), row.get("updated_at")
        if not isinstance(start, str) or not isinstance(end, str):
            continue
        try:
            total += (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()
        except ValueError:
            continue
    return round(total, 3)


def _token_accounting_warnings(run_rows: list[dict], tasks_total: int) -> list[str]:
    """Say out loud when the run-row totals cannot be trusted.

    Two independent failure modes, both observed on real threads:

    * A run whose status is still ``pending``/``running`` has not written its token
      columns yet (they land at finalization), so summing the rows understates it.
    * Even a ``success`` row can read 0 if the snapshot predates the write-back. The
      subagent events already carry per-task ``usage``, so ``run rows < events`` is a
      mechanical tell that the scalar is stale.

    Session ``2d628340`` hit both: 11/11 tasks ``completed``, 17.2M token of real usage,
    ``runs.status='running'``, ``runs.total_tokens=0`` — and the report happily printed
    628k. Without these warnings every before/after comparison built on this script is
    silently wrong.
    """
    warnings: list[str] = []
    for row in run_rows:
        status = str(row.get("status") or "unknown")
        if status in _NONTERMINAL_RUN_STATUSES:
            warnings.append(f"⚠ run {row.get('run_id')} 处于非终态（status={status}）：token 列在收尾时才回写，本次快照会低估该 run 的消耗。以 subagent 事件派生值为准。")
    run_total = sum(_int(r.get("total_tokens")) for r in run_rows)
    if tasks_total > run_total:
        warnings.append(f"⚠ run 行 token 求和 {run_total:,} 小于 subagent 事件派生总量 {tasks_total:,}（差 {tasks_total - run_total:,}）：至少一个 run 的 token 列尚未回写，真实消耗被低估。做基线对比时请使用 subagent_tokens_from_tasks。")
    return warnings


async def analyze_run(
    thread_id: str,
    *,
    run_store,
    event_store,
    event_limit: int = 100_000,
    user_id: str | None = None,
) -> dict:
    """Aggregate per-task and per-run accounting for *thread_id*.

    ``user_id=None`` is the documented CLI/migration opt-out for
    ``RunRepository.list_by_thread``: without it the repository resolves
    ``user_id=AUTO`` from a request-scoped contextvar that no CLI ever sets and
    raises. Analysis is read-only, so scoping by user adds nothing here.
    """
    try:
        runs = list(await run_store.list_by_thread(thread_id, user_id=user_id))
    except TypeError:
        # Test doubles / stores that do not take a user_id keyword.
        runs = list(await run_store.list_by_thread(thread_id))
    tasks: dict[str, _TaskAccumulator] = {}
    lead_compactions = 0
    lead_tool_error_results = 0
    lead_tool_errors_by_tool: Counter[str] = Counter()

    for row in runs:
        run_id = row.get("run_id")
        if not run_id:
            continue
        try:
            events = await event_store.list_events(
                thread_id,
                run_id,
                event_types=_ANALYZED_EVENT_TYPES,
                limit=event_limit,
                user_id=user_id,
            )
        except TypeError:
            # Test doubles / stores that do not take a user_id keyword.
            events = await event_store.list_events(
                thread_id,
                run_id,
                event_types=_ANALYZED_EVENT_TYPES,
                limit=event_limit,
            )
        for event in events:
            metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
            content = event.get("content") if isinstance(event.get("content"), dict) else {}
            if event.get("event_type") == _LEAD_TOOL_RESULT_EVENT_TYPE:
                # Lead tool results belong to no task, so they are tallied at run level.
                # Both signals matter: ``status`` is set by the tool framework, while a
                # sandbox tool that returns its failure as the payload keeps ``status`` at
                # "success" — which is how 4 of these were missed entirely.
                if content.get("status") not in (None, "success") or _tool_result_failed(content.get("content")):
                    lead_tool_error_results += 1
                    name = content.get("name")
                    lead_tool_errors_by_tool[str(name) if name else "(unnamed)"] += 1
                continue
            task_id = metadata.get("task_id") or content.get("task_id")
            if task_id is None and event.get("event_type") == _MIDDLEWARE_EVENT_TYPE:
                # A compaction event keeps its attribution one level down, inside the
                # audit payload the middleware wrote.
                changes = content.get("changes") if isinstance(content.get("changes"), dict) else {}
                task_id = changes.get("task_id")
                if not task_id:
                    # Lead-agent compaction: counted at run level, not against a task.
                    lead_compactions += 1
                    continue
            if not task_id:
                continue
            tasks.setdefault(str(task_id), _TaskAccumulator(str(task_id))).add(event)

    task_rows = [acc.to_dict() for acc in tasks.values()]
    task_rows.sort(key=lambda t: (t["first_seq"] if t["first_seq"] is not None else 0, t["task_id"]))
    run_rows = [_run_summary(r) for r in runs]

    read_calls = sum(t["read_file_calls"] for t in task_rows)
    paths_seen = sum(t["read_paths_seen"] for t in task_rows)
    unique_reads = sum(t["unique_read_paths"] for t in task_rows)
    # Counted from the accumulators: the per-task tool histogram has no paths, and
    # duplicate_read_paths only holds paths read 2+ times, so neither can answer
    # "how many SKILL.md reads happened at all" — which is Task 2's acceptance metric.
    skill_reads = sum(n for acc in tasks.values() for path, n in acc.read_paths.items() if "SKILL.md" in path)
    gate_calls: Counter[str] = Counter()
    gate_execs: Counter[str] = Counter()
    gate_inspects: Counter[str] = Counter()
    for acc in tasks.values():
        gate_calls.update(acc.gate_script_calls)
        gate_execs.update(acc.gate_script_execs)
        gate_inspects.update(acc.gate_script_inspects)

    totals = {
        "run_count": len(run_rows),
        "task_count": len(task_rows),
        "failed_tasks": sum(1 for t in task_rows if t["status"] == "failed"),
        "total_tokens": sum(r["total_tokens"] for r in run_rows),
        "input_tokens": sum(r["input_tokens"] for r in run_rows),
        "output_tokens": sum(r["output_tokens"] for r in run_rows),
        "lead_agent_tokens": sum(r["lead_agent_tokens"] for r in run_rows),
        "subagent_tokens": sum(r["subagent_tokens"] for r in run_rows),
        "middleware_tokens": sum(r["middleware_tokens"] for r in run_rows),
        "lead_llm_calls": sum(r["llm_call_count"] for r in run_rows),
        "ai_steps": sum(t["ai_step_count"] for t in task_rows),
        "tool_steps": sum(t["tool_step_count"] for t in task_rows),
        "empty_ai_steps": sum(t["empty_ai_steps"] for t in task_rows),
        "empty_ai_steps_no_tool_calls": sum(t["empty_ai_steps_no_tool_calls"] for t in task_rows),
        "tool_error_steps": sum(t["tool_error_steps"] for t in task_rows),
        # Subagent + lead. Reported separately as well, because the two channels usually
        # fail for different reasons and only the subagent side was ever counted.
        "lead_tool_error_results": lead_tool_error_results,
        "lead_tool_errors_by_tool": dict(sorted(lead_tool_errors_by_tool.items())),
        "tool_error_total": sum(t["tool_error_steps"] for t in task_rows) + lead_tool_error_results,
        "gate_script_calls": dict(sorted(gate_calls.items())),
        "gate_script_call_total": sum(gate_calls.values()),
        # Executions vs. source inspections. A single mixed number cannot drive either fix:
        # too many execs means too many round trips; too many inspects means the gate's
        # failure message does not state what it wants (session ``a7c19ea1``: 51 vs 20).
        "gate_script_execs": dict(sorted(gate_execs.items())),
        "gate_script_exec_total": sum(gate_execs.values()),
        "gate_script_inspects": dict(sorted(gate_inspects.items())),
        "gate_script_inspect_total": sum(gate_inspects.values()),
        "read_file_calls": read_calls,
        "read_paths_seen": paths_seen,
        "unique_read_paths": unique_reads,
        # ⚠️ Kept for continuity with the earlier baselines, but do NOT read it as an
        # opportunity size. It is already task-scoped (per-task counts, then summed), so the
        # task dimension is fine — what it ignores is the LINE RANGE. Re-reading a different
        # window of the same file is new content, and 70% of reads in session `93d8a2c6` were
        # ranged with almost no repeated window: the metric read 170 while only 6 calls were
        # ever dedupable, and acting on it cost a whole optimisation round.
        "duplicate_read_calls": paths_seen - unique_reads,
        "dedupable_read_calls": sum(t["dedupable_read_calls"] for t in task_rows),
        "ranged_read_calls": sum(t["ranged_read_calls"] for t in task_rows),
        "whole_file_read_calls": sum(t["whole_file_read_calls"] for t in task_rows),
        "range_lines_requested": sum(t["range_lines_requested"] for t in task_rows),
        "range_lines_distinct": sum(t["range_lines_distinct"] for t in task_rows),
        "range_overlap_lines": sum(t["range_overlap_lines"] for t in task_rows),
        "skill_md_reads": skill_reads,
        "subagent_compactions": sum(t["compactions"] for t in task_rows),
        "lead_compactions": lead_compactions,
        "whole_file_reread_calls": sum(t["whole_file_reread_calls"] for t in task_rows),
        "artifact_gate_failures": sum(1 for t in task_rows if t["artifact_gate_failed"]),
        "subagent_tokens_from_tasks": sum(t["total_tokens"] or 0 for t in task_rows),
        "tasks_missing_usage": sum(1 for t in task_rows if t["usage_missing"]),
        "active_seconds": _active_seconds(runs),
    }

    return {
        "thread_id": thread_id,
        "totals": totals,
        "token_accounting_warnings": _token_accounting_warnings(run_rows, totals["subagent_tokens_from_tasks"]),
        "runs": run_rows,
        "tasks": task_rows,
    }


def compare(current: dict, baseline: dict) -> dict:
    """Metric-by-metric delta. ``pct`` is ``None`` when the baseline is 0."""
    cur, base = current.get("totals") or {}, baseline.get("totals") or {}
    out: dict[str, dict] = {}
    for key in COMPARED_METRICS:
        if key not in cur and key not in base:
            continue
        # A metric the baseline predates must NOT read as "0 -> 9": that looks like a
        # regression from zero when the truth is "this baseline has no such figure".
        # Presenting a wrong number confidently is the failure mode this whole comparison
        # table exists to avoid.
        if key not in base:
            out[key] = {"baseline": None, "current": cur.get(key) or 0, "delta": None, "pct": None, "note": "baseline 无此口径"}
            continue
        c, b = cur.get(key) or 0, base.get(key) or 0
        out[key] = {
            "baseline": b,
            "current": c,
            "delta": c - b,
            "pct": round((c - b) / b * 100, 2) if b else None,
        }
    return out


def _render(report: dict, delta: dict | None) -> str:
    t = report["totals"]
    lines = [
        f"thread {report['thread_id']}: {t['run_count']} run(s), {t['task_count']} subagent task(s)" + (f"  ⚠ {t['failed_tasks']} failed" if t.get("failed_tasks") else ""),
        f"  tokens   total={t['total_tokens']:,}  lead={t['lead_agent_tokens']:,}  subagent={t['subagent_tokens']:,}  middleware={t['middleware_tokens']:,}",
        # Printed next to the run-row total on purpose: the two disagreeing is the only
        # visible symptom of a token column that has not been written back yet.
        f"           from_tasks={t['subagent_tokens_from_tasks']:,} (subagent.end usage, cross-check)",
        f"  steps    ai={t['ai_steps']}  tool={t['tool_steps']}  (lead llm calls={t['lead_llm_calls']})",
        f"  waste    empty_ai={t['empty_ai_steps']} (of which no_tool_calls={t['empty_ai_steps_no_tool_calls']})"
        + f"  tool_errors={t.get('tool_error_total', t['tool_error_steps'])} (subagent={t['tool_error_steps']}, lead={t.get('lead_tool_error_results', 0)})",
        f"  reads    read_file_calls={t['read_file_calls']}  unique_paths={t['unique_read_paths']}  ranged={t['ranged_read_calls']}  whole_file={t['whole_file_read_calls']}  SKILL.md={t['skill_md_reads']}",
        # Two waste figures with two different fixes, deliberately not summed:
        #   dedupable      — same task, same path, same range → a read cache can remove it.
        #   range_overlap  — lines re-sent under a different window → only the reading
        #                    strategy can remove it; widening windows makes it worse.
        f"           dedupable={t['dedupable_read_calls']}  range_overlap={t['range_overlap_lines']} lines of {t['range_lines_requested']} requested"
        + (f" ({t['range_overlap_lines'] * 100 // t['range_lines_requested']}%)" if t["range_lines_requested"] else ""),
        f"  time     active={t['active_seconds']}s ({t['active_seconds'] / 60:.1f} min)",
        # The three guard metrics, printed together because they are one causal chain:
        # repeat whole-file reads inflate a task's context -> compaction fires -> a subagent
        # that loses its working state stops producing the artifact it was dispatched for.
        f"  guards   whole_file_rereads={t.get('whole_file_reread_calls', 0)}  subagent_compactions={t.get('subagent_compactions', 0)}"
        + f" (lead={t.get('lead_compactions', 0)})  artifact_gate_failures={t.get('artifact_gate_failures', 0)}",
    ]
    if t.get("gate_script_calls"):
        top_gates = sorted(t["gate_script_calls"].items(), key=lambda kv: -kv[1])[:6]
        lines.append("  gates    " + "  ".join(f"{name}={n}" for name, n in top_gates) + f"  (total={t['gate_script_call_total']})")
        # exec vs inspect: re-running a gate and reading its source are different defects.
        lines.append(f"           exec={t.get('gate_script_exec_total', 0)}  read_source={t.get('gate_script_inspect_total', 0)} (gate contract unclear if high)")
    if t.get("lead_tool_errors_by_tool"):
        by_tool = sorted(t["lead_tool_errors_by_tool"].items(), key=lambda kv: -kv[1])[:6]
        lines.append("  lead errs " + "  ".join(f"{name}={n}" for name, n in by_tool))
    for warning in report.get("token_accounting_warnings") or []:
        lines.append(f"  {warning}")
    if t["tasks_missing_usage"]:
        lines.append(f"  ⚠ {t['tasks_missing_usage']} task(s) have no persisted usage (run predates per-task usage persistence) — per-task token totals are incomplete, not zero.")

    top = sorted((x for x in report["tasks"] if x["total_tokens"]), key=lambda x: -(x["total_tokens"] or 0))[:5]
    if top:
        lines.append("  heaviest tasks:")
        lines.extend(f"    {x['total_tokens']:>10,}  {x['ai_step_count']:>3} ai  {x['task_id']}  {(x['description'] or '')[:52]}" for x in top)
    if delta:
        lines.append("  vs baseline:")
        for key, d in delta.items():
            if d.get("note"):
                lines.append(f"    {key:<24} {'—':>12} -> {d['current']:>12,}  ({d['note']})")
                continue
            pct = "n/a" if d["pct"] is None else f"{d['pct']:+.1f}%"
            lines.append(f"    {key:<24} {d['baseline']:>12,} -> {d['current']:>12,}  ({pct})")
    return "\n".join(lines)


async def _open_stores(config):
    """Build the same store pair the Gateway uses at startup (``app/gateway/deps.py``).

    The engine must be initialised before ``RunRepository``: without it
    ``get_session_factory()`` returns ``None`` and we would silently analyse an empty
    in-memory store and report 0 tokens for a real thread.
    """
    from deerflow.persistence.engine import init_engine_from_config
    from deerflow.runtime.events.store import make_run_event_store

    await init_engine_from_config(config.database)

    from deerflow.persistence.engine import get_session_factory

    session_factory = get_session_factory()
    if session_factory is None:
        raise SystemExit("No database session factory: database.backend is memory-only, so no persisted run data exists to analyse. Point config.yaml at the sqlite/postgres backend that recorded the thread.")

    from deerflow.persistence.run import RunRepository

    return RunRepository(session_factory), make_run_event_store(getattr(config, "run_events", None))


async def _amain(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("thread_id")
    parser.add_argument("--output", help="write the full JSON report here")
    parser.add_argument("--baseline", help="compare against a previously written report")
    args = parser.parse_args(argv)

    # Imported lazily so --help works without a configured database.
    from deerflow.config import get_app_config

    run_store, event_store = await _open_stores(get_app_config())
    report = await analyze_run(args.thread_id, run_store=run_store, event_store=event_store)

    delta = None
    if args.baseline:
        baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
        delta = compare(report, baseline)
        report["baseline_comparison"] = delta

    print(_render(report, delta))
    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n→ {out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_amain(argv))


if __name__ == "__main__":
    sys.exit(main())
