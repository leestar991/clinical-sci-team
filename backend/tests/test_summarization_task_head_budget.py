"""Compaction must free context, and must never trade away the assignment.

Both behaviours pinned here come from thread ``247a535f`` (P001 judgment rerun), where
three judgment subagents each ran 93-118 steps and ~2M tokens with **zero**
``write_file`` calls and failed the artifact gate:

1. **The keep budget did not account for the rescued head.** ``_determine_cutoff_index``
   sizes the preserved suffix against the whole ``keep`` budget, then
   ``_preserve_task_head`` moves the head back into the preserved set — so the real
   post-compaction size was ``keep + head``. With ``trigger`` 60k / ``keep`` 40k and a
   ~13-15k head that landed at ~55k against a 60k trigger, so the next tool result
   re-crossed it: 14 compactions in one task, the first freeing only 4.5k
   (60,800 -> 55,786). ``_keep_budget_after_head`` charges the head to the budget.

2. **An oversized head cancelled the rescue entirely.** The old guard returned early when
   the head exceeded half the ``keep`` budget, which made a *larger* contract *more*
   likely to be lost. After the first compaction the subagents stopped knowing what they
   were: one announced it was "apparently the main agent", another argued with a
   "handover" that never existed. Now an over-budget head degrades to keeping the task
   statement (the assignment) and lets the skill contracts be summarized — rules can be
   re-read from disk by path, an assignment cannot.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from deerflow.agents.middlewares.summarization_middleware import DeerFlowSummarizationMiddleware

TASK_STATEMENT = "只判 IN-1 IN-2-1，只写 judgments_draft_P001_IN_b1.json"


def _char_count(messages) -> int:
    return sum(len(str(getattr(message, "content", ""))) for message in messages)


def _middleware(*, keep_tokens: int, trigger_tokens: int = 1_000_000) -> DeerFlowSummarizationMiddleware:
    model = MagicMock()
    model.invoke.return_value = SimpleNamespace(text="compressed summary")
    model.with_config.return_value = model
    return DeerFlowSummarizationMiddleware(
        model=model,
        trigger=("tokens", trigger_tokens),
        keep=("tokens", keep_tokens),
        token_counter=_char_count,
    )


def _runtime(*, is_subagent: bool) -> SimpleNamespace:
    context: dict = {"thread_id": "thread-247a535f"}
    if is_subagent:
        context["is_subagent"] = True
        context["task_id"] = "call_00_pijxdpTXjkNayHURC5m40330"
    return SimpleNamespace(context=context)


def _history(*, skill_chars: int, task_chars: int = 60, body_messages: int = 12, body_chars: int = 40) -> list:
    """A subagent history: skill contract(s), the task statement, then working turns."""
    messages: list = [SystemMessage(content="S" * skill_chars)]
    messages.append(HumanMessage(content=TASK_STATEMENT.ljust(task_chars, "·")))
    for i in range(body_messages):
        messages.append(AIMessage(content=f"step {i} " + "b" * body_chars))
    return messages


class TestKeepBudgetChargesTheHead:
    def test_suffix_is_sized_against_keep_minus_head(self):
        """head + preserved suffix must land at or under ``keep``, not ``keep + head``."""
        keep = 400
        mw = _middleware(keep_tokens=keep)
        messages = _history(skill_chars=150, body_messages=20)
        runtime = _runtime(is_subagent=True)

        cutoff = mw._determine_cutoff_index_for(messages, runtime)
        to_summarize, preserved = mw._partition_messages(messages, cutoff)
        _remaining, rescued_plus_preserved = mw._preserve_task_head(to_summarize, preserved, runtime)

        assert _char_count(rescued_plus_preserved) <= keep

    def test_unadjusted_cutoff_would_have_overshot(self):
        """Regression guard: the parent's own cutoff exceeds ``keep`` once the head returns.

        This is the arithmetic behind the 14-compactions-per-task oscillation; if this
        assertion ever fails, the parent changed and the adjustment may be redundant.
        """
        keep = 400
        mw = _middleware(keep_tokens=keep)
        messages = _history(skill_chars=150, body_messages=20)
        runtime = _runtime(is_subagent=True)

        parent_cutoff = mw._determine_cutoff_index(messages)
        to_summarize, preserved = mw._partition_messages(messages, parent_cutoff)
        _remaining, rescued_plus_preserved = mw._preserve_task_head(to_summarize, preserved, runtime)

        assert _char_count(rescued_plus_preserved) > keep

    def test_adjusted_cutoff_summarizes_at_least_as_much(self):
        adjusted = _middleware(keep_tokens=400)
        messages = _history(skill_chars=150, body_messages=20)
        runtime = _runtime(is_subagent=True)

        assert adjusted._determine_cutoff_index_for(messages, runtime) >= adjusted._determine_cutoff_index(messages)

    def test_lead_agent_cutoff_is_untouched(self):
        """The lead has no head rescue, so its cutoff must not shift."""
        mw = _middleware(keep_tokens=400)
        messages = _history(skill_chars=150, body_messages=20)

        assert mw._determine_cutoff_index_for(messages, _runtime(is_subagent=False)) == mw._determine_cutoff_index(messages)

    def test_message_based_keep_is_untouched(self):
        model = MagicMock()
        model.invoke.return_value = SimpleNamespace(text="s")
        model.with_config.return_value = model
        mw = DeerFlowSummarizationMiddleware(
            model=model,
            trigger=("messages", 100),
            keep=("messages", 4),
            token_counter=_char_count,
        )
        messages = _history(skill_chars=150, body_messages=20)

        assert mw._determine_cutoff_index_for(messages, _runtime(is_subagent=True)) == mw._determine_cutoff_index(messages)

    def test_floor_keeps_a_working_window_when_head_eats_the_budget(self):
        """A head larger than ``keep`` must not collapse the suffix to nothing.

        The model still needs the most recent tool result to act on; a zero-length
        window would make every turn re-trigger with nothing to show for it.
        """
        keep = 200
        mw = _middleware(keep_tokens=keep)
        messages = _history(skill_chars=5_000, body_messages=20)
        runtime = _runtime(is_subagent=True)

        adjusted = mw._keep_budget_after_head(messages, runtime)
        assert adjusted == max(1, int(keep * mw._MIN_SUFFIX_TOKEN_SHARE))

    def test_no_head_means_no_adjustment(self):
        mw = _middleware(keep_tokens=400)
        messages = [AIMessage(content="a" * 50) for _ in range(6)]

        assert mw._keep_budget_after_head(messages, _runtime(is_subagent=True)) is None


class TestOversizedHeadDegradesInsteadOfCancelling:
    def test_task_statement_survives_an_oversized_head(self):
        """The assignment must be preserved even when the skill block blows the budget."""
        mw = _middleware(keep_tokens=200)  # head budget = 100 chars
        skill = SystemMessage(content="S" * 5_000)
        task = HumanMessage(content=TASK_STATEMENT)
        body = [AIMessage(content=f"step {i}") for i in range(6)]
        runtime = _runtime(is_subagent=True)

        to_summarize, preserved = mw._preserve_task_head([skill, task, *body], [], runtime)

        assert task in preserved, "the assignment was traded away — the 247a535f failure"
        assert skill not in preserved, "the oversized skill block should be summarized, not kept"
        assert skill in to_summarize, "the skill block must go to the summarizer, not vanish"

    def test_no_message_is_lost_when_degrading(self):
        mw = _middleware(keep_tokens=200)
        skill_a = SystemMessage(content="A" * 3_000)
        skill_b = SystemMessage(content="B" * 3_000)
        task = HumanMessage(content=TASK_STATEMENT)
        body = [AIMessage(content=f"step {i}") for i in range(4)]
        original = [skill_a, skill_b, task, *body]

        to_summarize, preserved = mw._preserve_task_head(list(original), [], _runtime(is_subagent=True))

        assert sorted(id(m) for m in [*to_summarize, *preserved]) == sorted(id(m) for m in original)

    def test_head_within_budget_is_kept_whole(self):
        mw = _middleware(keep_tokens=10_000)
        skill = SystemMessage(content="S" * 100)
        task = HumanMessage(content=TASK_STATEMENT)
        body = [AIMessage(content=f"step {i}") for i in range(6)]

        to_summarize, preserved = mw._preserve_task_head([skill, task, *body], [], _runtime(is_subagent=True))

        assert preserved[:2] == [skill, task]
        assert skill not in to_summarize

    def test_lead_agent_never_gets_head_rescue(self):
        mw = _middleware(keep_tokens=200)
        first = HumanMessage(content="user turn one")
        body = [AIMessage(content=f"step {i}") for i in range(4)]

        to_summarize, preserved = mw._preserve_task_head([first, *body], [], _runtime(is_subagent=False))

        assert preserved == []
        assert to_summarize[0] is first

    def test_system_only_head_over_budget_is_still_kept(self):
        """No task statement to fall back on → keep the head rather than lose the rules."""
        mw = _middleware(keep_tokens=200)
        skill = SystemMessage(content="S" * 5_000)
        body = [AIMessage(content=f"step {i}") for i in range(4)]

        _to_summarize, preserved = mw._preserve_task_head([skill, *body], [], _runtime(is_subagent=True))

        assert preserved == [skill]
