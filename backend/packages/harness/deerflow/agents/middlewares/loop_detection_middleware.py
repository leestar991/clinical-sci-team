"""Middleware to detect and break repetitive tool call loops.

P0 safety: prevents the agent from calling the same tool with the same
arguments indefinitely until the recursion limit kills the run.

Detection strategy:
  1. After each model response, hash the tool calls (name + args).
  2. Track recent hashes in a sliding window.
  3. If the same hash appears >= warn_threshold times, queue a
     "you are repeating yourself — wrap up" warning for the current
     thread/run. The warning is **injected at the next model call** (in
     ``wrap_model_call``) as a ``HumanMessage`` appended to the message
     list, *after* all ToolMessage responses to the previous
     AIMessage(tool_calls).
  4. If it appears >= hard_limit times, strip all tool_calls from the
     response so the agent is forced to produce a final text answer.
  5. With ``mutation_reset`` on, an identical call re-issued after the world
     changed does not count toward the limits: a state-mutating call set
     (``write_file`` / ``str_replace`` / ``apply_json_patches`` / a
     write-shaped bash command) advances a per-scope *mutation epoch*, and a
     hash whose epoch moved since its last occurrence restarts at 1. The
     reset ignores bumps the hash itself caused (``rm -rf x`` x5 still
     hard-stops) and is bounded by ``mutation_reset_budget`` so two
     alternating writers cannot reset each other forever.

Why the warning is injected at ``wrap_model_call`` instead of
``after_model``:

  ``after_model`` fires immediately after the model emits an
  ``AIMessage`` that may carry ``tool_calls``. The tools node has not
  run yet, so no matching ``ToolMessage`` exists in the history. Any
  message we add here lands *between* the assistant's tool_calls and
  their responses. OpenAI/Moonshot reject the next request with
  ``"tool_call_ids did not have response messages"`` because their
  validators require the assistant's tool_calls to be followed
  immediately by tool messages. Anthropic also disallows mid-stream
  ``SystemMessage``. By deferring the warning to ``wrap_model_call``,
  every prior ToolMessage is already present in the request's message
  list and the warning is appended at the end — pairing intact, no
  ``AIMessage`` semantics are mutated.

Queued warnings are intentionally transient. If a run ends before the
next model request drains a queued warning, ``after_agent`` drops it
instead of carrying it into a later invocation for the same thread. The
hard-stop path still forces termination when the configured safety limit
is reached.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from collections import OrderedDict, defaultdict
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING, override

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelCallResult, ModelRequest, ModelResponse
from langchain_core.messages import HumanMessage
from langgraph.runtime import Runtime

if TYPE_CHECKING:
    from deerflow.config.loop_detection_config import LoopDetectionConfig

logger = logging.getLogger(__name__)

# Defaults — can be overridden via constructor
_DEFAULT_WARN_THRESHOLD = 3  # inject warning after 3 identical calls
_DEFAULT_HARD_LIMIT = 5  # force-stop after 5 identical calls
_DEFAULT_WINDOW_SIZE = 20  # track last N tool calls
#: Cap on distinct call hashes tracked per scope when ``cumulative_counting`` is on.
#: The window bounds itself; a monotonic counter does not, so a long task with many
#: distinct calls would grow unbounded. FIFO eviction only drops the oldest hashes,
#: which are by definition the ones that stopped repeating.
_MAX_CUMULATIVE_HASHES = 512
_DEFAULT_MAX_TRACKED_THREADS = 100  # LRU eviction limit
_DEFAULT_TOOL_FREQ_WARN = 30  # warn after 30 calls to the same tool type
_DEFAULT_TOOL_FREQ_HARD_LIMIT = 50  # force-stop after 50 calls to the same tool type
_MAX_PENDING_WARNINGS_PER_RUN = 4

# ⛔ REMOVED (2026-08-19): the ``verification_patterns`` exemption.
#
# It carried a hardcoded list of gate-script filenames — ``check_track_structure.py``,
# ``check_judgment_structure.py``, ``uncertain_recheck.py``, ``ocr_coverage.py`` and others —
# so that re-running one after each repair attempt got a wider budget (warn 8 / hard 12)
# than the ordinary "same call 3 times" rule.
#
# Why it had to go, even though deleting it makes the known false-positive WORSE (replaying
# thread ``98d27624`` off its persisted ``subagent.step`` rows: 6 warnings before the
# exemption was widened, 2 after, **8 with the mechanism removed**):
#
#   1. Every one of those filenames lives under ``skills/custom/``, which is gitignored
#      (``.gitignore`` line 40). This publishable framework package (``deerflow-harness``,
#      see ``packages/harness/pyproject.toml``) hardcoded seven paths to files not present in
#      the repository. Nothing could pin them; renaming a script silently degraded the
#      detector, and the symptom — "loop detection is misfiring again" — pointed nowhere near
#      here.
#   2. It inverted the harness/app boundary the repo enforces elsewhere
#      (``tests/test_harness_boundary.py``): a framework safety threshold must not depend on
#      which business skill a deployment happens to install.
#   3. The match was a bare substring test against the whole command, with no shell parsing,
#      so appending ``; sha256sum /a`` (or naming any listed script anywhere in the line)
#      relabelled an arbitrary command as "verification" and bought it the wider budget. The
#      "every call must match" guard only covered *separate tool_calls*; a single command
#      chained with ``;`` walked straight through.
#
# The false positive it was patching is real, but its root cause is NOT a missing whitelist:
# ``_stable_tool_key`` reduces a bash call to its ``command`` string alone, so *any* idempotent
# command is byte-identical on every repeat and trips a pure repeat-counter by construction.
# The whitelist treated that as a naming problem and required one more business string in the
# framework per newly-discovered idempotent command. Fixing it belongs in ``_stable_tool_key``
# (or in a skill-declared capability — ``Skill`` already carries ``allowed_tools``), not here.
#
# ⛔ Do not reintroduce a filename list in this module. See
# ``docs/eligibility-screener-gate-loop-optimization-changelog.md``.

#: Tools whose only purpose is to mutate state. Exact set - do not grow it
#: casually: a tool classified here resets other hashes' repeat counters, so
#: over-classification weakens loop detection.
_MUTATING_TOOLS = frozenset({"write_file", "str_replace", "apply_json_patches"})

#: Shell primitives that write state, matched as whole whitespace-separated
#: tokens. Generic Unix knowledge only - deliberately NOT business skill
#: filenames (see the REMOVED block above).
_BASH_MUTATOR_TOKENS = frozenset(
    {
        "mv",
        "cp",
        "rm",
        "mkdir",
        "rmdir",
        "touch",
        "tee",
        "truncate",
        "chmod",
        "chown",
        "ln",
        "dd",
        "install",
        "patch",
        "rsync",
        "shred",
        "unlink",
    }
)

#: Default cap on how many times one hash may consume a mutation reset.
#: Bounds the alternating-writer hole (two mutating hashes can reset each
#: other forever without a budget) while staying above what legitimate
#: repair->verify cycles need (observed: 2-3 per task).
_DEFAULT_MUTATION_RESET_BUDGET = 8


def _bash_command_mutates(command: str) -> bool:
    """Heuristic: does this shell command write state?

    Covers file redirects (``> file`` / ``>> file``, excluding descriptor
    duplication like ``2>&1`` and the ``/dev/null`` sink) and whole-token
    matches against :data:`_BASH_MUTATOR_TOKENS`, plus ``sed`` only when it
    carries ``-i``.

    Deliberately conservative: an unclassifiable command (``python3
    script.py``) is treated as NON-mutating, so its repeats keep counting.
    The asymmetry matters - missing a write only delays a reset (a false
    warning, Layer 2 still backstops), while inventing one lets real loops
    reset their own counters away (a missed hard stop).
    """
    if not command:
        return False
    tokens = command.split()
    if any(tok in _BASH_MUTATOR_TOKENS for tok in tokens):
        return True
    if "sed" in tokens and re.search(r"(?<!\S)-i\b", command):
        return True
    for match in re.finditer(r">{1,2}", command):
        rest = command[match.end() :].lstrip()
        if rest.startswith("&"):
            continue  # >&2 / >>&1: descriptor duplication, not a file write
        target = re.match(r"[^\s;|&]+", rest)
        if target and target.group(0) != "/dev/null":
            return True
    return False


def _call_set_mutates(tool_calls: list[dict]) -> bool:
    """Does this tool-call set contain any state-mutating call?"""
    for tc in tool_calls:
        name = tc.get("name", "")
        if name in _MUTATING_TOOLS:
            return True
        if name == "bash":
            args, _ = _normalize_tool_call_args(tc.get("args", {}))
            command = args.get("command") or args.get("cmd")
            if isinstance(command, str) and _bash_command_mutates(command):
                return True
    return False


@dataclass
class _MutationEntry:
    """Per-hash bookkeeping for the mutation-aware reset.

    ``others_at_last_seen`` is the value of "epoch bumps caused by other
    hashes" when this hash last occurred; ``own_bumps`` counts the bumps this
    hash itself caused (self-exclusion); ``resets_used`` consumes the budget.
    """

    others_at_last_seen: int = 0
    resets_used: int = 0
    own_bumps: int = 0


def _normalize_tool_call_args(raw_args: object) -> tuple[dict, str | None]:
    """Normalize tool call args to a dict plus an optional fallback key.

    Some providers serialize ``args`` as a JSON string instead of a dict.
    We defensively parse those cases so loop detection does not crash while
    still preserving a stable fallback key for non-dict payloads.
    """
    if isinstance(raw_args, dict):
        return raw_args, None

    if isinstance(raw_args, str):
        try:
            parsed = json.loads(raw_args)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}, raw_args

        if isinstance(parsed, dict):
            return parsed, None
        return {}, json.dumps(parsed, sort_keys=True, default=str)

    if raw_args is None:
        return {}, None

    return {}, json.dumps(raw_args, sort_keys=True, default=str)


def _stable_tool_key(name: str, args: dict, fallback_key: str | None) -> str:
    """Derive a stable key from salient args without overfitting to noise."""
    if name == "read_file" and fallback_key is None:
        path = args.get("path") or ""
        start_line = args.get("start_line")
        end_line = args.get("end_line")

        try:
            start_line = int(start_line) if start_line is not None else 1
        except (TypeError, ValueError):
            start_line = 1
        try:
            end_line = int(end_line) if end_line is not None else start_line
        except (TypeError, ValueError):
            end_line = start_line

        start_line, end_line = sorted((start_line, end_line))
        # Exact normalized range, not a 200-line bucket. The bucket's one
        # legitimate use - tolerating a near-identical re-read after the file
        # changed - is covered by the mutation-aware reset (a write between
        # occurrences restarts the counter). What the bucket added on top was
        # only harm: distinct intents ([1-220] vs [160-400] vs [90-115])
        # collapsed onto one key and tripped the repeat counter in
        # mutation-free stretches. A drifting-range loop ([1-100] -> [1-101]
        # -> ...) escapes this layer by design; Layer 2's per-tool frequency
        # budget is the backstop for it.
        return f"{path}:{max(start_line, 1)}-{max(end_line, 1)}"

    # write_file / str_replace / apply_json_patches are content-sensitive: the same path may
    # be updated with different payloads during iteration. Using only salient fields (path)
    # can collapse distinct calls, so we hash full args to reduce false positives — for
    # apply_json_patches that is essential, since every call to it carries only `path` plus
    # a `patches` list and would otherwise look identical to the previous batch.
    if name in {"write_file", "str_replace", "apply_json_patches"}:
        if fallback_key is not None:
            return fallback_key
        return json.dumps(args, sort_keys=True, default=str)

    salient_fields = ("path", "url", "query", "command", "pattern", "glob", "cmd")
    stable_args = {field: args[field] for field in salient_fields if args.get(field) is not None}
    if stable_args:
        return json.dumps(stable_args, sort_keys=True, default=str)

    if fallback_key is not None:
        return fallback_key

    return json.dumps(args, sort_keys=True, default=str)


def _hash_tool_calls(tool_calls: list[dict]) -> str:
    """Deterministic hash of a set of tool calls (name + stable key).

    This is intended to be order-independent: the same multiset of tool calls
    should always produce the same hash, regardless of their input order.
    """
    # Normalize each tool call to a stable (name, key) structure.
    normalized: list[str] = []
    for tc in tool_calls:
        name = tc.get("name", "")
        args, fallback_key = _normalize_tool_call_args(tc.get("args", {}))
        key = _stable_tool_key(name, args, fallback_key)

        normalized.append(f"{name}:{key}")

    # Sort so permutations of the same multiset of calls yield the same ordering.
    normalized.sort()
    blob = json.dumps(normalized, sort_keys=True, default=str)
    return hashlib.md5(blob.encode()).hexdigest()[:12]


_WARNING_MSG = "[LOOP DETECTED] You are repeating the same tool calls. Stop calling tools and produce your final answer now. If you cannot complete the task, summarize what you accomplished so far."

_TOOL_FREQ_WARNING_MSG = (
    "[LOOP DETECTED] You have called {tool_name} {count} times without producing a final answer. Stop calling tools and produce your final answer now. If you cannot complete the task, summarize what you accomplished so far."
)

_HARD_STOP_MSG = "[FORCED STOP] Repeated tool calls exceeded the safety limit. Producing final answer with results collected so far."

_TOOL_FREQ_HARD_STOP_MSG = "[FORCED STOP] Tool {tool_name} called {count} times — exceeded the per-tool safety limit. Producing final answer with results collected so far."


class LoopDetectionMiddleware(AgentMiddleware[AgentState]):
    """Detects and breaks repetitive tool call loops.

    Threshold parameters are validated upstream by :class:`LoopDetectionConfig`;
    construct via :meth:`from_config` to ensure values pass Pydantic validation.

    Args:
        warn_threshold: Number of identical tool call sets before injecting
            a warning message. Default: 3.
        hard_limit: Number of identical tool call sets before stripping
            tool_calls entirely. Default: 5.
        window_size: Size of the sliding window for tracking calls.
            Default: 20.
        max_tracked_threads: Maximum number of threads to track before
            evicting the least recently used. Default: 100.
        tool_freq_warn: Number of calls to the same tool *type* (regardless
            of arguments) before injecting a frequency warning. Catches
            cross-file read loops that hash-based detection misses.
            Default: 30.
        tool_freq_hard_limit: Number of calls to the same tool type before
            forcing a stop. Default: 50.
        tool_freq_overrides: Per-tool overrides for frequency thresholds,
            keyed by tool name. Each value is a ``(warn, hard_limit)`` tuple
            that replaces ``tool_freq_warn`` / ``tool_freq_hard_limit`` for
            that specific tool. Tools not listed here fall back to the global
            thresholds. Useful for raising limits on intentionally
            high-frequency tools (e.g. ``bash`` in batch pipelines) without
            weakening protection on all other tools. Default: ``None``
            (no overrides).
        cumulative_counting: Count identical tool-call sets cumulatively per
            scope instead of only within the sliding window. Needed when repeats
            are spaced further apart than ``window_size``.
        mutation_reset: Restart a hash's repeat counter when a *different*
            call set mutated state since its last occurrence (see step 5 in
            the module docstring). Default: ``False`` (historical behaviour).
        mutation_reset_budget: Cap on resets consumed per hash. Default: 8.
    """

    def __init__(
        self,
        warn_threshold: int = _DEFAULT_WARN_THRESHOLD,
        hard_limit: int = _DEFAULT_HARD_LIMIT,
        window_size: int = _DEFAULT_WINDOW_SIZE,
        max_tracked_threads: int = _DEFAULT_MAX_TRACKED_THREADS,
        tool_freq_warn: int = _DEFAULT_TOOL_FREQ_WARN,
        tool_freq_hard_limit: int = _DEFAULT_TOOL_FREQ_HARD_LIMIT,
        tool_freq_overrides: dict[str, tuple[int, int]] | None = None,
        cumulative_counting: bool = False,
        mutation_reset: bool = False,
        mutation_reset_budget: int = _DEFAULT_MUTATION_RESET_BUDGET,
    ):
        super().__init__()
        self.warn_threshold = warn_threshold
        self.hard_limit = hard_limit
        self.window_size = window_size
        self.max_tracked_threads = max_tracked_threads
        self.tool_freq_warn = tool_freq_warn
        self.tool_freq_hard_limit = tool_freq_hard_limit
        self.cumulative_counting = cumulative_counting
        self.mutation_reset = mutation_reset
        self.mutation_reset_budget = mutation_reset_budget
        self._tool_freq_overrides: dict[str, tuple[int, int]] = tool_freq_overrides or {}
        self._lock = threading.Lock()
        self._history: OrderedDict[str, list[str]] = OrderedDict()
        self._cumulative: dict[str, OrderedDict[str, int]] = defaultdict(OrderedDict)
        self._warned: dict[str, set[str]] = defaultdict(set)
        self._tool_freq: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self._tool_freq_warned: dict[str, set[str]] = defaultdict(set)
        # Mutation-aware reset state, per tracking scope.
        self._mutation_epoch: dict[str, int] = defaultdict(int)
        self._mut_state: dict[str, OrderedDict[str, _MutationEntry]] = defaultdict(OrderedDict)
        # Per-thread/run queue of warnings to inject at the next model call.
        # Populated by ``after_model`` (detection) and drained by
        # ``wrap_model_call`` (injection); see module docstring.
        self._pending_warnings: dict[tuple[str, str], list[str]] = defaultdict(list)
        self._pending_warning_touch_order: OrderedDict[tuple[str, str], None] = OrderedDict()
        self._max_pending_warning_keys = max(1, self.max_tracked_threads * 2)

    @classmethod
    def from_config(cls, config: LoopDetectionConfig, *, cumulative_counting: bool | None = None, mutation_reset: bool | None = None) -> LoopDetectionMiddleware:
        """Construct from a Pydantic-validated config, trusting its validation.

        ``cumulative_counting`` / ``mutation_reset`` may be overridden by the
        caller so the subagent chain can turn them on (its repeats are spaced
        wider than the window, and its fix->verify cycles are legitimate
        re-checks after mutations) while the lead agent keeps the historical
        semantics.
        """
        return cls(
            warn_threshold=config.warn_threshold,
            hard_limit=config.hard_limit,
            window_size=config.window_size,
            max_tracked_threads=config.max_tracked_threads,
            tool_freq_warn=config.tool_freq_warn,
            tool_freq_hard_limit=config.tool_freq_hard_limit,
            tool_freq_overrides={name: (o.warn, o.hard_limit) for name, o in config.tool_freq_overrides.items()},
            cumulative_counting=config.cumulative_counting if cumulative_counting is None else cumulative_counting,
            mutation_reset=config.mutation_reset if mutation_reset is None else mutation_reset,
            mutation_reset_budget=config.mutation_reset_budget,
        )

    def _get_thread_id(self, runtime: Runtime) -> str:
        """Extract thread_id from runtime context for per-thread tracking."""
        thread_id = runtime.context.get("thread_id") if runtime.context else None
        if thread_id:
            return str(thread_id)
        return "default"

    def _get_run_id(self, runtime: Runtime) -> str:
        """Extract run_id from runtime context for per-run warning scoping."""
        run_id = runtime.context.get("run_id") if runtime.context else None
        if run_id:
            return str(run_id)
        return "default"

    def _get_tracking_scope(self, runtime: Runtime) -> str:
        """The bucket loop counters belong to.

        For the lead agent this is ``thread_id`` — unchanged, so existing per-thread
        behaviour and state keys stay exactly as they were. A subagent additionally
        carries ``task_id`` (injected by ``SubagentExecutor``), and every task of one run
        shares thread_id/run_id, so without it one task's repeats would count against
        another's budget and could force-stop a task that repeated nothing.
        """
        thread_id = self._get_thread_id(runtime)
        task_id = runtime.context.get("task_id") if runtime.context else None
        if not task_id:
            return thread_id
        return f"{thread_id}::{self._get_run_id(runtime)}::{task_id}"

    def _pending_key(self, runtime: Runtime) -> tuple[str, str]:
        """Return the pending-warning key for the current thread/run."""
        return self._get_thread_id(runtime), self._get_run_id(runtime)

    def _evict_if_needed(self) -> None:
        """Evict least recently used threads if over the limit.

        Must be called while holding self._lock.
        """
        while len(self._history) > self.max_tracked_threads:
            evicted_id, _ = self._history.popitem(last=False)
            self._warned.pop(evicted_id, None)
            self._cumulative.pop(evicted_id, None)
            self._tool_freq.pop(evicted_id, None)
            self._tool_freq_warned.pop(evicted_id, None)
            self._mutation_epoch.pop(evicted_id, None)
            self._mut_state.pop(evicted_id, None)
            for key in list(self._pending_warnings):
                if key[0] == evicted_id:
                    self._drop_pending_warning_key_locked(key)
            logger.debug("Evicted loop tracking for thread %s (LRU)", evicted_id)

    def _drop_pending_warning_key_locked(self, key: tuple[str, str]) -> None:
        """Drop all pending-warning bookkeeping for one thread/run key.

        Must be called while holding self._lock.
        """
        self._pending_warnings.pop(key, None)
        self._pending_warning_touch_order.pop(key, None)

    def _touch_pending_warning_key_locked(self, key: tuple[str, str]) -> None:
        """Mark a pending-warning key as recently used.

        Must be called while holding self._lock.
        """
        self._pending_warning_touch_order[key] = None
        self._pending_warning_touch_order.move_to_end(key)

    def _prune_pending_warning_state_locked(self, protected_key: tuple[str, str]) -> None:
        """Cap pending-warning state across abnormal or concurrent runs.

        Must be called while holding self._lock.
        """
        overflow = len(self._pending_warning_touch_order) - self._max_pending_warning_keys
        if overflow <= 0:
            return

        candidates = [key for key in self._pending_warning_touch_order if key != protected_key]
        for key in candidates[:overflow]:
            self._drop_pending_warning_key_locked(key)

    def _queue_pending_warning(self, runtime: Runtime, warning: str) -> None:
        """Queue one transient warning for the current thread/run with caps."""
        pending_key = self._pending_key(runtime)
        with self._lock:
            warnings = self._pending_warnings[pending_key]
            if warning not in warnings:
                warnings.append(warning)
            if len(warnings) > _MAX_PENDING_WARNINGS_PER_RUN:
                del warnings[: len(warnings) - _MAX_PENDING_WARNINGS_PER_RUN]
            self._touch_pending_warning_key_locked(pending_key)
            self._prune_pending_warning_state_locked(protected_key=pending_key)

    def _track_and_check(self, state: AgentState, runtime: Runtime) -> tuple[str | None, bool]:
        """Track tool calls and check for loops.

        Two detection layers, plus an opt-in Layer 0 reset:
          0. **Mutation-aware reset** (``mutation_reset``): an identical call set
             re-issued after a different call set mutated state restarts its
             counter - a verify-after-fix re-check is not loop evidence.
          1. **Hash-based** (existing): catches identical tool call sets.
          2. **Frequency-based** (new): catches the same *tool type* being
             called many times with varying arguments (e.g. ``read_file``
             on 40 different files).

        Returns:
            (warning_message_or_none, should_hard_stop)
        """
        messages = state.get("messages", [])
        if not messages:
            return None, False

        last_msg = messages[-1]
        if getattr(last_msg, "type", None) != "ai":
            return None, False

        tool_calls = getattr(last_msg, "tool_calls", None)
        if not tool_calls:
            return None, False

        thread_id = self._get_tracking_scope(runtime)
        call_hash = _hash_tool_calls(tool_calls)

        with self._lock:
            # Touch / create entry (move to end for LRU)
            if thread_id in self._history:
                self._history.move_to_end(thread_id)
            else:
                self._history[thread_id] = []
                self._evict_if_needed()

            history = self._history[thread_id]

            # --- Layer 0: mutation-aware reset (opt-in) ---
            # An identical call re-issued after a *different* call set mutated state is a
            # re-check (verify-after-fix), not loop evidence: the world it inspects changed,
            # so the repeat counter restarts at this occurrence. Self-bumps are excluded -
            # a mutating call's own repeats (``rm -rf x`` x5) must still trip the limits -
            # and resets are budgeted so two alternating writers cannot keep resetting
            # each other forever.
            do_reset = False
            if self.mutation_reset:
                mut = self._mut_state[thread_id]
                entry = mut.get(call_hash)
                if entry is None:
                    mut[call_hash] = _MutationEntry(others_at_last_seen=self._mutation_epoch[thread_id])
                    mut.move_to_end(call_hash)
                    while len(mut) > _MAX_CUMULATIVE_HASHES:
                        mut.popitem(last=False)
                else:
                    others_now = self._mutation_epoch[thread_id] - entry.own_bumps
                    if others_now > entry.others_at_last_seen and entry.resets_used < self.mutation_reset_budget:
                        do_reset = True
                        entry.resets_used += 1
                    entry.others_at_last_seen = others_now
                    mut.move_to_end(call_hash)

            if do_reset:
                # Restart this hash's counter in whichever counting mode is active, and
                # forget a prior warning so a future genuine loop can warn again.
                history[:] = [h for h in history if h != call_hash]
                if self.cumulative_counting:
                    self._cumulative[thread_id][call_hash] = 0
                self._warned.get(thread_id, set()).discard(call_hash)

            history.append(call_hash)
            if len(history) > self.window_size:
                history[:] = history[-self.window_size :]

            # Mutation bookkeeping for future occurrences: advance the epoch when this
            # call set mutates state, credited to its own hash (self-exclusion above).
            if self.mutation_reset and _call_set_mutates(tool_calls):
                self._mutation_epoch[thread_id] += 1
                entry = self._mut_state[thread_id].get(call_hash)
                if entry is not None:
                    entry.own_bumps += 1

            if self.cumulative_counting:
                counts = self._cumulative[thread_id]
                counts[call_hash] = counts.get(call_hash, 0) + 1
                counts.move_to_end(call_hash)
                while len(counts) > _MAX_CUMULATIVE_HASHES:
                    counts.popitem(last=False)
                count = counts[call_hash]
                # Deliberately NOT pruning ``_warned`` to the window here: under cumulative
                # counting a hash that slid out of the window is still being counted, so
                # pruning would let the same loop re-warn forever.
                warned_hashes = self._warned.get(thread_id)
                if warned_hashes is not None and len(warned_hashes) > _MAX_CUMULATIVE_HASHES:
                    self._warned[thread_id] = set(list(warned_hashes)[-_MAX_CUMULATIVE_HASHES:])
            else:
                warned_hashes = self._warned.get(thread_id)
                if warned_hashes is not None:
                    warned_hashes.intersection_update(history)
                    if not warned_hashes:
                        self._warned.pop(thread_id, None)

                count = history.count(call_hash)
            tool_names = [tc.get("name", "?") for tc in tool_calls]

            # --- Layer 1: hash-based (identical call sets) ---
            if count >= self.hard_limit:
                logger.error(
                    "Loop hard limit reached — forcing stop",
                    extra={
                        "thread_id": thread_id,
                        "call_hash": call_hash,
                        "count": count,
                        "tools": tool_names,
                    },
                )
                return _HARD_STOP_MSG, True

            if count >= self.warn_threshold:
                warned = self._warned[thread_id]
                if call_hash not in warned:
                    warned.add(call_hash)
                    logger.warning(
                        "Repetitive tool calls detected — injecting warning",
                        extra={
                            "thread_id": thread_id,
                            "call_hash": call_hash,
                            "count": count,
                            "tools": tool_names,
                        },
                    )
                    return _WARNING_MSG, False

            # --- Layer 2: per-tool-type frequency ---
            # ⛔ There is no longer a verification exemption here: gate re-runs count toward
            # the per-tool budget like any other bash call. Raise ``tool_freq_overrides.bash``
            # if a legitimate repair→verify cycle needs more room — that is a per-deployment
            # number in config, not a filename list in this module.
            freq = self._tool_freq[thread_id]
            for tc in tool_calls:
                name = tc.get("name", "")
                if not name:
                    continue
                freq[name] += 1
                tc_count = freq[name]

                if name in self._tool_freq_overrides:
                    eff_warn, eff_hard = self._tool_freq_overrides[name]
                else:
                    eff_warn, eff_hard = self.tool_freq_warn, self.tool_freq_hard_limit

                if tc_count >= eff_hard:
                    logger.error(
                        "Tool frequency hard limit reached — forcing stop",
                        extra={
                            "thread_id": thread_id,
                            "tool_name": name,
                            "count": tc_count,
                        },
                    )
                    return _TOOL_FREQ_HARD_STOP_MSG.format(tool_name=name, count=tc_count), True

                if tc_count >= eff_warn:
                    warned = self._tool_freq_warned[thread_id]
                    if name not in warned:
                        warned.add(name)
                        logger.warning(
                            "Tool frequency warning — too many calls to same tool type",
                            extra={
                                "thread_id": thread_id,
                                "tool_name": name,
                                "count": tc_count,
                            },
                        )
                        return _TOOL_FREQ_WARNING_MSG.format(tool_name=name, count=tc_count), False

        return None, False

    @staticmethod
    def _append_text(content: str | list | None, text: str) -> str | list:
        """Append *text* to AIMessage content, handling str, list, and None.

        When content is a list of content blocks (e.g. Anthropic thinking mode),
        we append a new ``{"type": "text", ...}`` block instead of concatenating
        a string to a list, which would raise ``TypeError``.
        """
        if content is None:
            return text
        if isinstance(content, list):
            return [*content, {"type": "text", "text": f"\n\n{text}"}]
        if isinstance(content, str):
            return content + f"\n\n{text}"
        # Fallback: coerce unexpected types to str to avoid TypeError
        return str(content) + f"\n\n{text}"

    @staticmethod
    def _build_hard_stop_update(last_msg, content: str | list) -> dict:
        """Clear tool-call metadata so forced-stop messages serialize as plain assistant text."""
        update = {
            "tool_calls": [],
            "content": content,
        }

        additional_kwargs = dict(getattr(last_msg, "additional_kwargs", {}) or {})
        for key in ("tool_calls", "function_call"):
            additional_kwargs.pop(key, None)
        update["additional_kwargs"] = additional_kwargs

        response_metadata = deepcopy(getattr(last_msg, "response_metadata", {}) or {})
        if response_metadata.get("finish_reason") == "tool_calls":
            response_metadata["finish_reason"] = "stop"
        update["response_metadata"] = response_metadata

        return update

    def _apply(self, state: AgentState, runtime: Runtime) -> dict | None:
        warning, hard_stop = self._track_and_check(state, runtime)

        if hard_stop:
            # Strip tool_calls from the last AIMessage to force text output.
            # Once tool_calls are stripped, the AIMessage no longer requires
            # matching ToolMessage responses, so mutating it in place here
            # is safe for OpenAI/Moonshot pairing validators.
            messages = state.get("messages", [])
            last_msg = messages[-1]
            content = self._append_text(last_msg.content, warning or _HARD_STOP_MSG)
            stripped_msg = last_msg.model_copy(update=self._build_hard_stop_update(last_msg, content))
            return {"messages": [stripped_msg]}

        if warning:
            # Defer injection to the next model call. We must NOT alter the
            # AIMessage(tool_calls=...) here (would put framework words in
            # the model's mouth, polluting downstream consumers like
            # MemoryMiddleware), nor insert a separate non-tool message
            # (would break OpenAI/Moonshot tool-call pairing because the
            # tools node has not produced ToolMessage responses yet). The
            # warning is delivered via ``wrap_model_call`` below.
            self._queue_pending_warning(runtime, warning)
            return None

        return None

    def _clear_other_run_pending_warnings(self, runtime: Runtime) -> None:
        """Drop stale pending warnings for previous runs in this thread."""
        thread_id, current_run_id = self._pending_key(runtime)
        with self._lock:
            for key in list(self._pending_warnings):
                if key[0] == thread_id and key[1] != current_run_id:
                    self._drop_pending_warning_key_locked(key)

    def _clear_current_run_pending_warnings(self, runtime: Runtime) -> None:
        """Drop pending warnings owned by the current thread/run."""
        pending_key = self._pending_key(runtime)
        with self._lock:
            self._drop_pending_warning_key_locked(pending_key)

    @staticmethod
    def _format_warning_message(warnings: list[str]) -> str:
        """Merge pending warnings into one prompt message."""
        deduped = list(dict.fromkeys(warnings))
        return "\n\n".join(deduped)

    @override
    def before_agent(self, state: AgentState, runtime: Runtime) -> dict | None:
        self._clear_other_run_pending_warnings(runtime)
        return None

    @override
    async def abefore_agent(self, state: AgentState, runtime: Runtime) -> dict | None:
        self._clear_other_run_pending_warnings(runtime)
        return None

    @override
    def after_model(self, state: AgentState, runtime: Runtime) -> dict | None:
        return self._apply(state, runtime)

    @override
    async def aafter_model(self, state: AgentState, runtime: Runtime) -> dict | None:
        return self._apply(state, runtime)

    @override
    def after_agent(self, state: AgentState, runtime: Runtime) -> dict | None:
        self._clear_current_run_pending_warnings(runtime)
        return None

    @override
    async def aafter_agent(self, state: AgentState, runtime: Runtime) -> dict | None:
        self._clear_current_run_pending_warnings(runtime)
        return None

    def _drain_pending_warnings(self, runtime: Runtime) -> list[str]:
        """Pop and return all queued warnings for *runtime*'s thread/run."""
        pending_key = self._pending_key(runtime)
        with self._lock:
            warnings = self._pending_warnings.pop(pending_key, [])
            self._pending_warning_touch_order.pop(pending_key, None)
        return warnings

    def _augment_request(self, request: ModelRequest) -> ModelRequest:
        """Append queued loop warnings (if any) to the outgoing message list.

        The warning is placed *after* every existing message, including the
        ToolMessage responses to the previous AIMessage(tool_calls). This
        keeps ``assistant tool_calls -> tool_messages`` pairing intact for
        OpenAI/Moonshot, avoids the Anthropic mid-stream SystemMessage
        restriction (we use HumanMessage), and never mutates an existing
        AIMessage.
        """
        warnings = self._drain_pending_warnings(request.runtime)
        if not warnings:
            return request
        new_messages = [
            *request.messages,
            HumanMessage(content=self._format_warning_message(warnings), name="loop_warning"),
        ]
        return request.override(messages=new_messages)

    @override
    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelCallResult:
        return handler(self._augment_request(request))

    @override
    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelCallResult:
        return await handler(self._augment_request(request))

    def reset(self, thread_id: str | None = None) -> None:
        """Clear tracking state. If thread_id given, clear only that thread."""
        with self._lock:
            if thread_id:
                self._history.pop(thread_id, None)
                self._warned.pop(thread_id, None)
                self._cumulative.pop(thread_id, None)
                self._tool_freq.pop(thread_id, None)
                self._tool_freq_warned.pop(thread_id, None)
                self._mutation_epoch.pop(thread_id, None)
                self._mut_state.pop(thread_id, None)
                for key in list(self._pending_warnings):
                    if key[0] == thread_id:
                        self._drop_pending_warning_key_locked(key)
            else:
                self._history.clear()
                self._warned.clear()
                self._cumulative.clear()
                self._tool_freq.clear()
                self._tool_freq_warned.clear()
                self._mutation_epoch.clear()
                self._mut_state.clear()
                self._pending_warnings.clear()
                self._pending_warning_touch_order.clear()
