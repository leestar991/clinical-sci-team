#!/usr/bin/env python3
"""Wall-clock and failure accounting for one thread — where did the time go, what broke.

Companion to ``analyze_eligibility_run.py``, which answers "where did the TOKENS go".
Neither subsumes the other: a run can be cheap and slow (serial LLM waits) or fast and
expensive (parallel subagents), and the fixes differ. Session ``a7c19ea1`` was 113.9 min
with 98.1% of the wall clock inside serial LLM calls — tool execution and IO together
accounted for 127s, so every token-shaped metric pointed at the wrong lever.

Read-only: reads persisted run data plus (optionally) the gateway log. Never replays a
run, never calls a model.

WHY THE LOG IS NEEDED FOR SUBAGENT TIMING
-----------------------------------------
Two clocks, and only one of them is instrumented:

* ``llm.ai.response.metadata.latency_ms`` — exact per-call latency, measured monotonically
  by ``RunJournal.on_llm_end``. Covers the LEAD agent and middleware callers ONLY;
  subagent LLM calls have no journal attached (``executor.py`` passes just a
  ``SubagentTokenCollector``), so they carry no latency at all.
* ``gateway.log`` ``task_tool`` poll lines (``Task <id> sent message #N/M``) — the only
  wall-clock source for subagent steps, at the poll loop's 5s resolution.

Since 2026-08-13 ``subagent.step`` events carry a real ``created_at`` stamped at step time,
so step ordering and gaps are readable from the database alone. Runs recorded BEFORE that
fix have flush-time stamps instead (``SubagentStepEventBuffer`` batches at
``FLUSH_THRESHOLD=25``, so a 127-step task collapsed onto 7 distinct second values) — this
script detects that shape and says so rather than reporting fabricated per-step timings.

Retry tasks (``<id>-retry1``) never emit ``Started background task``; their start comes
from the executor's ``starting async execution, task_id=`` line. Log timestamps are UTC+8
(``logging_config.py`` pins ``Asia/Shanghai``) while the database is UTC — the two are
reconciled here, and every rendered time is UTC+8 to match what an operator reads in the
log.

WHAT IT REPORTS
---------------
* Per-run wall clock, and the human gap between runs (a follow-up run's think time is not
  the system being slow).
* Lead LLM busy time as a UNION of call windows, so overlapping calls are not double
  counted, plus the share of the session it covers.
* Subagent busy time as a union of task windows, and the overlap between the two — a large
  overlap means real parallelism, a near-zero one means the lead just waits in its poll
  loop (which is what makes a session serial).
* Latency against context size and output size, which is what separates "context too big"
  from "reasoning too long". These call for opposite fixes.
* The dispatch pattern — how many ``task`` tool calls each lead reply packed — and the
  measured subagent concurrency watermark, which settle WHY a session was serial (model
  habit vs. a global phase barrier) instead of just observing that it was.
* Every failed tool call, lead and subagent, with the failure text.
* Compaction attempts that produced no summary (an empty summary costs a full model call
  and changes nothing).

Usage:
    PYTHONPATH=. uv run python scripts/analyze_run_timing.py <thread_id> \
        [--log ../logs/gateway.log] [--output report.json] [--failures-only]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# Log timestamps are UTC+8 by deliberate choice (``logging_config.py``), the database is
# UTC. Rendering in UTC+8 keeps this report comparable with the log an operator is reading.
DISPLAY_TZ = timezone(timedelta(hours=8))

_LEAD_RESPONSE = "llm.ai.response"
_LEAD_TOOL_RESULT = "llm.tool.result"
_SUBAGENT_EVENTS = ("subagent.start", "subagent.step", "subagent.end")
_MIDDLEWARE_SUMMARIZE = "middleware:summarize"
_ANALYZED = [_LEAD_RESPONSE, _LEAD_TOOL_RESULT, *_SUBAGENT_EVENTS, _MIDDLEWARE_SUMMARIZE]

_TASK_TOOL_NAMES = ("task", "task_tool")
_WRITE_TOOL_NAMES = ("write_file", "write_file_tool")
#: First summary write of a phase marks its boundary (phase2_5 and phase3 both exist).
_PHASE_SUMMARY = re.compile(r"phase(\d+(?:_\d+)?)_summary\.json")

#: Gate scripts whose designed non-zero exits are VERDICTS, not tool failures
#: (f9231297: 25 "failures" were mostly gates correctly reporting problems).
_GATE_COMMANDS = (
    "uncertain_recheck",
    "check_reason_alignment",
    "exclusion_direction_check",
    "check_judgment_structure",
    "check_track_structure",
    "criteria_qc",
    "locate_criteria_sections",
    "judge_pack",
    "criteria_qc_bundle",
)
#: Textual signatures of an actual gate verdict (usage errors / tracebacks lack them).
_GATE_VERDICT_MARKERS = (
    "⚠️ 疑似漏判",
    "✅ 反查通过",
    "reason 对齐闸",
    "stage=draft",
    "· 基线已写入",
    "闸",
    "suspected_missed",
)

# ── failure detection ────────────────────────────────────────────────────────────
# Two anchors, because the shapes sit in different places. A tool that *reports* a failure
# leads with it, so these are matched at the start of the payload only — ``Error:`` on a
# later line is usually a gate quoting an instruction, or a source file the agent read.
_FAILURE_LEADING = re.compile(r"^\s*(?:Error:|Traceback \(most recent call last\):)", re.IGNORECASE)
# An exit-status line is appended AFTER the command's own stdout, so it must be matched
# anywhere — but anchored to its own line, so an echoed "EXIT=2" inside a report body does
# not count. Both halves were found by re-running against real data: the loose version
# flagged skill docs that merely mention ``Error:``.
_FAILURE_STATUS = re.compile(
    r"^[ \t]*(?:Std ?Error:[ \t]*$|EXIT(?: CODE)?[ \t]*[=:][ \t]*[1-9]|Exit Code:[ \t]*[1-9])",
    re.MULTILINE | re.IGNORECASE,
)

# ── gateway.log line shapes ──────────────────────────────────────────────────────
_LOG_TS = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) - ")
# Covers retries too, unlike "Started background task".
_LOG_EXEC_START = re.compile(r"Subagent (\S+) starting async execution, task_id=(\S+), timeout=(\d+)s")
_LOG_STEP = re.compile(r"Task (\S+) sent message #(\d+)/(\d+)")
_LOG_RETRY = re.compile(r"Task (\S+) failed, retrying as (\S+)")
_LOG_DONE = re.compile(r"Task (\S+) (completed after \d+ polls|failed \(no retries left\)|failed with stop_reason=\S+|cancelled|timed out)")
_LOG_EMPTY_SUMMARY = re.compile(r"Summary model returned no text \(finish_reason='(\w+)', output_tokens=(\d+), reasoning_chars=(\d+)\)")

#: Below this many distinct second-values per step, the stamps are flush times, not step
#: times (the pre-2026-08-13 shape). Reported as a caveat instead of silently trusted.
_FLUSH_TIME_SUSPICION_RATIO = 0.35


def tool_result_failed(text: Any) -> bool:
    """Whether a persisted tool result records a failure."""
    if not isinstance(text, str):
        return False
    return bool(_FAILURE_LEADING.match(text) or _FAILURE_STATUS.search(text))


def classify_failure(text: str, command: str | None = None) -> str:
    """``"failure"`` vs ``"gate_finding"`` for a result that ``tool_result_failed`` flagged.

    Gates exit non-zero by design when they FIND problems — that is them doing
    their job, and counting it as a tool failure floods the metric (f9231297:
    bash=19 "failures", most were gate verdicts). Verdicts carry structural
    markers and come from a gate-script command; argparse ``usage:`` noise and
    tracebacks stay genuine failures.
    """
    if "Traceback" in text[:2000] or "usage:" in text[:200]:
        return "failure"
    in_gate_command = bool(command) and any(name in command for name in _GATE_COMMANDS)
    has_verdict = any(marker in text for marker in _GATE_VERDICT_MARKERS)
    if has_verdict and (in_gate_command or "EXIT" in text[:600]):
        return "gate_finding"
    return "failure"


def _local(value: Any) -> datetime | None:
    """Parse a stored timestamp into a UTC+8 datetime."""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str) and value:
        try:
            dt = datetime.fromisoformat(value)
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(DISPLAY_TZ)


def merge_intervals(intervals: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    """Union of (start, end) pairs, so overlapping windows are counted once."""
    merged: list[tuple[datetime, datetime]] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _span(intervals: list[tuple[datetime, datetime]]) -> float:
    return sum((end - start).total_seconds() for start, end in intervals)


def _overlap(a: list[tuple[datetime, datetime]], b: list[tuple[datetime, datetime]]) -> float:
    """Seconds where a merged interval from *a* coincides with one from *b*."""
    total = 0.0
    for a_start, a_end in a:
        for b_start, b_end in b:
            lo, hi = max(a_start, b_start), min(a_end, b_end)
            if hi > lo:
                total += (hi - lo).total_seconds()
    return total


# ── gateway.log ------------------------------------------------------------------


def parse_gateway_log(path: Path, window: tuple[datetime, datetime] | None = None) -> dict[str, Any]:
    """Subagent task timing + aborted compactions, from the log's UTC+8 timestamps.

    Only lines inside *window* are kept, so a long-lived log shared by many threads does
    not attribute another thread's tasks to this one. The log carries no thread id on these
    lines, which is precisely why the window matters.
    """
    tasks: dict[str, dict[str, Any]] = defaultdict(lambda: {"start": None, "end": None, "steps": [], "outcome": None, "type": None})
    empty_summaries: list[dict[str, Any]] = []

    with path.open(errors="replace") as handle:
        for line in handle:
            stamp = _LOG_TS.match(line)
            if not stamp:
                continue
            when = datetime.strptime(stamp.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=DISPLAY_TZ)
            if window and not (window[0] <= when <= window[1]):
                continue
            rest = line[stamp.end() :]

            if match := _LOG_EXEC_START.search(rest):
                task = tasks[match.group(2)]
                if task["start"] is None:
                    task["start"] = when
                task["type"] = match.group(1)
            elif match := _LOG_STEP.search(rest):
                tasks[match.group(1)]["steps"].append(when)
            elif match := _LOG_RETRY.search(rest):
                tasks[match.group(1)]["end"] = when
                tasks[match.group(1)]["outcome"] = f"failed → retry as {match.group(2)}"
            elif match := _LOG_DONE.search(rest):
                tasks[match.group(1)]["end"] = when
                tasks[match.group(1)]["outcome"] = match.group(2)
            elif match := _LOG_EMPTY_SUMMARY.search(rest):
                empty_summaries.append(
                    {
                        "at": when.isoformat(),
                        "finish_reason": match.group(1),
                        "output_tokens": int(match.group(2)),
                        "reasoning_chars": int(match.group(3)),
                    }
                )

    return {"tasks": {k: v for k, v in tasks.items() if v["start"]}, "empty_summaries": empty_summaries}


# ── database ---------------------------------------------------------------------


async def _open_stores(config):
    """The store pair the Gateway builds at startup (mirrors ``analyze_eligibility_run``).

    The engine must be initialised before ``RunRepository``, or ``get_session_factory()``
    returns ``None`` and we would silently analyse an empty in-memory store.
    """
    from deerflow.persistence.engine import init_engine_from_config
    from deerflow.runtime.events.store import make_run_event_store

    await init_engine_from_config(config.database)

    from deerflow.persistence.engine import get_session_factory

    session_factory = get_session_factory()
    if session_factory is None:
        raise SystemExit("No database session factory: database.backend is memory-only, so there is no persisted run data to analyse. Point config.yaml at the sqlite/postgres backend that recorded the thread.")

    from deerflow.persistence.run import RunRepository

    return RunRepository(session_factory), make_run_event_store(getattr(config, "run_events", None))


async def _fetch(thread_id: str, *, run_store, event_store, user_id: str | None = None, event_limit: int = 100_000):
    """Run rows plus every analysed event, per run.

    ``user_id=None`` is the documented CLI opt-out for ``list_by_thread``: without it the
    repository resolves ``user_id=AUTO`` from a request-scoped contextvar no CLI ever sets.
    """
    try:
        runs = list(await run_store.list_by_thread(thread_id, user_id=user_id))
    except TypeError:  # test doubles without the keyword
        runs = list(await run_store.list_by_thread(thread_id))

    events: dict[str, list[dict]] = {}
    for row in runs:
        run_id = row.get("run_id")
        if not run_id:
            continue
        try:
            events[run_id] = list(await event_store.list_events(thread_id, run_id, event_types=_ANALYZED, limit=event_limit, user_id=user_id))
        except TypeError:
            events[run_id] = list(await event_store.list_events(thread_id, run_id, event_types=_ANALYZED, limit=event_limit))
    return runs, events


def analyze_timing(thread_id: str, runs: list[dict], events: dict[str, list[dict]], log: dict[str, Any] | None = None) -> dict:
    """Build the timing + failure report from already-fetched rows (pure, unit-testable)."""
    run_rows: list[dict] = []
    for row in runs:
        created, updated = _local(row.get("created_at")), _local(row.get("updated_at"))
        run_rows.append(
            {
                "run_id": row.get("run_id"),
                "status": row.get("status"),
                "created_at": created.isoformat() if created else None,
                "updated_at": updated.isoformat() if updated else None,
                "wall_seconds": round((updated - created).total_seconds(), 1) if created and updated else None,
                "total_tokens": row.get("total_tokens") or 0,
                "llm_call_count": row.get("llm_call_count") or 0,
                "error": row.get("error"),
            }
        )
    run_rows.sort(key=lambda r: r["created_at"] or "")

    lead_calls: list[dict] = []
    lead_failures: list[dict] = []
    gate_findings: list[dict] = []
    subagent_failures: list[dict] = []
    compactions: list[dict] = []
    step_stamps: dict[str, list[datetime]] = defaultdict(list)
    task_meta: dict[str, dict] = defaultdict(dict)
    phase_marks: dict[str, datetime] = {}
    declared_calls: list[dict] = []
    executed_call_ids: set[str] = set()
    call_args: dict[str, dict] = {}
    task_call_args: dict[str, dict] = defaultdict(dict)

    for run_id, run_events in events.items():
        for event in run_events:
            etype = event.get("event_type")
            meta = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
            content = event.get("content") if isinstance(event.get("content"), dict) else {}
            when = _local(event.get("created_at"))

            if etype == _LEAD_RESPONSE:
                usage = meta.get("usage") if isinstance(meta.get("usage"), dict) else {}
                latency_ms = meta.get("latency_ms") or 0
                tool_calls = [c for c in (content.get("tool_calls") or []) if isinstance(c, dict)]
                lead_calls.append(
                    {
                        "run_id": run_id,
                        "caller": meta.get("caller"),
                        "at": when.isoformat() if when else None,
                        "_end": when,
                        "latency_seconds": round(latency_ms / 1000, 1),
                        "input_tokens": usage.get("input_tokens", 0) or 0,
                        "output_tokens": usage.get("output_tokens", 0) or 0,
                        "cache_read": (usage.get("input_token_details") or {}).get("cache_read", 0) or 0,
                        "reasoning_tokens": (usage.get("output_token_details") or {}).get("reasoning", 0) or 0,
                        "tools": [c.get("name") for c in tool_calls],
                        "task_calls": sum(1 for c in tool_calls if c.get("name") in _TASK_TOOL_NAMES),
                    }
                )
                for c in tool_calls:
                    call_id = c.get("id")
                    if call_id:
                        declared_calls.append(
                            {
                                "run_id": run_id,
                                "at": when.isoformat() if when else None,
                                "id": call_id,
                                "name": c.get("name") or "(unnamed)",
                            }
                        )
                        call_args[call_id] = c.get("args") if isinstance(c.get("args"), dict) else {}
                    # First ``write_file`` of a phase summary marks that phase's boundary.
                    if c.get("name") in _WRITE_TOOL_NAMES and when:
                        args = c.get("args") if isinstance(c.get("args"), dict) else {}
                        if match := _PHASE_SUMMARY.search(str(args.get("path") or args.get("file_path") or "")):
                            phase_marks.setdefault(match.group(1), when)
            elif etype == _LEAD_TOOL_RESULT:
                text = content.get("content")
                if not isinstance(text, str):
                    text = json.dumps(text, ensure_ascii=False) if text is not None else ""
                if content.get("tool_call_id"):
                    executed_call_ids.add(content["tool_call_id"])
                if content.get("status") not in (None, "success") or tool_result_failed(text):
                    which = classify_failure(text, call_args.get(content.get("tool_call_id"), {}).get("command"))
                    bucket = gate_findings if which == "gate_finding" else lead_failures
                    bucket.append(
                        {
                            "at": when.isoformat() if when else None,
                            "tool": content.get("name") or "(unnamed)",
                            "status": content.get("status"),
                            "text": text[:1200],
                        }
                    )
            elif etype == "subagent.step":
                task_id = content.get("task_id") or meta.get("task_id")
                if when and task_id:
                    step_stamps[task_id].append(when)
                    # A subagent's steps prove its lead-side ``task`` tool call executed
                    # (``task_tool`` stores the tool_call_id as the task id).
                    executed_call_ids.add(str(task_id))
                if content.get("kind") == "ai":
                    # Last ai step inside the task carries the upcoming tool calls
                    # and their args — the only source of a subagent bash command
                    # (tool RESULT events store the output, never the input).
                    task_call_args[task_id] = {c.get("name"): (c.get("args") or {}) for c in (content.get("tool_calls") or []) if isinstance(c, dict)}
                elif content.get("kind") == "tool" and tool_result_failed(content.get("text")):
                    args = task_call_args.get(task_id, {}).get(content.get("tool_name") or "")
                    args = args if isinstance(args, dict) else {}  # c.get("args") can be a bare string for some tools
                    which = classify_failure(content.get("text") or "", args.get("command"))
                    bucket = gate_findings if which == "gate_finding" else subagent_failures
                    bucket.append(
                        {
                            "at": when.isoformat() if when else None,
                            "task_id": task_id,
                            "tool": content.get("tool_name") or "(unnamed)",
                            "message_index": content.get("message_index"),
                            "text": (content.get("text") or "")[:1200],
                        }
                    )
            elif etype == "subagent.start":
                task_id = content.get("task_id") or meta.get("task_id")
                if task_id:
                    task_meta[task_id]["description"] = content.get("description")
            elif etype == "subagent.end":
                task_id = content.get("task_id") or meta.get("task_id")
                if task_id:
                    task_meta[task_id]["status"] = content.get("status")
                    task_meta[task_id]["error"] = content.get("error")
                    usage = meta.get("usage") if isinstance(meta.get("usage"), dict) else {}
                    task_meta[task_id]["total_tokens"] = usage.get("total_tokens")
            elif etype == _MIDDLEWARE_SUMMARIZE:
                changes = content.get("changes") if isinstance(content.get("changes"), dict) else {}
                compactions.append(
                    {
                        "at": when.isoformat() if when else None,
                        "task_id": changes.get("task_id"),
                        "tokens_before": changes.get("tokens_before"),
                        "tokens_after": changes.get("tokens_after"),
                        "summary_chars": changes.get("summary_chars"),
                    }
                )

    # A reply that is the run's FINAL model output often ends with pending tool calls the
    # graph never runs (user interrupt / stop reason) — benign. Mid-run drops are the
    # silent-drop class (f9231297: 3 judgment tasks at 17:02:46).
    last_response_at: dict[str, str] = {}
    for call in lead_calls:
        if call.get("at") and call.get("run_id"):
            last_response_at[call["run_id"]] = max(last_response_at.get(call["run_id"], ""), call["at"])
    dropped = [d for d in declared_calls if d["id"] and d["id"] not in executed_call_ids]
    for d in dropped:
        d["terminal"] = d.get("at") == last_response_at.get(d.get("run_id"))
    return _assemble(
        thread_id,
        run_rows,
        lead_calls,
        lead_failures,
        subagent_failures,
        compactions,
        step_stamps,
        task_meta,
        phase_marks,
        log,
        gate_findings=gate_findings,
        declared_unexecuted=dropped,
    )


def _task_rows(step_stamps: dict[str, list[datetime]], task_meta: dict[str, dict], log: dict[str, Any] | None) -> tuple[list[dict], list[str]]:
    """Per-task windows, preferring log poll lines and falling back to event stamps."""
    rows: list[dict] = []
    caveats: list[str] = []
    log_tasks = (log or {}).get("tasks") or {}

    for task_id in sorted(set(step_stamps) | set(task_meta) | set(log_tasks)):
        meta = task_meta.get(task_id, {})
        entry = log_tasks.get(task_id)
        source = None
        start = end = None
        step_count = 0

        if entry and entry.get("start"):
            source = "gateway.log"
            start = entry["start"]
            steps = entry.get("steps") or []
            end = entry.get("end") or (steps[-1] if steps else start)
            step_count = len(steps)
        elif step_stamps.get(task_id):
            stamps = sorted(step_stamps[task_id])
            distinct = len({s.replace(microsecond=0) for s in stamps})
            # Pre-2026-08-13 events carry flush time, not step time. Say so instead of
            # reporting per-step numbers that are not measurements.
            if len(stamps) >= 6 and distinct / len(stamps) < _FLUSH_TIME_SUSPICION_RATIO:
                source = "run_events (⚠ flush-time stamps)"
                caveats.append(f"task {task_id}: {len(stamps)} steps share only {distinct} distinct timestamps — these are batch-flush times, not step times (run predates the created_at fix). Per-step timing needs --log.")
            else:
                source = "run_events"
            start, end, step_count = stamps[0], stamps[-1], len(stamps)

        if start is None:
            continue
        wall = (end - start).total_seconds()
        rows.append(
            {
                "task_id": task_id,
                "description": meta.get("description"),
                "status": meta.get("status") or (entry or {}).get("outcome"),
                "started_at": start.isoformat(),
                "ended_at": end.isoformat(),
                "wall_seconds": round(wall, 1),
                "steps": step_count,
                "seconds_per_step": round(wall / step_count, 1) if step_count else None,
                "total_tokens": meta.get("total_tokens"),
                "timing_source": source,
                "_start": start,
                "_end": end,
            }
        )
    rows.sort(key=lambda r: r["_start"])
    return rows, caveats


def _dispatch_pattern(lead_calls: list[dict]) -> dict:
    """How many ``task`` tool calls the lead packed into each AI reply.

    The framework fans out concurrent tool calls per reply (``Send`` per tool call), so a
    reply carrying 3 tasks runs 3 subagents in parallel; one carrying 1 runs them serially.
    A histogram concentrated on 1 — with independent work available — is the smoking gun
    for the serial-dispatch habit, independent of how fast any single task is.
    """
    counts: Counter[int] = Counter()
    dispatching = 0
    for call in lead_calls:
        if call.get("caller") != "lead_agent":
            continue
        count = call.get("task_calls") or 0
        if count:
            counts[min(count, 4)] += 1
            dispatching += 1
    per_reply = {str(n): counts.get(n, 0) for n in (1, 2, 3)}
    per_reply["4+"] = counts.get(4, 0)
    total = sum(per_reply.values())
    per_reply["share_of_dispatch_replies"] = {label: round(value / total * 100, 1) if total else None for label, value in per_reply.items()}
    return {
        "dispatch_replies": dispatching,
        "replies_with_1_task": per_reply["1"],
        "replies_with_2_tasks": per_reply["2"],
        "replies_with_3_tasks": per_reply["3"],
        "replies_with_4plus_tasks": per_reply["4+"],
    }


def _concurrency_watermark(task_rows: list[dict]) -> dict:
    """In-flight subagent count across the session — measured, not implied.

    Sweeps the task windows' boundaries. ``avg_while_busy`` weights each stretch by how
    many tasks were in flight, so 1.0 = fully serial, 3.0 = the concurrency budget held
    throughout. A serial session wastes wall clock even when every task is fast.
    """
    points: list[tuple[datetime, int]] = []
    for row in task_rows:
        points.append((row["_start"], +1))
        points.append((row["_end"], -1))
    # Ends (``-1``) sort before starts (``+1``) at the same instant, so a task finishing at
    # t does not inflate the count for one starting at t.
    points.sort(key=lambda p: (p[0], p[1]))

    running = 0
    peak = 0
    peak_at: datetime | None = None
    peak_tasks: list[str] = []
    weighted = 0.0
    previous: datetime | None = None
    for when, delta in points:
        if previous is not None and when > previous and running:
            weighted += (when - previous).total_seconds() * running
        running += delta
        if running > peak:
            peak = running
            peak_at = when
        previous = when

    # Who was in flight at the peak — names the tasks that actually overlapped.
    for row in task_rows:
        if peak_at is not None and row["_start"] <= peak_at <= row["_end"]:
            peak_tasks.append(row["task_id"])

    busy_union = _span(merge_intervals([(row["_start"], row["_end"]) for row in task_rows]))
    return {
        "task_count": len(task_rows),
        "max_concurrent": peak,
        "avg_while_busy": round(weighted / busy_union, 2) if busy_union else None,
        "peak_at": peak_at.isoformat() if peak_at else None,
        "tasks_at_peak": sorted(peak_tasks)[:6],
    }


def _assemble(
    thread_id: str,
    run_rows: list[dict],
    lead_calls: list[dict],
    lead_failures: list[dict],
    subagent_failures: list[dict],
    compactions: list[dict],
    step_stamps: dict[str, list[datetime]],
    task_meta: dict[str, dict],
    phase_marks: dict[str, datetime],
    log: dict[str, Any] | None,
    gate_findings: list[dict] | None = None,
    declared_unexecuted: list[dict] | None = None,
) -> dict:
    gate_findings = gate_findings or []
    declared_unexecuted = declared_unexecuted or []
    task_rows, caveats = _task_rows(step_stamps, task_meta, log)

    # Lead busy = union of [end - latency, end]. A union, not a sum: concurrent calls would
    # otherwise inflate the total past the wall clock.
    lead_windows = merge_intervals([(call["_end"] - timedelta(seconds=call["latency_seconds"]), call["_end"]) for call in lead_calls if call["_end"] and call["latency_seconds"]])
    subagent_windows = merge_intervals([(row["_start"], row["_end"]) for row in task_rows])

    starts = [_local(r["created_at"]) for r in run_rows if r["created_at"]]
    ends = [_local(r["updated_at"]) for r in run_rows if r["updated_at"]]
    session_start, session_end = (min(starts) if starts else None), (max(ends) if ends else None)
    session_seconds = (session_end - session_start).total_seconds() if session_start and session_end else 0.0

    lead_busy, subagent_busy = _span(lead_windows), _span(subagent_windows)
    overlap = _overlap(lead_windows, subagent_windows)
    combined = lead_busy + subagent_busy - overlap

    # Gaps between consecutive runs are human think time, not system latency.
    idle_gaps = []
    for previous, following in zip(run_rows, run_rows[1:]):
        prev_end, next_start = _local(previous["updated_at"]), _local(following["created_at"])
        if prev_end and next_start and next_start > prev_end:
            idle_gaps.append({"after_run": previous["run_id"], "seconds": round((next_start - prev_end).total_seconds(), 1)})

    by_caller: dict[str, dict] = {}
    for caller, calls in _group_by(lead_calls, "caller").items():
        latencies = sorted(c["latency_seconds"] for c in calls)
        outputs = sum(c["output_tokens"] for c in calls)
        by_caller[caller or "(unknown)"] = {
            "calls": len(calls),
            "total_seconds": round(sum(latencies), 1),
            "avg_seconds": round(sum(latencies) / len(latencies), 1),
            "median_seconds": latencies[len(latencies) // 2],
            "max_seconds": latencies[-1],
            "output_tokens": outputs,
            "reasoning_share": round(sum(c["reasoning_tokens"] for c in calls) / outputs * 100, 1) if outputs else None,
            "output_tokens_per_second": round(outputs / sum(latencies), 1) if sum(latencies) else None,
        }

    empty_summaries = (log or {}).get("empty_summaries") or []
    applied = [c for c in compactions if (c.get("summary_chars") or 0) > 0]
    attempts = len(compactions) + len(empty_summaries)

    dispatch = _dispatch_pattern(lead_calls)
    concurrency = _concurrency_watermark(task_rows)
    marks = [
        {
            "phase": phase,
            "at": when.isoformat(),
            "in_flight_at_boundary": sum(1 for r in task_rows if r["_start"] <= when <= r["_end"]),
        }
        for phase, when in sorted(phase_marks.items(), key=lambda kv: kv[1])
    ]

    return {
        "thread_id": thread_id,
        "display_timezone": "UTC+08:00",
        "totals": {
            "session_seconds": round(session_seconds, 1),
            "session_minutes": round(session_seconds / 60, 1),
            "run_count": len(run_rows),
            "task_count": len(task_rows),
            "lead_llm_busy_seconds": round(lead_busy, 1),
            "lead_llm_busy_share": round(lead_busy / session_seconds * 100, 1) if session_seconds else None,
            "subagent_busy_seconds": round(subagent_busy, 1),
            "subagent_busy_share": round(subagent_busy / session_seconds * 100, 1) if session_seconds else None,
            # Near-zero overlap means the lead is just waiting in its poll loop: the session
            # is serial, and parallelising the dispatch is the lever, not faster tools.
            "lead_subagent_overlap_seconds": round(overlap, 1),
            "combined_busy_seconds": round(combined, 1),
            "combined_busy_share": round(combined / session_seconds * 100, 1) if session_seconds else None,
            "unaccounted_seconds": round(session_seconds - combined, 1),
            "idle_between_runs_seconds": round(sum(g["seconds"] for g in idle_gaps), 1),
            "lead_tool_failures": len(lead_failures),
            "subagent_tool_failures": len(subagent_failures),
            "tool_failures_total": len(lead_failures) + len(subagent_failures),
            "gate_findings": len(gate_findings),
            "declared_unexecuted": len(declared_unexecuted),
            "compaction_attempts": attempts,
            "compactions_applied": len(applied),
            "compactions_empty_summary": len(empty_summaries),
            "empty_summary_share": round(len(empty_summaries) / attempts * 100, 1) if attempts else None,
        },
        "runs": run_rows,
        "idle_gaps": idle_gaps,
        "latency_by_caller": by_caller,
        "latency_vs_context": _latency_buckets(lead_calls),
        "slowest_lead_calls": [{k: v for k, v in c.items() if not k.startswith("_")} for c in sorted(lead_calls, key=lambda c: -c["latency_seconds"])[:10]],
        "tasks": [{k: v for k, v in r.items() if not k.startswith("_")} for r in task_rows],
        "dispatch": dispatch,
        "concurrency": concurrency,
        "phase_marks": marks,
        "failures": {
            "lead": lead_failures,
            "subagent": subagent_failures,
            "by_tool": dict(Counter([f["tool"] for f in lead_failures] + [f["tool"] for f in subagent_failures])),
        },
        "gate_findings": gate_findings,
        "declared_unexecuted": declared_unexecuted,
        "empty_summaries": empty_summaries,
        "caveats": caveats,
    }


def _group_by(rows: list[dict], key: str) -> dict[Any, list[dict]]:
    grouped: dict[Any, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[row.get(key)].append(row)
    return grouped


def _latency_buckets(lead_calls: list[dict]) -> list[dict]:
    """Latency against input size — separates "context too big" from "reasoning too long".

    If latency tracks output/reasoning rather than input, trimming context buys nothing and
    the reasoning budget is the lever. Session ``a7c19ea1``: 85.4% cache hit, and the
    slowest call had a mid-size input with 10,214 reasoning tokens.
    """
    bounds = [(0, 50_000), (50_000, 100_000), (100_000, 150_000), (150_000, 200_000), (200_000, None)]
    buckets = []
    for low, high in bounds:
        selected = [c for c in lead_calls if c["caller"] == "lead_agent" and low <= c["input_tokens"] < (high or float("inf"))]
        if not selected:
            continue
        latencies = [c["latency_seconds"] for c in selected]
        inputs = sum(c["input_tokens"] for c in selected)
        outputs = sum(c["output_tokens"] for c in selected)
        buckets.append(
            {
                "input_tokens_from": low,
                "input_tokens_to": high,
                "calls": len(selected),
                "avg_latency_seconds": round(sum(latencies) / len(latencies), 1),
                "total_latency_seconds": round(sum(latencies), 1),
                "avg_output_tokens": round(outputs / len(selected)),
                "reasoning_share": round(sum(c["reasoning_tokens"] for c in selected) / outputs * 100, 1) if outputs else None,
                "cache_hit_share": round(sum(c["cache_read"] for c in selected) / inputs * 100, 1) if inputs else None,
            }
        )
    return buckets


# ── rendering --------------------------------------------------------------------


def _hhmmss(iso: str | None) -> str:
    return iso[11:19] if iso and len(iso) >= 19 else "--:--:--"


def render(report: dict, *, failures_only: bool = False) -> str:
    totals = report["totals"]
    lines: list[str] = []

    if not failures_only:
        lines += [
            f"thread {report['thread_id']}  ({report['display_timezone']}, matching gateway.log)",
            f"  session  {totals['session_minutes']} min ({totals['session_seconds']}s)  runs={totals['run_count']}  subagent tasks={totals['task_count']}",
        ]
        for row in report["runs"]:
            flag = "  ⚠" if row["status"] not in ("success", "completed") else ""
            lines.append(f"    {str(row['run_id'])[:8]}  {row['status']:<12} {_hhmmss(row['created_at'])} → {_hhmmss(row['updated_at'])}  {row['wall_seconds']}s  tokens={row['total_tokens']:,}{flag}")
        if report["idle_gaps"]:
            lines.append(f"    idle between runs: {totals['idle_between_runs_seconds']}s (human think time, not system latency)")

        lines += [
            "",
            "  where the wall clock went",
            f"    lead LLM busy      {totals['lead_llm_busy_seconds']:>9}s  {totals['lead_llm_busy_share'] or 0:>5}%",
            f"    subagent busy      {totals['subagent_busy_seconds']:>9}s  {totals['subagent_busy_share'] or 0:>5}%",
            f"    overlap            {totals['lead_subagent_overlap_seconds']:>9}s  ({'real parallelism' if totals['lead_subagent_overlap_seconds'] > totals['session_seconds'] * 0.05 else 'lead just waits in the poll loop → serial'})",
            f"    combined (union)   {totals['combined_busy_seconds']:>9}s  {totals['combined_busy_share'] or 0:>5}%",
            f"    unaccounted        {totals['unaccounted_seconds']:>9}s  (tool exec, IO, idle)",
        ]

        if report["dispatch"]["dispatch_replies"]:
            dispatch = report["dispatch"]
            lines += [
                "",
                "  task dispatch (tasks packed per AI reply — 1 = serial habit, 3 = budget held)",
                f"    dispatching replies  {dispatch['dispatch_replies']}   "
                f"1-task={dispatch['replies_with_1_task']}  2-tasks={dispatch['replies_with_2_tasks']}  "
                f"3-tasks={dispatch['replies_with_3_tasks']}  4+={dispatch['replies_with_4plus_tasks']}",
            ]

        concurrency = report["concurrency"]
        if concurrency["max_concurrent"]:
            lines += [
                "",
                "  subagent concurrency (max in-flight subagents, measured from task windows)",
                f"    max_concurrent={concurrency['max_concurrent']}  avg_while_busy={concurrency['avg_while_busy']}  peak at {_hhmmss(concurrency['peak_at'])}  tasks=",
                *[f"      {task}" for task in concurrency["tasks_at_peak"]],
            ]
            if concurrency["max_concurrent"] == 1 and concurrency["task_count"] >= 6:
                lines.append(
                    f"    ⛔ serial task dispatch: {concurrency['task_count']} subagent tasks ran strictly one-at-a-time (max_concurrent=1). Independent tasks must be batched 3 per AI reply — dispatch is the lever, not tool speed."
                )

        if report["phase_marks"]:
            lines += ["", "  phase boundaries (first phase*_summary.json write; in-flight tasks at the boundary)"]
            for mark in report["phase_marks"]:
                flag = "  ⚠ global barrier — model waited for every task to finish" if mark["in_flight_at_boundary"] == 0 else ""
                lines.append(f"    phase{mark['phase']:<6} {_hhmmss(mark['at'])}  in_flight={mark['in_flight_at_boundary']}{flag}")

        if report["latency_by_caller"]:
            lines += ["", "  LLM latency by caller (instrumented callers only — subagent calls carry none)"]
            for caller, stats in sorted(report["latency_by_caller"].items(), key=lambda kv: -kv[1]["total_seconds"]):
                share = f"reasoning {stats['reasoning_share']}%" if stats["reasoning_share"] is not None else ""
                lines.append(f"    {caller:<22} n={stats['calls']:<4} sum={stats['total_seconds']:>8}s  avg={stats['avg_seconds']:>6}s  p50={stats['median_seconds']:>6}s  max={stats['max_seconds']:>7}s  {share}")

        if report["latency_vs_context"]:
            lines += ["", "  latency vs input size (is it context-bound or reasoning-bound?)"]
            for bucket in report["latency_vs_context"]:
                upper = f"{bucket['input_tokens_to'] // 1000}k" if bucket["input_tokens_to"] else "∞"
                lines.append(
                    f"    in∈[{bucket['input_tokens_from'] // 1000:>3}k,{upper:>4})  n={bucket['calls']:<4} avg={bucket['avg_latency_seconds']:>6}s  "
                    f"out avg={bucket['avg_output_tokens']:>6}  reasoning={bucket['reasoning_share']}%  cache={bucket['cache_hit_share']}%"
                )

        if report["tasks"]:
            lines += ["", "  subagent tasks"]
            for row in report["tasks"]:
                tokens = f"{row['total_tokens']:,}" if row["total_tokens"] else "—"
                lines.append(
                    f"    {row['task_id'][:40]:<42} {_hhmmss(row['started_at'])}→{_hhmmss(row['ended_at'])} {row['wall_seconds']:>7}s  "
                    f"steps={row['steps']:>3}  s/step={row['seconds_per_step'] or '—':>5}  tok={tokens:>11}  {row['status'] or '?'}"
                )

        if totals["compaction_attempts"]:
            lines += [
                "",
                f"  compaction  attempts={totals['compaction_attempts']}  applied={totals['compactions_applied']}  "
                f"empty_summary={totals['compactions_empty_summary']}" + (f" ({totals['empty_summary_share']}% wasted — a full model call that changed nothing)" if totals["empty_summary_share"] else ""),
            ]

    if report.get("declared_unexecuted"):
        mid_run = [d for d in report["declared_unexecuted"] if not d.get("terminal")]
        terminal = [d for d in report["declared_unexecuted"] if d.get("terminal")]
        lines += [
            "",
            f"  ⛔ declared but never executed  total={totals['declared_unexecuted']}  "
            f"(mid-run drops={len(mid_run)} — the silent-drop signature, e.g. f9231297's 3 judgment tasks at 17:02:46;  "
            f"run-terminal={len(terminal)} — pending calls of a run's final reply, benign)",
        ]
        for dropped in mid_run[:10]:
            lines.append(f"    {_hhmmss(dropped['at'])} {dropped['name']:<20} id={str(dropped['id'])[:24]}")
        for dropped in terminal[:5]:
            lines.append(f"    {_hhmmss(dropped['at'])} {dropped['name']:<20} id={str(dropped['id'])[:24]}  (run-terminal)")

    lines += ["", f"  tool failures  total={totals['tool_failures_total']}  (lead={totals['lead_tool_failures']}, subagent={totals['subagent_tool_failures']})"]
    if totals.get("gate_findings"):
        lines.append(f"  gate findings   total={totals['gate_findings']}  (gates correctly reporting problems — NOT tool failures; see plan 2026-08-24 f9231297 T7)")
        first_finding = next(
            (ln.strip() for finding in report["gate_findings"] for ln in finding["text"].splitlines() if ln.strip()),
            "",
        )
        if first_finding:
            lines.append(f"    example: {first_finding[:100]}")
    if report["failures"]["by_tool"]:
        lines.append("    by tool: " + "  ".join(f"{name}={count}" for name, count in sorted(report["failures"]["by_tool"].items(), key=lambda kv: -kv[1])))
    for failure in report["failures"]["lead"]:
        first = next((ln.strip() for ln in failure["text"].splitlines() if ln.strip()), "")
        lines.append(f"    {_hhmmss(failure['at'])} LEAD  {failure['tool']:<20} {first[:96]}")
    for failure in report["failures"]["subagent"]:
        first = next((ln.strip() for ln in failure["text"].splitlines() if ln.strip()), "")
        lines.append(f"    {_hhmmss(failure['at'])} {str(failure['task_id'])[:26]:<26} {failure['tool']:<20} {first[:80]}")

    for caveat in report["caveats"]:
        lines.append(f"  ⚠ {caveat}")
    if not (report.get("empty_summaries") or report["tasks"]) and not report["caveats"]:
        lines.append("  note: no --log given, so subagent step timing comes from run_events only.")

    return "\n".join(lines)


async def _amain(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("thread_id")
    parser.add_argument(
        "--log",
        help="gateway.log path (default: ../logs/gateway.log). The ONLY source of subagent step wall clock for runs recorded before the created_at fix, and of aborted-compaction lines.",
    )
    parser.add_argument("--output", help="write the full JSON report here")
    parser.add_argument("--failures-only", action="store_true", help="print just the failed tool calls")
    args = parser.parse_args(argv)

    from deerflow.config import get_app_config

    run_store, event_store = await _open_stores(get_app_config())
    runs, events = await _fetch(args.thread_id, run_store=run_store, event_store=event_store)
    if not runs:
        print(f"thread {args.thread_id}: no runs found. Check the thread id and that config.yaml points at the recording database.")
        return 1

    log_path = Path(args.log) if args.log else Path(__file__).resolve().parents[2] / "logs" / "gateway.log"
    log_data = None
    if log_path.exists():
        starts = [_local(r.get("created_at")) for r in runs if r.get("created_at")]
        ends = [_local(r.get("updated_at")) for r in runs if r.get("updated_at")]
        window = None
        if starts and ends:
            # Widen slightly: a task's first poll line can precede the run row's own stamp.
            window = (min(s for s in starts if s) - timedelta(minutes=2), max(e for e in ends if e) + timedelta(minutes=2))
        log_data = parse_gateway_log(log_path, window)
    elif args.log:
        print(f"⚠ log not found: {log_path} — subagent step timing will come from run_events only.", file=sys.stderr)

    report = analyze_timing(args.thread_id, runs, events, log_data)
    print(render(report, failures_only=args.failures_only))

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
