"""The repo's own ``config.yaml`` must actually arm the subagent guards.

Why a test about the shipped config, and not just the models: **a commented-out guard is
indistinguishable from a configured one when you skim the file.** The
``subagents.token_budget`` block carried a complete, well-argued rationale from session
``2812eaf8`` onward — and was `#`-commented the whole time. Nobody noticed, because the
prose read like settings.

Session ``247a535f`` paid for it: three judgment subagents burned 1.94M / 1.96M / 2.43M
tokens (6.33M total) with **zero** ``write_file`` calls and nothing capping them, and the
run ended on `402 Insufficient Balance` — the account balance was the de facto token
budget. In the same session two revision subagents ran ``check_track_structure.py`` 22 and
24 times, continuing past a green ``EXIT=0`` gate, with no ``LoopDetectionMiddleware``
attached to interrupt them because that guard was never enabled either.

``test_subagent_runtime_guards.py`` covers the wiring (does the middleware attach when the
flag is on). This file covers the *deployment*: is the flag on, in the config we ship.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from deerflow.config.subagents_config import SubagentsAppConfig

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = REPO_ROOT / "config.yaml"


@pytest.fixture(scope="module")
def subagents() -> SubagentsAppConfig:
    if not CONFIG_PATH.exists():  # gitignored at runtime; a bare checkout has only the example
        pytest.skip("config.yaml 不存在（未运行 make config）")
    raw = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    section = raw.get("subagents")
    assert isinstance(section, dict), "config.yaml 缺少 subagents 段"
    return SubagentsAppConfig(**section)


class TestTokenBudgetIsArmed:
    def test_enabled(self, subagents):
        assert subagents.token_budget.enabled, "子代理 token 预算未启用——失控子代理将烧到余额见底（会话 247a535f）"

    def test_hard_stop_leaves_room_to_write_the_artifact(self, subagents):
        """hard stop 的价值是换回一份产物，所以必须在 100% 之前触发。

        留到 1.0 才停，模型连 ``write_file`` 那一步的预算都没有了，
        于是又变成"烧完即作废"——与不设预算的结果相同。
        """
        assert subagents.token_budget.hard_stop_threshold < 1.0

    def test_warning_precedes_the_hard_stop(self, subagents):
        assert subagents.token_budget.warn_threshold < subagents.token_budget.hard_stop_threshold

    def test_budget_admits_the_heaviest_legitimate_task(self, subagents):
        """实测锚点：本会话最重的**成功**任务（EX 轨修订）是 2.46M token。

        上限低于它会误杀合法修订任务；本项把那条实测线钉住，
        任何下调都必须先解释为什么 2.46M 的任务不再合法。
        """
        heaviest_successful_task_tokens = 2_463_663
        assert subagents.token_budget.max_tokens > heaviest_successful_task_tokens

    def test_budget_stops_a_runaway_before_the_account_does(self, subagents):
        """失败的判定任务各约 2M 且零产物；预算必须在"三个这样的任务"之前就介入。

        会话总量 20.33M。若单任务上限乘以并发数仍远高于账户余量，
        这个护栏就只是记录了一次超支，而不是防止它。
        """
        concurrent_judgment_tasks = 3
        assert subagents.token_budget.max_tokens * concurrent_judgment_tasks < 20_325_774


class TestLoopDetectionIsArmed:
    def test_enabled(self, subagents):
        assert subagents.loop_detection.enabled, "子代理闸循环检测未启用——闸绿后继续跑无人拦截（会话 247a535f）"

    def test_cumulative_counting_stays_on(self, subagents):
        """子代理的重复被 read_file/grep 隔开，20 条滑窗会在计数器到限前挤掉哈希。"""
        assert subagents.loop_detection.cumulative_counting


class TestCompactionThresholdsClearTheReadingSet:
    """压缩阈值必须大于"任务开工所需的最小上下文"。

    判定子代理的必读集是可以逐项加总的（chars / 1.65）：judgment-principles 16.8k +
    judge-delegation 11.9k + SKILL.md 11.3k + schema_example 7.0k + judgment-schema 5.1k
    + criteria_judge_EX 22.0k + ocr_page_index 6.2k ≈ 60k（EX 轨），再加委派 prompt。
    旧的 60k trigger 正好落在这条线上，于是三个任务全部在**第 2 步**触发首次压缩，
    此后每 2–3 步压一次，从未攒够上下文进入判定。
    """

    # EX 轨（较重的一轨）必读集实测值，向上取整留一点余地。
    READING_SET_TOKENS = 60_000

    def _trigger_tokens(self, subagents) -> int:
        trigger = subagents.summarization.trigger
        entries = trigger if isinstance(trigger, list) else [trigger]
        token_triggers = [int(e.value) for e in entries if e.type == "tokens"]
        assert token_triggers, "子代理压缩必须有一个 tokens 型 trigger"
        return min(token_triggers)

    def test_summarization_enabled(self, subagents):
        assert subagents.summarization.enabled

    def test_trigger_clears_the_reading_set_with_headroom(self, subagents):
        """光把规则和输入读进来就触发压缩 = 压缩在阻止任务开始，而不是省钱。"""
        assert self._trigger_tokens(subagents) > self.READING_SET_TOKENS * 1.5

    def test_keep_still_holds_the_reading_set(self, subagents):
        """压后装不下必读集，下一步必然重读规则文件——那正是 247a535f 的形态。"""
        keep = subagents.summarization.keep
        assert keep.type == "tokens"
        assert int(keep.value) >= self.READING_SET_TOKENS

    def test_keep_leaves_room_to_work_before_retriggering(self, subagents):
        """trigger 与 keep 之间必须有实际工作空间，否则压完很快又顶线（震荡）。"""
        keep = int(subagents.summarization.keep.value)
        assert self._trigger_tokens(subagents) - keep >= 40_000

    def test_trim_limit_still_matches_the_summary_models_output_budget(self, subagents):
        """trigger 抬高后待压窗口变大，输入上限仍须与摘要模型 8192 输出预算相称。

        会话 ``7512ebd2``：120k 输入 / 8192 输出 → 48% 的压缩返回**空摘要**，
        而空摘要在当时等于净删除。40k 输入 ≈ 5:1，实测 0 次空摘要。
        """
        assert subagents.summarization.trim_tokens_to_summarize is not None
        assert subagents.summarization.trim_tokens_to_summarize <= 40_000

    def test_summary_is_handed_back_to_the_subagent(self, subagents):
        """关掉它不是"少一个功能"，而是保留一个静默删数据的 bug（会话 88df83a8）。"""
        assert subagents.summarization.inject_summary_message


class TestSummaryPromptDoesNotInviteAHandoverHallucination:
    """摘要 prompt 不得把压缩产物称作"交接单"。

    会话 ``247a535f``：旧 prompt 首句写着「这不是会话摘要，是**任务交接单**」，
    三个判定子代理于是都去找那个不存在的交接方 —— 一个宣称 "according to the handover,
    the previous sub-agent found the OCR records were empty"（OCR 实际完整、也没有别的
    子代理），并花 8 步"推翻"自己虚构的前提。
    """

    def _prompt(self, subagents) -> str:
        prompt = subagents.summarization.summary_prompt
        assert prompt, "子代理摘要 prompt 未配置"
        return prompt

    def test_handover_framing_is_not_asserted(self, subagents):
        """旧 prompt 断言"**是**任务交接单"；现在只允许出现在否定句里。

        不能简单断言"交接单"不出现——现行 prompt 正是靠一条「这不是"交接单"」的
        硬约束来纠正模型口吻的，那处出现是**修复本身**。
        """
        prompt = self._prompt(subagents)
        assert "是**任务交接单**" not in prompt
        assert '不是"交接单"' in prompt or "不是「交接单」" in prompt

    def test_prompt_forbids_third_party_narration(self, subagents):
        prompt = self._prompt(subagents)
        assert "上一个子代理" in prompt, "必须显式点名并禁止这种表述"
        assert "第一人称" in prompt

    def test_prompt_states_the_reader_is_the_same_task(self, subagents):
        assert "你自己" in self._prompt(subagents)
