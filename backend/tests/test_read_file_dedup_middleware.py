"""版本感知 ``read_file`` 去重(criteria-token-saving-v1.2 Task 5)。

基线证据:147 个外部化 read_file 文件里只有 62 个唯一哈希,完全重复的外部化字节
2,479,270(63.6%)。同一个 ``SKILL.md`` / 判定 JSON 在一个 run 内被反复整篇重读。

**默认关闭**:返回引用而非正文会改变模型看到的内容,必须按部署显式开启,不能靠升级悄悄生效。
本文件的第一条测试就锁定这一点。

正确性优先于节省:cache key 含内容哈希,任何修改都是自然 miss。命中过期内容比多花 token
糟得多 —— 模型会基于已不存在的内容去改文件。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip(
    "deerflow.agents.middlewares.read_file_dedup_middleware",
    reason="read_file_dedup_middleware 尚未实现",
)

from langchain_core.messages import ToolMessage  # noqa: E402

from deerflow.agents.middlewares.read_file_dedup_middleware import (  # noqa: E402
    ReadFileDedupMiddleware,
    _dedup_cache_size,
    _reset_dedup_cache,
)
from deerflow.config.read_dedup_config import ReadFileDedupConfig  # noqa: E402

BIG_A = "A" * 3000
BIG_B = "B" * 3000


class FakeRequest:
    """最小 ToolCallRequest 替身。"""

    def __init__(
        self,
        name: str,
        args: dict,
        *,
        thread_id: str = "t1",
        run_id: str = "r1",
        sandbox_id: str = "s1",
        task_id: str | None = None,
    ):
        self.tool_call = {"name": name, "args": args, "id": "call-1"}
        self.state = {}
        context = {"thread_id": thread_id, "run_id": run_id, "sandbox_id": sandbox_id}
        if task_id is not None:
            context["task_id"] = task_id
        self.runtime = SimpleNamespace(context=context, state={})


@pytest.fixture(autouse=True)
def _clean_cache():
    _reset_dedup_cache()
    yield
    _reset_dedup_cache()


def _mw(**over) -> ReadFileDedupMiddleware:
    cfg = ReadFileDedupConfig(**{"enabled": True, "min_chars": 100, **over})
    return ReadFileDedupMiddleware(config=cfg)


def _call(mw, request, content: str) -> str:
    """跑一遍 wrap_tool_call,handler 固定返回 content。"""
    result = mw.wrap_tool_call(request, lambda req: ToolMessage(content=content, tool_call_id="call-1"))
    return result.content if isinstance(result, ToolMessage) else str(result)


# --------------------------------------------------------------------------- #
# 默认关闭                                                                     #
# --------------------------------------------------------------------------- #


def test_disabled_by_default():
    assert ReadFileDedupConfig().enabled is False


def test_disabled_config_never_dedupes():
    mw = ReadFileDedupMiddleware(config=ReadFileDedupConfig(enabled=False, min_chars=0))
    req = FakeRequest("read_file", {"path": "/mnt/user-data/workspace/a.md"})
    assert _call(mw, req, BIG_A) == BIG_A
    assert _call(mw, req, BIG_A) == BIG_A, "关闭时第二次读必须仍返回完整正文"
    assert _dedup_cache_size() == 0, "关闭时不得建立缓存条目"


# --------------------------------------------------------------------------- #
# 同版本重复读 → 引用                                                          #
# --------------------------------------------------------------------------- #


def test_second_read_of_same_version_returns_reference():
    mw = _mw()
    req = FakeRequest("read_file", {"path": "/mnt/user-data/workspace/a.md"})
    assert _call(mw, req, BIG_A) == BIG_A, "首次读必须给完整正文"
    second = _call(mw, req, BIG_A)
    assert second != BIG_A
    assert "a.md" in second, "引用必须点明是哪个文件"
    assert BIG_A not in second


def test_reference_mentions_it_is_unchanged():
    mw = _mw()
    req = FakeRequest("read_file", {"path": "/mnt/user-data/workspace/a.md"})
    _call(mw, req, BIG_A)
    second = _call(mw, req, BIG_A)
    assert "unchanged" in second.lower() or "未变" in second


# --------------------------------------------------------------------------- #
# 版本失效                                                                     #
# --------------------------------------------------------------------------- #


def test_content_change_is_a_cache_miss():
    """核心正确性:文件改了就必须返回新正文,绝不能给旧引用。"""
    mw = _mw()
    req = FakeRequest("read_file", {"path": "/mnt/user-data/workspace/a.md"})
    _call(mw, req, BIG_A)
    assert _call(mw, req, BIG_B) == BIG_B
    # 改回旧内容也算新版本命中链的一部分:仍应给正文一次
    assert _call(mw, req, BIG_B) != BIG_B


def test_different_range_is_a_separate_entry():
    mw = _mw()
    path = "/mnt/user-data/workspace/a.md"
    r1 = FakeRequest("read_file", {"path": path, "start_line": 1, "end_line": 50})
    r2 = FakeRequest("read_file", {"path": path, "start_line": 51, "end_line": 100})
    assert _call(mw, r1, BIG_A) == BIG_A
    assert _call(mw, r2, BIG_B) == BIG_B, "不同 range 是不同条目，不得互相命中"


# --------------------------------------------------------------------------- #
# 隔离                                                                         #
# --------------------------------------------------------------------------- #


def test_different_thread_does_not_share_cache():
    mw = _mw()
    a = FakeRequest("read_file", {"path": "/mnt/user-data/workspace/a.md"}, thread_id="t1")
    b = FakeRequest("read_file", {"path": "/mnt/user-data/workspace/a.md"}, thread_id="t2")
    assert _call(mw, a, BIG_A) == BIG_A
    assert _call(mw, b, BIG_A) == BIG_A, "跨 thread 不得共享 mutable 文件缓存"


def test_different_sandbox_does_not_share_cache():
    mw = _mw()
    a = FakeRequest("read_file", {"path": "/mnt/user-data/workspace/a.md"}, sandbox_id="s1")
    b = FakeRequest("read_file", {"path": "/mnt/user-data/workspace/a.md"}, sandbox_id="s2")
    assert _call(mw, a, BIG_A) == BIG_A
    assert _call(mw, b, BIG_A) == BIG_A


def test_different_run_does_not_share_cache():
    """跨 run 不复用:run 之间可能有人在外部改了文件,且引用对新 run 无上下文。"""
    mw = _mw()
    a = FakeRequest("read_file", {"path": "/mnt/user-data/workspace/a.md"}, run_id="r1")
    b = FakeRequest("read_file", {"path": "/mnt/user-data/workspace/a.md"}, run_id="r2")
    assert _call(mw, a, BIG_A) == BIG_A
    assert _call(mw, b, BIG_A) == BIG_A


# --------------------------------------------------------------------------- #
# 适用范围                                                                     #
# --------------------------------------------------------------------------- #


def test_small_reads_are_not_deduped():
    mw = _mw(min_chars=1000)
    req = FakeRequest("read_file", {"path": "/mnt/user-data/workspace/small.md"})
    tiny = "x" * 10
    assert _call(mw, req, tiny) == tiny
    assert _call(mw, req, tiny) == tiny, "小读的间接成本高于收益，不该去重"


def test_non_read_tools_pass_through():
    mw = _mw()
    req = FakeRequest("write_file", {"path": "/mnt/user-data/workspace/a.md", "content": BIG_A})
    assert _call(mw, req, BIG_A) == BIG_A
    assert _call(mw, req, BIG_A) == BIG_A
    assert _dedup_cache_size() == 0


def test_error_results_are_not_cached():
    """报错不能被缓存,否则一次瞬时失败会被无限复述为"未变"。"""
    mw = _mw()
    req = FakeRequest("read_file", {"path": "/mnt/user-data/workspace/missing.md"})
    err = "Error: File not found: /mnt/user-data/workspace/missing.md"
    assert _call(mw, req, err) == err
    assert _call(mw, req, err) == err
    assert _dedup_cache_size() == 0


def test_cache_is_bounded():
    mw = _mw(max_entries=3)
    for i in range(10):
        req = FakeRequest("read_file", {"path": f"/mnt/user-data/workspace/f{i}.md"})
        _call(mw, req, BIG_A + str(i))
    assert _dedup_cache_size() <= 3


def test_write_invalidates_so_next_read_returns_body():
    """写后再读必须拿到正文 —— 与 read-before-write 闸的语义一致。"""
    mw = _mw()
    path = "/mnt/user-data/workspace/a.md"
    read_req = FakeRequest("read_file", {"path": path})
    _call(mw, read_req, BIG_A)
    assert _call(mw, read_req, BIG_A) != BIG_A  # 已建立缓存

    write_req = FakeRequest("str_replace", {"path": path, "old_str": "A", "new_str": "B"})
    _call(mw, write_req, "OK")

    assert _call(mw, read_req, BIG_A) == BIG_A, "写操作后必须失效该文件的读缓存"


# --------------------------------------------------------------------------- #
# 子代理隔离（Phase 1 / Task 14a）                                             #
# --------------------------------------------------------------------------- #
#
# 子代理上下文是**隔离**的：task B 的 transcript 里没有 task A 的那次读。若缓存只按
# (sandbox, thread, run) 归集，B 的首读会拿到「你先前已读过、正文省略」的引用，而它既
# 看不到那次读、也无法取回正文 —— 判定/QC 子代理会因此直接失去取证能力。
# 因此 cache key 必须带 task 维度（`context["task_id"]`，由 executor 注入）。


def test_first_read_in_another_task_returns_body():
    mw = _mw()
    path = "/mnt/skills/custom/criteria-parser/SKILL.md"
    task_a = FakeRequest("read_file", {"path": path}, task_id="task-a")
    task_b = FakeRequest("read_file", {"path": path}, task_id="task-b")

    assert _call(mw, task_a, BIG_A) == BIG_A
    assert _call(mw, task_a, BIG_A) != BIG_A, "同一 task 内的重复读仍要去重"
    assert _call(mw, task_b, BIG_A) == BIG_A, "另一个 task 的首读必须拿到完整正文"
    assert _call(mw, task_b, BIG_A) != BIG_A, "该 task 自己的第二次读才去重"


def test_lead_and_subagent_do_not_share_cache():
    """lead（无 task_id）与子代理（有 task_id）是两个独立上下文。"""
    mw = _mw()
    path = "/mnt/user-data/workspace/criteria_parsed.json"
    lead = FakeRequest("read_file", {"path": path})
    sub = FakeRequest("read_file", {"path": path}, task_id="task-a")

    assert _call(mw, lead, BIG_A) == BIG_A
    assert _call(mw, sub, BIG_A) == BIG_A, "子代理不得继承 lead 的读缓存"


def test_write_in_one_task_does_not_leak_reference_to_another(monkeypatch):
    """跨 task 的失效互不干扰，但也不得让另一个 task 的首读变成引用。"""
    mw = _mw()
    path = "/mnt/user-data/workspace/judgments_draft_IN.json"
    task_a = FakeRequest("read_file", {"path": path}, task_id="task-a")
    task_b = FakeRequest("read_file", {"path": path}, task_id="task-b")

    _call(mw, task_a, BIG_A)
    write_a = FakeRequest("str_replace", {"path": path}, task_id="task-a")
    _call(mw, write_a, "OK")

    assert _call(mw, task_a, BIG_A) == BIG_A, "本 task 写后重读拿正文"
    assert _call(mw, task_b, BIG_A) == BIG_A, "另一 task 首读拿正文"


# --------------------------------------------------------------------------- #
# 异步路径（Task 14b-P1）                                                       #
# --------------------------------------------------------------------------- #
#
# ⚠️ 生产走的是 `awrap_tool_call`，而启用前的 14 项测试**全是同步路径**。两条路径是各自独立
# 的实现（不是 sync 包 async），任何一侧漏改都不会被同步测试发现——这类"测了没跑的那条路"
# 的缺口，是把 `enabled: true` 从"改一行配置"变成一个任务的原因之一。


async def _acall(mw, request, content: str) -> str:
    async def handler(_req):
        return ToolMessage(content=content, tool_call_id="call-1")

    result = await mw.awrap_tool_call(request, handler)
    return result.content if isinstance(result, ToolMessage) else str(result)


class TestAsyncPathMatchesSync:
    @pytest.mark.anyio
    async def test_disabled_config_never_dedupes(self):
        mw = ReadFileDedupMiddleware(config=ReadFileDedupConfig(enabled=False, min_chars=100))
        req = FakeRequest("read_file", {"path": "/x/a.md"})
        assert await _acall(mw, req, BIG_A) == BIG_A
        assert await _acall(mw, req, BIG_A) == BIG_A
        assert _dedup_cache_size() == 0

    @pytest.mark.anyio
    async def test_second_read_of_same_version_returns_reference(self):
        mw = _mw()
        req = FakeRequest("read_file", {"path": "/x/a.md"})
        assert await _acall(mw, req, BIG_A) == BIG_A
        second = await _acall(mw, req, BIG_A)
        assert second != BIG_A and "dedup" in second

    @pytest.mark.anyio
    async def test_content_change_is_a_cache_miss(self):
        mw = _mw()
        req = FakeRequest("read_file", {"path": "/x/a.md"})
        await _acall(mw, req, BIG_A)
        assert await _acall(mw, req, BIG_B) == BIG_B, "内容变了必须给正文"

    @pytest.mark.anyio
    async def test_write_invalidates_so_next_read_returns_body(self):
        mw = _mw()
        path = "/x/a.md"
        read_req = FakeRequest("read_file", {"path": path})
        await _acall(mw, read_req, BIG_A)
        assert await _acall(mw, read_req, BIG_A) != BIG_A
        await _acall(mw, FakeRequest("str_replace", {"path": path, "old_str": "A", "new_str": "B"}), "OK")
        assert await _acall(mw, read_req, BIG_A) == BIG_A, "异步路径也必须在写后失效"

    @pytest.mark.anyio
    async def test_first_read_in_another_task_returns_body(self):
        mw = _mw()
        path = "/mnt/skills/custom/criteria-parser/SKILL.md"
        task_a = FakeRequest("read_file", {"path": path}, task_id="task-a")
        task_b = FakeRequest("read_file", {"path": path}, task_id="task-b")
        assert await _acall(mw, task_a, BIG_A) == BIG_A
        assert await _acall(mw, task_a, BIG_A) != BIG_A
        assert await _acall(mw, task_b, BIG_A) == BIG_A, "跨 task 首读必须拿正文（异步路径）"

    @pytest.mark.anyio
    async def test_error_results_are_not_cached(self):
        mw = _mw()
        req = FakeRequest("read_file", {"path": "/x/a.md"})
        err = "Error: " + "E" * 3000
        assert await _acall(mw, req, err) == err
        assert await _acall(mw, req, err) == err, "错误结果不得被当成「未变化」"

    @pytest.mark.anyio
    async def test_small_reads_are_not_deduped(self):
        mw = _mw()
        req = FakeRequest("read_file", {"path": "/x/a.md"})
        small = "s" * 50
        assert await _acall(mw, req, small) == small
        assert await _acall(mw, req, small) == small

    @pytest.mark.anyio
    async def test_non_read_tools_pass_through(self):
        mw = _mw()
        req = FakeRequest("bash", {"command": "ls"})
        assert await _acall(mw, req, BIG_A) == BIG_A
        assert await _acall(mw, req, BIG_A) == BIG_A

    @pytest.mark.anyio
    async def test_sync_cache_is_shared_with_async(self):
        """两条路径共用模块级缓存：混用不得出现"同步读过、异步又给正文"。"""
        mw = _mw()
        req = FakeRequest("read_file", {"path": "/x/a.md"})
        assert _call(mw, req, BIG_A) == BIG_A
        assert await _acall(mw, req, BIG_A) != BIG_A


# --------------------------------------------------------------------------- #
# 引用文案（Task 14b-P1）                                                       #
# --------------------------------------------------------------------------- #
#
# 引用替代正文之后，这段文字就是模型**唯一**能依据的东西。它必须只包含可执行的读取动作。


class TestReferenceWording:
    def _reference_for(self, mw, req) -> str:
        _call(mw, req, BIG_A)
        return _call(mw, req, BIG_A)

    def test_reference_never_suggests_writing_to_the_file(self):
        """⛔ 旧文案写着"modify the file …"——把写操作当成让读成功的手段。

        卡住的 agent 会照做：为了绕开缓存去动产物。产物一动，下游门禁就有活儿干了。
        """
        text = self._reference_for(_mw(), FakeRequest("read_file", {"path": "/x/a.md"}))
        assert "modify the file" not in text
        assert "Do NOT write to the file" in text

    def test_reference_does_not_document_how_to_bypass_the_cache(self):
        """⛔ 上一版写着「给明确 start_line/end_line 可强制完整读」——本意是逃生阀。

        实测（thread `e3c15416`）模型把它当**操作指南**：一次被抑制的 30KB 整篇读变成
        **4 次分段读**（30-120 / 120-250 / 250-370 / 370-末），搬动的字节比省下的还多。
        写进提示里的绕过方式，就是会被用的绕过方式。
        """
        text = self._reference_for(_mw(), FakeRequest("read_file", {"path": "/x/a.md"}))
        assert "start_line" not in text, "不得再给出「用行范围强制完整读」的说明"
        assert "force a fresh full read" not in text

    def test_reference_offers_the_cheap_alternative(self):
        """只说"别读"不够——必须给出比再读一次更便宜的动作。"""
        text = self._reference_for(_mw(), FakeRequest("read_file", {"path": "/x/a.md"}))
        assert '"op": "get"' in text, "应指向 apply_json_patches 的 op:get 查单点"
        assert "costs more than it saves" in text

    def test_reference_points_at_the_externalized_artifact(self):
        """首读被外部化时，引用要给出**实际可读路径**，而不是让模型翻历史。

        外部化由 ToolOutputBudgetMiddleware 在**更外层**完成，本中间件看不到那条标记，
        因此按首读的 tool_call_id 回查 transcript。
        """
        mw = _mw()
        path = "/mnt/user-data/workspace/judgments_draft_IN.json"
        req = FakeRequest("read_file", {"path": path})
        _call(mw, req, BIG_A)
        artifact = "/mnt/user-data/workspace/.tool-results/read_file_call-1.txt"
        req.state = {
            "messages": [
                ToolMessage(
                    content=f"preview…\n\n[Full read_file output saved to {artifact} (3000 chars, ~750 tokens).]",
                    tool_call_id="call-1",
                )
            ]
        }
        text = _call(mw, req, BIG_A)
        assert artifact in text
        assert "read_file that path" in text

    def test_reference_falls_back_when_nothing_was_externalized(self):
        """没有外部化就**不能编**一个路径出来。"""
        text = self._reference_for(_mw(), FakeRequest("read_file", {"path": "/x/a.md"}))
        assert ".tool-results" not in text
        assert "Scroll back" in text

    def test_externalized_path_is_matched_by_call_id_not_filename(self):
        """同一文件读了多个 range 时，按 tool_call_id 匹配才不会指向错误的产物。"""
        mw = _mw()
        path = "/x/a.md"
        req = FakeRequest("read_file", {"path": path})
        req.tool_call["id"] = "call-range-1"
        _call(mw, req, BIG_A)
        req.state = {
            "messages": [
                ToolMessage(content="[Full read_file output saved to /x/.tool-results/other.txt (1 chars).]", tool_call_id="call-other"),
                ToolMessage(content="[Full read_file output saved to /x/.tool-results/mine.txt (1 chars).]", tool_call_id="call-range-1"),
            ]
        }
        text = _call(mw, req, BIG_A)
        assert "mine.txt" in text and "other.txt" not in text


# --------------------------------------------------------------------------- #
# 与 read-before-write 闸的不变量（Task 14b-P1）                                #
# --------------------------------------------------------------------------- #
#
# RBW 的 mark 由**磁盘回读**算出（`read_before_write_middleware._attach_read_mark` 里
# `self._content_reader(...)`），与消息正文无关。因此去重引用照样带得到有效 mark ——
# 这是**有意保留**的：清掉它等于逼模型重读一遍它已经拿着的内容，正是本中间件要消除的浪费。


class TestReadBeforeWriteInvariants:
    def test_dedup_reference_preserves_the_read_mark(self):
        mw = _mw()
        req = FakeRequest("read_file", {"path": "/x/a.md"})
        mark = {"path": "/x/a.md", "hash": "abc123"}

        def handler(_req):
            return ToolMessage(content=BIG_A, tool_call_id="call-1", additional_kwargs={"deerflow_read_mark": mark})

        assert mw.wrap_tool_call(req, handler).content == BIG_A
        second = mw.wrap_tool_call(req, handler)
        assert second.content != BIG_A, "第二次读应给引用"
        assert second.additional_kwargs["deerflow_read_mark"] == mark, "mark 必须原样带过去"
        assert second.additional_kwargs["read_file_dedup"] is True

    def test_read_then_str_replace_then_read_sees_the_edit(self):
        """read → str_replace → read：第二次读必须看到改动。

        两道保险同时成立：写操作会失效该路径的全部条目，而且改动后的内容哈希本来就不同 ——
        命中过期内容会让模型接着去改已经不存在的文本。
        """
        mw = _mw()
        path = "/x/a.md"
        read_req = FakeRequest("read_file", {"path": path})
        assert _call(mw, read_req, BIG_A) == BIG_A
        assert _call(mw, read_req, BIG_A) != BIG_A
        _call(mw, FakeRequest("str_replace", {"path": path, "old_str": "A", "new_str": "B"}), "OK")
        assert _call(mw, read_req, BIG_B) == BIG_B, "改动后的正文必须完整给出"
        assert _call(mw, read_req, BIG_B) != BIG_B, "新版本的第二次读才去重"

    def test_apply_json_patches_also_invalidates(self):
        """对象级编辑（Phase 2 的改判唯一手段）同样要失效缓存。"""
        mw = _mw()
        path = "/x/judgments_draft_IN.json"
        read_req = FakeRequest("read_file", {"path": path})
        _call(mw, read_req, BIG_A)
        assert _call(mw, read_req, BIG_A) != BIG_A
        _call(mw, FakeRequest("apply_json_patches", {"path": path, "patches": []}), "OK")
        assert _call(mw, read_req, BIG_A) == BIG_A


# --------------------------------------------------------------------------- #
# 引用必须指向**还拿得到**的正文(thread 7512ebd2)                              #
# --------------------------------------------------------------------------- #
#
# 引用文案让模型「翻回上一次读取」。可子代理在两次读之间被压缩过,那条消息已经不在
# 它的上下文里 —— scroll back 无处可翻,`op: get` 也取不回一份 Markdown 规范。
# 实测(thread 7512ebd2,判定 task `...5434-retry1`):judgment-schema.md 与
# schema_example.json 被这样挡掉,子代理原话「the dedup system thinks I read these in
# this run, but I did not (this is a fresh context)」,随后自创输出 schema 与文件名,
# 整单被产物闸作废。
#
# 判据是三态的:transcript 查得到首读 → 给引用;查不到 → 放行正文;
# **压根没法查**(无 state / 无 messages)→ 保持既有行为,不能把「不知道」读成「没有」。


class TestReferenceRequiresAReachablePayload:
    def _seen_once(self, mw, req, content: str = BIG_A) -> None:
        assert _call(mw, req, content) == content

    def test_payload_passes_through_when_the_first_read_was_compacted_away(self):
        mw = _mw()
        path = "/mnt/skills/custom/eligibility-judgment/references/judgment-schema.md"
        req = FakeRequest("read_file", {"path": path}, task_id="call_00_x-retry1")
        self._seen_once(mw, req)

        # 压缩后的 transcript:首读的 ToolMessage 没了,只剩后来的消息。
        req.state = {"messages": [ToolMessage(content="unrelated later tool output", tool_call_id="call-99")]}

        assert _call(mw, req, BIG_A) == BIG_A, "首读已被压缩删除时必须给正文,不能给悬空引用"

    def test_externalized_payload_still_gets_a_reference_after_compaction(self):
        """落盘过就仍然拿得到 —— 这种情况引用照旧,并且指向磁盘路径。"""
        mw = _mw()
        path = "/mnt/user-data/workspace/criteria_judge_IN.json"
        req = FakeRequest("read_file", {"path": path})
        self._seen_once(mw, req)
        artifact = "/mnt/user-data/workspace/.tool-results/read_file_call-1.txt"
        req.state = {
            "messages": [
                ToolMessage(
                    content=f"preview…\n\n[Full read_file output saved to {artifact} (3000 chars).]",
                    tool_call_id="call-1",
                )
            ]
        }

        text = _call(mw, req, BIG_A)

        assert text != BIG_A
        assert artifact in text

    def test_reference_is_kept_when_the_first_read_is_still_in_context(self):
        mw = _mw()
        req = FakeRequest("read_file", {"path": "/x/a.md"})
        self._seen_once(mw, req)
        req.state = {"messages": [ToolMessage(content=BIG_A, tool_call_id="call-1")]}

        assert _call(mw, req, BIG_A) != BIG_A

    def test_unsearchable_state_keeps_the_existing_reference_behaviour(self):
        """没有 state 可查时不得改变行为:那是「未知」,不是「已丢」。"""
        mw = _mw()
        req = FakeRequest("read_file", {"path": "/x/a.md"})
        self._seen_once(mw, req)

        assert req.state == {}
        assert _call(mw, req, BIG_A) != BIG_A

    def test_pass_through_does_not_drop_the_cache_entry(self):
        """放行是给正文,不是失效缓存:同一 run 里后续再读仍应能被去重。"""
        mw = _mw()
        req = FakeRequest("read_file", {"path": "/x/a.md"})
        self._seen_once(mw, req)
        req.state = {"messages": [ToolMessage(content="later", tool_call_id="call-99")]}
        assert _call(mw, req, BIG_A) == BIG_A

        req.state = {"messages": [ToolMessage(content=BIG_A, tool_call_id="call-1")]}
        assert _call(mw, req, BIG_A) != BIG_A
        assert _dedup_cache_size() == 1
