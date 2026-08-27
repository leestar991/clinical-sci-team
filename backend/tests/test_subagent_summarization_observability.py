"""Why subagent summarization reported ``middleware:summarize = 0``.

Session ``c2518bc7`` was the acceptance re-run for ``subagents.summarization``
(trigger already tightened 80k → 60k after ``e3c15416`` showed zero events). It again
measured **0** events. Two independent defects, both pinned here:

1. **The event never existed.** ``RunJournal.record_middleware`` is the only writer of
   ``middleware:{tag}`` run events, and nothing in the tree ever called it with
   ``"summarize"`` — so the metric could not have been non-zero even if every turn had
   compacted. Subagents additionally never received ``__run_journal`` in their runtime
   context, so any such call would have been dropped there anyway.

2. **The trigger was unreachable.** ``count_tokens_approximately`` assumes
   ``chars_per_token=4`` (English). Measured over this deployment's own skill corpus
   (14 files, 154,488 chars / 93,624 o200k tokens) the real ratio is **1.65**, i.e. the
   counter under-reports by **2.42×**. LangChain's usage-metadata rescue is clamped
   (``token_count *= min(1.25, max(1.0, scale_factor))``), so at best it recovers 1.25×.
   ``trigger: 60000`` therefore meant ~116k–145k *real* tokens, while the heaviest task
   in the session peaked around 110k. The threshold was never crossed.

Fix 2 without fix 3 would be a quality regression: compaction removes the *head* of the
message list, and a subagent's head is its injected ``<skill>`` SystemMessages plus the
task statement. Dropping those mid-task loses the rules and the assignment, which is
strictly worse than paying for context.
"""

from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from deerflow.agents.middlewares.summarization_middleware import (
    DeerFlowSummarizationMiddleware,
    build_summarization_middleware,
)
from deerflow.config.summarization_config import ContextSize, SummarizationConfig


class _StubModel:
    """Minimal chat model stand-in: no network, no profile."""

    _llm_type = "stub"

    def __init__(self) -> None:
        self.config: dict = {}

    def with_config(self, **_kwargs):
        return self

    def invoke(self, *_args, **_kwargs):
        return AIMessage(content="STUB SUMMARY")

    async def ainvoke(self, *_args, **_kwargs):
        return AIMessage(content="STUB SUMMARY")

    def _get_ls_params(self):
        return {"ls_provider": "stub"}


def _build(config: SummarizationConfig, monkeypatch) -> DeerFlowSummarizationMiddleware:
    monkeypatch.setattr(
        "deerflow.models.create_chat_model",
        lambda *args, **kwargs: _StubModel(),
    )
    mw = build_summarization_middleware(config, app_config=SimpleNamespace(models=[]))
    assert mw is not None
    return mw


def _cfg(**overrides) -> SummarizationConfig:
    base = {
        "enabled": True,
        "trigger": [ContextSize(type="tokens", value=60000)],
        "keep": ContextSize(type="tokens", value=40000),
        "trim_tokens_to_summarize": 120000,
    }
    base.update(overrides)
    return SummarizationConfig(**base)


class _Runtime:
    def __init__(self, context: dict) -> None:
        self.context = context


class _Journal:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def record_middleware(self, tag: str, *, name: str, hook: str, action: str, changes: dict) -> None:
        self.events.append({"tag": tag, "name": name, "hook": hook, "action": action, "changes": changes})


# ── 1. token counting calibration ──────────────────────────────────────────────


def test_chars_per_token_default_keeps_current_behaviour(monkeypatch) -> None:
    """Unset ``chars_per_token`` must not change any existing deployment."""
    mw = _build(_cfg(), monkeypatch)
    chinese = [HumanMessage(content="临床试验入选标准逐条判定" * 400)]

    # 4 chars/token is what LangChain's default counter uses.
    assert mw.token_counter(chinese) == pytest.approx(len("临床试验入选标准逐条判定" * 400) / 4, rel=0.15)


def test_chars_per_token_calibration_raises_the_counted_total(monkeypatch) -> None:
    """``chars_per_token: 1.65`` makes ``trigger`` mean real tokens for CJK text."""
    mw = _build(_cfg(chars_per_token=1.65), monkeypatch)
    chinese = [HumanMessage(content="临床试验入选标准逐条判定" * 400)]

    counted = mw.token_counter(chinese)
    assert counted == pytest.approx(len("临床试验入选标准逐条判定" * 400) / 1.65, rel=0.15)


def test_calibrated_counter_actually_reaches_the_configured_trigger(monkeypatch) -> None:
    """The end-to-end symptom: same history, same 60k trigger, opposite outcome."""
    history = [
        SystemMessage(content="<skill name=eligibility-judgment>\n" + "判定规则条目。" * 3000 + "\n</skill>"),
        HumanMessage(content="请判定患者 2150006 的入选标准。"),
    ]
    for i in range(12):
        history.append(AIMessage(content="继续核验。", tool_calls=[{"name": "read_file", "args": {"path": "/mnt/x.md"}, "id": f"c{i}"}]))
        history.append(ToolMessage(content="原文摘录：受试者签署知情同意书。" * 900, tool_call_id=f"c{i}"))

    default_mw = _build(_cfg(), monkeypatch)
    calibrated_mw = _build(_cfg(chars_per_token=1.65), monkeypatch)

    assert not default_mw._should_summarize(history, default_mw.token_counter(history)), "baseline expectation: chars/4 keeps the 60k trigger out of reach"
    assert calibrated_mw._should_summarize(history, calibrated_mw.token_counter(history))


# ── 2. observability ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_compaction_records_a_middleware_summarize_event(monkeypatch) -> None:
    """``middleware:summarize`` must be written when — and only when — history is compacted."""
    mw = _build(_cfg(chars_per_token=1.65), monkeypatch)
    journal = _Journal()
    runtime = _Runtime({"thread_id": "t1", "task_id": "call_01", "agent_name": "general-purpose", "__run_journal": journal})

    messages = [SystemMessage(content="<skill name=x>" + "规则。" * 200 + "</skill>"), HumanMessage(content="任务：判定入选标准。")]
    for i in range(10):
        messages.append(AIMessage(content="继续。", tool_calls=[{"name": "read_file", "args": {"path": "/mnt/x"}, "id": f"c{i}"}]))
        messages.append(ToolMessage(content="原文摘录。" * 4000, tool_call_id=f"c{i}"))

    result = await mw._amaybe_summarize({"messages": messages}, runtime)

    assert result is not None, "history above the trigger must be compacted"
    assert [e["tag"] for e in journal.events] == ["summarize"]
    changes = journal.events[0]["changes"]
    assert changes["tokens_before"] > changes["tokens_after"] > 0
    assert changes["messages_summarized"] > 0
    assert changes["task_id"] == "call_01"


@pytest.mark.asyncio
async def test_no_event_when_below_trigger(monkeypatch) -> None:
    mw = _build(_cfg(chars_per_token=1.65), monkeypatch)
    journal = _Journal()
    runtime = _Runtime({"thread_id": "t1", "__run_journal": journal})

    result = await mw._amaybe_summarize({"messages": [HumanMessage(content="短任务")]}, runtime)

    assert result is None
    assert journal.events == []


@pytest.mark.asyncio
async def test_missing_journal_never_breaks_compaction(monkeypatch) -> None:
    """Embedded / unit-test runtimes carry no journal; compaction must still happen."""
    mw = _build(_cfg(chars_per_token=1.65), monkeypatch)
    messages = [HumanMessage(content="任务")]
    for i in range(10):
        messages.append(AIMessage(content="继续。", tool_calls=[{"name": "read_file", "args": {"path": "/mnt/x"}, "id": f"c{i}"}]))
        messages.append(ToolMessage(content="原文摘录。" * 4000, tool_call_id=f"c{i}"))

    result = await mw._amaybe_summarize({"messages": messages}, _Runtime({"thread_id": "t1"}))

    assert result is not None


# ── 3. head preservation ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_skill_messages_and_task_survive_compaction(monkeypatch) -> None:
    """A subagent's skill rules and task statement must never be summarized away."""
    mw = _build(_cfg(chars_per_token=1.65), monkeypatch)
    skill = SystemMessage(content="<skill name=eligibility-judgment>\n判定规则：证据缺失不得转为负面结论。\n</skill>")
    task = HumanMessage(content="任务：判定患者 2150006 的入选标准，产物写入 judgments_draft_IN.json。")
    messages = [skill, task]
    for i in range(10):
        messages.append(AIMessage(content="继续。", tool_calls=[{"name": "read_file", "args": {"path": "/mnt/x"}, "id": f"c{i}"}]))
        messages.append(ToolMessage(content="原文摘录。" * 4000, tool_call_id=f"c{i}"))

    result = await mw._amaybe_summarize({"messages": messages}, _Runtime({"thread_id": "t1", "is_subagent": True}))

    assert result is not None
    kept = result["messages"][1:]
    assert any(getattr(m, "content", None) == skill.content for m in kept), "skill rules were compacted away"
    assert any(getattr(m, "content", None) == task.content for m in kept), "task statement was compacted away"
    # Preserved head must stay in front of the retained tail.
    contents = [getattr(m, "content", "") for m in kept]
    assert contents.index(skill.content) < contents.index(task.content)
