from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from deerflow.agents.middlewares.dynamic_context_middleware import _DYNAMIC_CONTEXT_REMINDER_KEY
from deerflow.agents.middlewares.summarization_middleware import DeerFlowSummarizationMiddleware


def _char_count(messages) -> int:
    return sum(len(str(getattr(message, "content", ""))) for message in messages)


def _raising_count(messages) -> int:
    raise RuntimeError("token counter unavailable")


class _RaisingChatModel(BaseChatModel):
    @property
    def _llm_type(self) -> str:
        return "raising-summary-test-chat-model"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        raise RuntimeError("summary model boom")

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        return self._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


class _StaticChatModel(BaseChatModel):
    text: str = "COMPRESSED_SUMMARY"

    @property
    def _llm_type(self) -> str:
        return "static-summary-test-chat-model"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=self.text))])

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        return self._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


class _RecordingSummaryModel(_StaticChatModel):
    prompts: list[str] = Field(default_factory=list)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.prompts.append("\n".join(str(getattr(message, "content", message)) for message in messages))
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


def _big_history(n: int = 12) -> list:
    messages = []
    for i in range(n):
        messages.append(HumanMessage(content=f"user turn {i} " * 20))
        messages.append(AIMessage(content=f"assistant turn {i} " * 20))
    return messages


class TestSummaryFailureSafety:
    def test_summary_model_failure_does_not_destroy_history(self):
        middleware = DeerFlowSummarizationMiddleware(
            model=_RaisingChatModel(),
            trigger=("messages", 4),
            keep=("messages", 2),
            token_counter=len,
        )

        out = middleware._maybe_summarize({"messages": _big_history()}, None)

        assert out is None


class TestSummaryWritesChannel:
    def _middleware(self) -> DeerFlowSummarizationMiddleware:
        return DeerFlowSummarizationMiddleware(
            model=_StaticChatModel(text="COMPRESSED_SUMMARY"),
            trigger=("messages", 4),
            keep=("messages", 2),
            token_counter=len,
        )

    def test_summary_goes_to_summary_text_not_messages(self):
        out = self._middleware()._maybe_summarize({"messages": _big_history()}, None)

        assert out is not None
        assert out["summary_text"] == "COMPRESSED_SUMMARY"
        injected = [message for message in out["messages"] if isinstance(message, HumanMessage) and message.name == "summary"]
        assert injected == []
        assert any(isinstance(message, RemoveMessage) for message in out["messages"])

    def test_empty_summary_window_after_rescue_does_not_overwrite_existing_summary(self):
        middleware = DeerFlowSummarizationMiddleware(
            model=_StaticChatModel(text="SHOULD_NOT_BE_USED"),
            trigger=("messages", 2),
            keep=("messages", 1),
            token_counter=len,
        )
        reminder = SystemMessage(
            content="<system-reminder>date</system-reminder>",
            additional_kwargs={_DYNAMIC_CONTEXT_REMINDER_KEY: True},
        )
        out = middleware._maybe_summarize(
            {
                "messages": [
                    reminder,
                    HumanMessage(content="latest user message"),
                ],
                "summary_text": "EXISTING_SUMMARY",
            },
            None,
        )

        assert out is None

    def test_existing_summary_is_included_when_creating_next_summary(self):
        model = _RecordingSummaryModel(text="UPDATED_SUMMARY")
        middleware = DeerFlowSummarizationMiddleware(
            model=model,
            trigger=("messages", 4),
            keep=("messages", 2),
            token_counter=len,
        )

        out = middleware._maybe_summarize(
            {
                "messages": _big_history(),
                "summary_text": "OLD_SUMMARY_SENTINEL",
            },
            None,
        )

        assert out is not None
        assert out["summary_text"] == "UPDATED_SUMMARY"
        assert model.prompts
        assert "OLD_SUMMARY_SENTINEL" in model.prompts[-1]

    def test_summary_text_counts_toward_summarization_trigger(self):
        middleware = DeerFlowSummarizationMiddleware(
            model=_StaticChatModel(text="UPDATED_SUMMARY"),
            trigger=("tokens", 80),
            keep=("messages", 2),
            token_counter=_char_count,
        )

        out = middleware._maybe_summarize(
            {
                "messages": [
                    HumanMessage(content="old"),
                    AIMessage(content="older"),
                    HumanMessage(content="latest"),
                ],
                "summary_text": "S" * 120,
            },
            None,
        )

        assert out is not None
        assert out["summary_text"] == "UPDATED_SUMMARY"

    def test_previous_summary_is_trimmed_with_summary_prompt_input(self):
        middleware = DeerFlowSummarizationMiddleware(
            model=_StaticChatModel(text="UPDATED_SUMMARY"),
            trigger=("messages", 4),
            keep=("messages", 2),
            token_counter=_char_count,
            trim_tokens_to_summarize=80,
        )
        previous_summary = "OLD_SUMMARY_START " + ("S" * 240) + " OLD_SUMMARY_END"

        prompt = middleware._build_summary_prompt(
            [HumanMessage(content="NEW_MESSAGE_SENTINEL " + ("N" * 240))],
            previous_summary=previous_summary,
        )

        assert prompt is not None
        assert previous_summary not in prompt
        assert "NEW_MESSAGE_SENTINEL" in prompt

    def test_new_message_summary_prompt_trim_uses_token_counter_budget(self):
        middleware = DeerFlowSummarizationMiddleware(
            model=_StaticChatModel(text="UPDATED_SUMMARY"),
            trigger=("messages", 4),
            keep=("messages", 2),
            token_counter=_char_count,
            trim_tokens_to_summarize=40,
        )

        body = middleware._build_summary_input_text("Human: NEW_MESSAGE_SENTINEL " + ("N" * 200))

        assert body is not None
        new_messages = body.split("<new_messages>\n", 1)[1].split("\n</new_messages>", 1)[0]
        assert len(new_messages) <= 40
        assert "NEW_MESSAGE_SENTINEL" in new_messages

    def test_summary_prompt_fallback_bound_respects_small_budget(self):
        middleware = DeerFlowSummarizationMiddleware(
            model=_StaticChatModel(text="UPDATED_SUMMARY"),
            trigger=("messages", 4),
            keep=("messages", 2),
            token_counter=_raising_count,
            trim_tokens_to_summarize=2,
        )

        text = middleware._trim_summary_section_text("abcdef", 2, strategy="first")

        assert len(text) <= 2


# --------------------------------------------------------------------------- #
# 空摘要不得换走历史(thread 7512ebd2)                                          #
# --------------------------------------------------------------------------- #
#
# 压缩是一次**交换**:删掉未保留的消息,把 summary_text 放到它们的位置。
# `_summarize_with` 返回 `response.text.strip()`,摘要模型只回空白时得到 `""` ——
# 假值,但**不是** None,而旧守卫只挡 None。于是交换照做:消息删了,summary_text 被
# 覆写成 "",子代理注入端因为通道为空而跳过注入 = 净删除。
#
# 实测(thread 7512ebd2,判定 task `...5434-retry1`):10 次压缩里 6 次 summary_chars=0,
# 每次带走 11-22 条消息。子代理随后丢掉自己的任务书,反复自问「我的任务到底是什么」,
# 读了**对侧轨**的标准包,最后写出 `eligibility_judgment_EX_...json`,而它的
# expected_outputs 声明的是 IN 初稿 —— 4.02M token / 18.8 分钟被产物闸整单作废。


class TestEmptySummaryNeverTradesAwayHistory:
    def _middleware(self, text: str) -> DeerFlowSummarizationMiddleware:
        return DeerFlowSummarizationMiddleware(
            model=_StaticChatModel(text=text),
            trigger=("messages", 4),
            keep=("messages", 2),
            token_counter=len,
        )

    @pytest.mark.parametrize("text", ["", "   ", "\n\n", "\t"])
    def test_blank_summary_skips_compaction(self, text):
        out = self._middleware(text)._maybe_summarize({"messages": _big_history()}, None)

        assert out is None, "空摘要必须放弃本轮压缩,而不是换走历史"

    @pytest.mark.parametrize("text", ["", "  "])
    def test_blank_summary_does_not_overwrite_an_existing_summary(self, text):
        out = self._middleware(text)._maybe_summarize(
            {"messages": _big_history(), "summary_text": "EXISTING_SUMMARY"},
            None,
        )

        assert out is None

    def test_blank_summary_skips_compaction_on_the_async_path(self):
        import asyncio

        out = asyncio.run(self._middleware("")._amaybe_summarize({"messages": _big_history()}, None))

        assert out is None, "生产走的是 async 路径,守卫必须两边一致"

    def test_non_blank_summary_still_compacts(self):
        out = self._middleware("REAL_SUMMARY")._maybe_summarize({"messages": _big_history()}, None)

        assert out is not None
        assert out["summary_text"] == "REAL_SUMMARY"
        assert any(isinstance(message, RemoveMessage) for message in out["messages"])

    def test_blank_summary_emits_no_summarize_event(self):
        """事件是验收指标:一次没发生的压缩不能记成发生了。"""
        recorded: list[dict] = []
        journal = type("J", (), {"record_middleware": lambda self, kind, **kw: recorded.append(kw)})()
        runtime = SimpleNamespace(context={"__run_journal": journal, "is_subagent": True})

        out = self._middleware("")._maybe_summarize({"messages": _big_history()}, runtime)

        assert out is None
        assert recorded == []


class TestEmptySummaryIsDiagnosable:
    """空摘要有三种成因，日志必须能区分，否则下一次会话只能重猜一遍。

    会话 7512ebd2：69 次子代理压缩里 33 次空（48%），8192-token 的
    ``deepseek-v4-flash`` 摘要最多 120k token 的输入，日志里没有任何一句能说清
    「是 reasoning 吃光了预算，还是 length 截断，还是模型真的回了空白」。
    """

    def test_reasoning_only_response_is_reported_with_its_signals(self, caplog):
        import logging

        response = SimpleNamespace(
            text="",
            response_metadata={"finish_reason": "length"},
            usage_metadata={"output_tokens": 8192},
            additional_kwargs={"reasoning_content": "R" * 4096},
        )

        with caplog.at_level(logging.WARNING):
            out = DeerFlowSummarizationMiddleware._summary_text_of(response)

        assert out == ""
        message = caplog.text
        assert "length" in message, "finish_reason 决定是调 max_tokens 还是调输入上限"
        assert "8192" in message
        assert "4096" in message, "reasoning 字数是「预算被思考吃掉」的直接证据"

    def test_non_empty_text_is_returned_untouched_without_warning(self, caplog):
        import logging

        response = SimpleNamespace(text="  REAL_SUMMARY  ", response_metadata={}, usage_metadata={}, additional_kwargs={})

        with caplog.at_level(logging.WARNING):
            out = DeerFlowSummarizationMiddleware._summary_text_of(response)

        assert out == "REAL_SUMMARY"
        assert caplog.text == ""

    def test_response_without_text_attribute_degrades_to_empty(self):
        class _NoText:
            @property
            def text(self):
                raise AttributeError("no text on this response")

        assert DeerFlowSummarizationMiddleware._summary_text_of(_NoText()) == ""
