# "Input Too Long" Emergency Trim Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When the LLM gateway returns 400 "Input is too long for requested model", automatically trim the oldest conversation messages from the request and retry once, so the agent can continue working rather than failing or exiting.

**Architecture:** Single-file change in `LLMErrorHandlingMiddleware`. The middleware already owns the retry loop inside `wrap_model_call`/`awrap_model_call`. We add: (1) a new "too_long" error category to `_classify_error`, (2) a static `_trim_messages_for_retry` helper that drops old messages while preserving AI+Tool pairs, and (3) one retry attempt with the trimmed `ModelRequest` before falling back to the error message. No state is mutated — the trim is local to the request object. If the trim retry also fails, the existing error message path is used.

**Tech Stack:** Python 3.12, langchain `ModelRequest.override()`, `langchain_core.messages`. No new dependencies.

---

## Root Cause Analysis

**Failure chain without this fix:**
1. Agent reads a large document via multiple `read_file` calls → conversation history grows.
2. `SummarizationMiddleware.before_model` runs. If summarization is disabled, threshold not met, or fails silently, the full history is passed to the model.
3. LLM gateway returns `HTTP 400 – "Input is too long for requested model."`.
4. `LLMErrorHandlingMiddleware._classify_error` returns `(False, "generic")` — not retriable.
5. Middleware injects `AIMessage(content="LLM request failed: ...")` into state.
6. `todo_middleware.after_model` (with current branch fix) allows exit → task incomplete.

**Why existing fixes are insufficient:** The `todo_middleware` exit fix prevents an infinite retry loop, but the task fails. Users need the agent to actually complete the task, not just exit gracefully.

**This fix's approach:** When "too long" is detected at the `wrap_model_call` level, silently drop the oldest conversation messages from the *request* (not from persistent state) and retry once. If the trimmed request fits, the model responds and the agent continues normally. The trimmed-out messages are already summarized in the system prompt summary (if summarization is active) or are simply old context the model can work without.

---

## File Structure

- **Modify:** `backend/packages/harness/deerflow/agents/middlewares/llm_error_handling_middleware.py`
  - Add `_TOO_LONG_PATTERNS` constant
  - Add `"too_long"` branch in `_classify_error`
  - Add `_build_too_long_message` method
  - Add `_trim_messages_for_retry` static method
  - Modify `wrap_model_call` to handle `"too_long"` with one trim-retry
  - Modify `awrap_model_call` to handle `"too_long"` with one trim-retry
- **Modify:** `backend/tests/test_llm_error_handling_middleware.py`
  - Add `_FakeRequest` helper class
  - Add `TestTooLongHandling` test class

---

### Task 1: Classify "too long" as a new error category

**Files:**
- Modify: `backend/packages/harness/deerflow/agents/middlewares/llm_error_handling_middleware.py`
- Test: `backend/tests/test_llm_error_handling_middleware.py`

- [ ] **Step 1: Write failing tests for `_classify_error` with "too long" messages**

Add this class to `backend/tests/test_llm_error_handling_middleware.py`:

```python
class TestClassifyTooLong:
    def _mw(self):
        return LLMErrorHandlingMiddleware()

    def test_input_too_long_from_gateway(self):
        mw = self._mw()
        err = FakeError("Error code: 400 - {'message': 'Input is too long for requested model.'}", status_code=400)
        retriable, reason = mw._classify_error(err)
        assert not retriable
        assert reason == "too_long"

    def test_openai_context_length_exceeded(self):
        mw = self._mw()
        err = FakeError("This model's maximum context length is 16385 tokens. However, your messages resulted in 20000 tokens.")
        retriable, reason = mw._classify_error(err)
        assert not retriable
        assert reason == "too_long"

    def test_max_tokens_exceeded(self):
        mw = self._mw()
        err = FakeError("max_tokens is too large: 8192. This model supports at most 4096 completion tokens")
        retriable, reason = mw._classify_error(err)
        assert not retriable
        assert reason == "too_long"

    def test_generic_400_not_classified_as_too_long(self):
        """A plain 400 without too-long keywords must remain 'generic'."""
        mw = self._mw()
        err = FakeError("Bad request", status_code=400)
        retriable, reason = mw._classify_error(err)
        assert reason == "generic"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd backend && PYTHONPATH=. uv run pytest tests/test_llm_error_handling_middleware.py::TestClassifyTooLong -v
```

Expected: 3 FAILED (reason != "too_long"), 1 PASSED (generic_400 passes by default).

- [ ] **Step 3: Add `_TOO_LONG_PATTERNS` constant and update `_classify_error`**

In `llm_error_handling_middleware.py`, add after `_BUSY_PATTERNS`:

```python
_TOO_LONG_PATTERNS = (
    "input is too long",
    "maximum context length",
    "too many tokens",
    "tokens in your messages",
    "context_length_exceeded",
    "context length exceeded",
    "reduce the length",
    "request is too large",
)
```

In `_classify_error`, add this block **before** the `return False, "generic"` line:

```python
        if _matches_any(lowered, _TOO_LONG_PATTERNS):
            return False, "too_long"
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd backend && PYTHONPATH=. uv run pytest tests/test_llm_error_handling_middleware.py::TestClassifyTooLong -v
```

Expected: all 4 PASSED.

- [ ] **Step 5: Commit**

```bash
git add backend/packages/harness/deerflow/agents/middlewares/llm_error_handling_middleware.py \
        backend/tests/test_llm_error_handling_middleware.py
git commit -m "feat(middleware): classify 'input too long' as new 'too_long' error category"
```

---

### Task 2: Add `_trim_messages_for_retry` helper

**Files:**
- Modify: `backend/packages/harness/deerflow/agents/middlewares/llm_error_handling_middleware.py`
- Test: `backend/tests/test_llm_error_handling_middleware.py`

- [ ] **Step 1: Write failing tests for `_trim_messages_for_retry`**

First, add the `_FakeRequest` helper near the top of the test file (after the imports):

```python
class _FakeRequest:
    """Minimal stand-in for ModelRequest that supports override(messages=...)."""

    def __init__(self, messages: list):
        self.messages = messages

    def override(self, **kwargs) -> "_FakeRequest":
        return _FakeRequest(kwargs.get("messages", self.messages))
```

Then add this test class:

```python
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage


def _human(text="q"):
    return HumanMessage(content=text)


def _ai(tool_calls=None):
    msg = AIMessage(content="")
    if tool_calls:
        msg.tool_calls = [{"id": tc, "name": "read_file", "args": {}} for tc in tool_calls]
    return msg


def _tool(tool_call_id="c1"):
    return ToolMessage(content="result", tool_call_id=tool_call_id)


class TestTrimMessagesForRetry:
    def _mw(self):
        return LLMErrorHandlingMiddleware()

    def test_drops_oldest_messages(self):
        """Trims roughly half of messages from the front."""
        msgs = [_human("q1"), _ai(), _human("q2"), _ai(), _human("q3"), _ai()]
        result = LLMErrorHandlingMiddleware._trim_messages_for_retry(msgs)
        assert result is not None
        assert len(result) < len(msgs)
        # Preserved messages should be the newest ones
        assert result[-1] is msgs[-1]

    def test_never_drops_below_min_keep(self):
        """With very few messages, returns None (can't reduce further)."""
        msgs = [_human(), _ai()]
        result = LLMErrorHandlingMiddleware._trim_messages_for_retry(msgs, min_keep=4)
        assert result is None

    def test_result_never_starts_with_tool_message(self):
        """After trimming, the first preserved message must not be a ToolMessage."""
        msgs = [_human("q"), _ai(["c1"]), _tool("c1"), _human("q2"), _ai()]
        # Force dropping 1 message — that would leave [ai(c1), tool, human, ai]
        # which starts with an ai-with-tools message — pairs should be skipped
        result = LLMErrorHandlingMiddleware._trim_messages_for_retry(msgs, min_keep=3)
        assert result is not None
        first_type = getattr(result[0], "type", None)
        assert first_type != "tool"

    def test_single_pair_below_min_keep_returns_none(self):
        msgs = [_ai(["c1"]), _tool("c1")]
        result = LLMErrorHandlingMiddleware._trim_messages_for_retry(msgs, min_keep=4)
        assert result is None

    def test_empty_messages_returns_none(self):
        assert LLMErrorHandlingMiddleware._trim_messages_for_retry([]) is None
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd backend && PYTHONPATH=. uv run pytest tests/test_llm_error_handling_middleware.py::TestTrimMessagesForRetry -v
```

Expected: all FAILED (`_trim_messages_for_retry` does not exist yet).

- [ ] **Step 3: Implement `_trim_messages_for_retry`**

Add this static method to `LLMErrorHandlingMiddleware`, just before `_classify_error`:

```python
    @staticmethod
    def _trim_messages_for_retry(
        messages: list,
        min_keep: int = 4,
        drop_fraction: float = 0.5,
    ) -> list | None:
        """Return a trimmed copy of messages by dropping the oldest entries.

        Drops up to `drop_fraction` of messages from the front, but never
        reduces the list below `min_keep` entries. After dropping, advances
        past any leading ToolMessages so the preserved section always starts
        with a non-Tool message (prevents orphaned tool responses).

        Returns None when no reduction is possible (list is already at or
        below min_keep, or all candidates are ToolMessages).
        """
        if not messages or len(messages) <= min_keep:
            return None

        n_to_drop = max(1, int(len(messages) * drop_fraction))
        n_to_drop = min(n_to_drop, len(messages) - min_keep)

        trimmed = messages[n_to_drop:]

        # Advance past leading ToolMessages to avoid orphaned tool responses
        while trimmed and getattr(trimmed[0], "type", None) == "tool":
            trimmed = trimmed[1:]

        if not trimmed or trimmed is messages:
            return None

        return trimmed
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd backend && PYTHONPATH=. uv run pytest tests/test_llm_error_handling_middleware.py::TestTrimMessagesForRetry -v
```

Expected: all 5 PASSED.

- [ ] **Step 5: Commit**

```bash
git add backend/packages/harness/deerflow/agents/middlewares/llm_error_handling_middleware.py \
        backend/tests/test_llm_error_handling_middleware.py
git commit -m "feat(middleware): add _trim_messages_for_retry for emergency context reduction"
```

---

### Task 3: Add "too long" user message and wire retry in `wrap_model_call`

**Files:**
- Modify: `backend/packages/harness/deerflow/agents/middlewares/llm_error_handling_middleware.py`
- Test: `backend/tests/test_llm_error_handling_middleware.py`

- [ ] **Step 1: Write failing tests for the retry behavior**

```python
class TestTooLongRetry:
    """wrap_model_call/awrap_model_call must trim and retry on 'too_long' errors."""

    def _too_long_error(self):
        return FakeError(
            "Error code: 400 - {'message': 'Input is too long for requested model.'}",
            status_code=400,
        )

    def _make_request(self, n_messages: int = 8):
        msgs = []
        for i in range(n_messages):
            msgs.append(HumanMessage(content=f"q{i}"))
            msgs.append(AIMessage(content=f"a{i}"))
        return _FakeRequest(msgs)

    def test_sync_retries_with_trimmed_request_on_too_long(self, monkeypatch):
        """wrap_model_call retries once with fewer messages when too_long."""
        mw = LLMErrorHandlingMiddleware()
        monkeypatch.setattr("time.sleep", lambda _: None)

        received_message_counts = []

        def handler(request):
            received_message_counts.append(len(request.messages))
            if len(received_message_counts) == 1:
                raise self._too_long_error()
            return AIMessage(content="ok")

        request = self._make_request(n_messages=8)
        result = mw.wrap_model_call(request, handler)

        assert isinstance(result, AIMessage)
        assert result.content == "ok"
        assert len(received_message_counts) == 2
        # Second attempt must have fewer messages than the first
        assert received_message_counts[1] < received_message_counts[0]

    def test_async_retries_with_trimmed_request_on_too_long(self, monkeypatch):
        """awrap_model_call retries once with fewer messages when too_long."""
        import asyncio

        mw = LLMErrorHandlingMiddleware()
        monkeypatch.setattr("asyncio.sleep", lambda _: asyncio.coroutine(lambda: None)())

        received_message_counts = []

        async def handler(request):
            received_message_counts.append(len(request.messages))
            if len(received_message_counts) == 1:
                raise self._too_long_error()
            return AIMessage(content="async ok")

        request = self._make_request(n_messages=8)
        result = asyncio.run(mw.awrap_model_call(request, handler))

        assert isinstance(result, AIMessage)
        assert result.content == "async ok"
        assert len(received_message_counts) == 2
        assert received_message_counts[1] < received_message_counts[0]

    def test_sync_returns_error_message_when_trim_does_not_help(self, monkeypatch):
        """If trimmed retry also fails with too_long, returns user-facing error."""
        mw = LLMErrorHandlingMiddleware()
        monkeypatch.setattr("time.sleep", lambda _: None)

        def handler(request):
            raise self._too_long_error()

        request = self._make_request(n_messages=8)
        result = mw.wrap_model_call(request, handler)

        assert isinstance(result, AIMessage)
        assert "too long" in result.content.lower() or "context" in result.content.lower()
        assert "LLM request failed" not in result.content

    def test_sync_returns_error_when_too_few_messages_to_trim(self, monkeypatch):
        """When there are too few messages to trim, returns error immediately."""
        mw = LLMErrorHandlingMiddleware()
        monkeypatch.setattr("time.sleep", lambda _: None)

        call_count = [0]

        def handler(request):
            call_count[0] += 1
            raise self._too_long_error()

        # Only 2 messages — can't trim below min_keep=4
        request = _FakeRequest([HumanMessage(content="q"), AIMessage(content="a")])
        result = mw.wrap_model_call(request, handler)

        assert isinstance(result, AIMessage)
        assert call_count[0] == 1  # no retry attempted
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd backend && PYTHONPATH=. uv run pytest tests/test_llm_error_handling_middleware.py::TestTooLongRetry -v
```

Expected: all FAILED (no "too_long" handling in wrap_model_call yet).

- [ ] **Step 3: Add `_build_too_long_message` method**

In `LLMErrorHandlingMiddleware`, add after `_build_user_message`:

```python
    @staticmethod
    def _build_too_long_message() -> str:
        return (
            "The conversation context has grown too long for the current model. "
            "The agent was unable to continue even after reducing the context. "
            "Please start a new conversation or break the task into smaller steps."
        )
```

- [ ] **Step 4: Modify `wrap_model_call` to handle "too_long"**

In `wrap_model_call`, replace the final `return AIMessage(content=self._build_user_message(exc, reason))` with:

```python
                if reason == "too_long":
                    trimmed = self._trim_messages_for_retry(request.messages)
                    if trimmed is not None:
                        trimmed_request = request.override(messages=trimmed)
                        logger.info(
                            "Context too long (%d msgs); retrying with %d msgs",
                            len(request.messages),
                            len(trimmed),
                        )
                        try:
                            response = handler(trimmed_request)
                            self._record_success()
                            return response
                        except Exception:
                            pass
                    return AIMessage(content=self._build_too_long_message())
                return AIMessage(content=self._build_user_message(exc, reason))
```

- [ ] **Step 5: Modify `awrap_model_call` to handle "too_long"**

Apply the same change to `awrap_model_call`. Replace the final `return AIMessage(content=self._build_user_message(exc, reason))` with:

```python
                if reason == "too_long":
                    trimmed = self._trim_messages_for_retry(request.messages)
                    if trimmed is not None:
                        trimmed_request = request.override(messages=trimmed)
                        logger.info(
                            "Context too long (%d msgs); retrying with %d msgs",
                            len(request.messages),
                            len(trimmed),
                        )
                        try:
                            response = await handler(trimmed_request)
                            self._record_success()
                            return response
                        except Exception:
                            pass
                    return AIMessage(content=self._build_too_long_message())
                return AIMessage(content=self._build_user_message(exc, reason))
```

- [ ] **Step 6: Run tests to verify they pass**

```bash
cd backend && PYTHONPATH=. uv run pytest tests/test_llm_error_handling_middleware.py::TestTooLongRetry -v
```

Expected: all 4 PASSED.

- [ ] **Step 7: Run the full test suite to check for regressions**

```bash
cd backend && PYTHONPATH=. uv run pytest tests/test_llm_error_handling_middleware.py -v
```

Expected: all existing tests still PASSED.

- [ ] **Step 8: Commit**

```bash
git add backend/packages/harness/deerflow/agents/middlewares/llm_error_handling_middleware.py \
        backend/tests/test_llm_error_handling_middleware.py
git commit -m "feat(middleware): retry with trimmed context on 'input too long' errors"
```

---

### Task 4: Update `todo_middleware` to distinguish "too long" from other LLM errors

**Background:** The current `todo_middleware` fix allows exit when ANY "LLM request failed" content is seen. With this new fix, the "too long" error will have a different message (from `_build_too_long_message`), so the existing check may still work — but we should verify and update it to be precise.

**Files:**
- Modify: `backend/packages/harness/deerflow/agents/middlewares/todo_middleware.py`
- Test: `backend/tests/test_todo_middleware.py`

- [ ] **Step 1: Verify current exit check covers both error messages**

Run the existing todo_middleware tests:

```bash
cd backend && PYTHONPATH=. uv run pytest tests/test_todo_middleware.py::TestLLMErrorExit -v
```

Expected: all PASSED (they should still pass since the check looks for "LLM request failed" which is in the old message, not the new too-long message).

- [ ] **Step 2: Add test for the new "too long" user message**

Add to the `TestLLMErrorExit` class in `backend/tests/test_todo_middleware.py`:

```python
    def test_too_long_message_allows_exit(self):
        """The new 'too long' message from LLMErrorHandlingMiddleware must also trigger exit."""
        mw = TodoMiddleware()
        state = {
            "messages": [AIMessage(
                content="The conversation context has grown too long for the current model.",
                tool_calls=[],
            )],
            "todos": [{"content": "task", "status": "in_progress"}],
        }
        assert mw.after_model(state, _make_runtime()) is None
```

- [ ] **Step 3: Run the new test to see if it passes or fails**

```bash
cd backend && PYTHONPATH=. uv run pytest tests/test_todo_middleware.py::TestLLMErrorExit::test_too_long_message_allows_exit -v
```

**If PASSED:** The existing check `"LLM request failed"` does NOT match the new "too long" message, but the test expects exit — this means the current check doesn't catch it. Proceed to step 4.
**If FAILED:** The existing check already catches it. Skip to step 5.

- [ ] **Step 4: Update the exit check in `todo_middleware.py` to cover both messages**

In `backend/packages/harness/deerflow/agents/middlewares/todo_middleware.py`, replace:

```python
        if "LLM request failed" in str(getattr(last_ai, "content", "")):
            return None
```

with:

```python
        _content = str(getattr(last_ai, "content", ""))
        if "LLM request failed" in _content or "conversation context has grown too long" in _content:
            return None
```

- [ ] **Step 5: Run all todo_middleware tests**

```bash
cd backend && PYTHONPATH=. uv run pytest tests/test_todo_middleware.py -v
```

Expected: all PASSED.

- [ ] **Step 6: Commit**

```bash
git add backend/packages/harness/deerflow/agents/middlewares/todo_middleware.py \
        backend/tests/test_todo_middleware.py
git commit -m "fix(middleware): extend LLM-error exit check to cover 'too long' message variant"
```

---

### Task 5: Run full test suite and verify

**Files:**
- No changes

- [ ] **Step 1: Run the complete backend test suite**

```bash
cd backend && PYTHONPATH=. uv run pytest tests/ -v --tb=short 2>&1 | tail -30
```

Expected: all existing tests PASS, new tests PASS.

- [ ] **Step 2: Verify the key new behaviors with a targeted run**

```bash
cd backend && PYTHONPATH=. uv run pytest \
  tests/test_llm_error_handling_middleware.py::TestClassifyTooLong \
  tests/test_llm_error_handling_middleware.py::TestTrimMessagesForRetry \
  tests/test_llm_error_handling_middleware.py::TestTooLongRetry \
  tests/test_todo_middleware.py::TestLLMErrorExit \
  -v
```

Expected: all PASSED.

---

## Self-Review Checklist

- [x] **Root cause covered**: `_classify_error` now detects "too long" patterns so the error is categorized and handled specifically — not silently dropped as "generic".
- [x] **Retry logic**: Both `wrap_model_call` and `awrap_model_call` attempt one retry with a trimmed request before falling through to the error message.
- [x] **No orphaned ToolMessages**: `_trim_messages_for_retry` advances past leading ToolMessages after trimming.
- [x] **Fallback is safe**: If trimming returns `None` (too few messages) or the retry also fails, the middleware returns `_build_too_long_message()` instead of `_build_user_message()`, giving a user-friendly and accurate error.
- [x] **`todo_middleware` exit covers new message**: Task 4 ensures both the old `"LLM request failed"` message and the new `"too long"` message both allow graceful exit if the trim-retry also fails.
- [x] **No placeholders**: All test code and implementation code is complete.
- [x] **Type consistency**: `_trim_messages_for_retry` returns `list | None`; callers check `is not None` before use.
- [x] **Existing tests protected**: `TestTooLongRetry.test_sync_returns_error_message_when_trim_does_not_help` explicitly covers the case where trim-retry also fails.
