# 上下文爆炸优化开发计划

> 实施文档，对应 [context-explosion-analysis-and-requirements.md](./context-explosion-analysis-and-requirements.md)。
>
> 关联分支：`feature/gpt-team` | 创建时间：2026-07-11

---

## 1. 计划概览

本计划落地需求文档中的 5 个优化方案，分 4 个阶段实施，每阶段独立可验证、可回滚。

### 实施阶段

| 阶段 | 方案 | 优先级 | 预计耗时 | 依赖 |
|------|------|--------|----------|------|
| 阶段 1 | 方案 1 + 方案 5（read_file 外部化 + 上限） | P0 | 2 h | 无 |
| 阶段 2 | 方案 2（SOUL.md 读取纪律） | P0 | 1 h | 无 |
| 阶段 3 | 方案 4（summarization 阈值） | P1 | 0.5 h | 无 |
| 阶段 4 | 方案 3（read_file 工具去重缓存） | P1 | 4 h | 阶段 1 |
| 验证 | 集成验证 + 回归 | - | 2 h | 全部 |

**总预计**：9.5 h。建议按阶段 1 -> 2 -> 3 -> 验证 -> 4 顺序，阶段 4 风险最高可独立评估。

---

## 2. 阶段 1：read_file 外部化 + 输出上限（P0）

### 2.1 目标
- 方案 1：read_file 大输出纳入 tool_output 外部化
- 方案 5：降低 read_file 单次输出上限

### 2.2 实施步骤

#### 步骤 1.1：TDD - 先写外部化测试

**文件**：`backend/tests/test_tool_output_read_file_externalize.py`（新增）

```python
"""Tests: read_file output is externalized when exceeding threshold (方案 1)."""

import pytest
from langchain_core.messages import ToolMessage

from deerflow.agents.middlewares.tool_output_budget_middleware import _budget_tool_message
from deerflow.config.tool_output_config import ToolOutputConfig


def _make_read_file_msg(content: str, call_id: str = "call_1") -> ToolMessage:
    return ToolMessage(content=content, name="read_file", tool_call_id=call_id)


class TestReadFileExternalize:
    def test_large_read_file_output_externalized(self, tmp_path):
        """read_file output > externalize_min_chars is externalized."""
        config = ToolOutputConfig(
            enabled=True,
            externalize_min_chars=8000,
            exempt_tools=[],  # read_file NOT exempt
        )
        large_content = "x" * 20000  # 20K chars > 8K threshold
        msg = _make_read_file_msg(large_content)

        result = _budget_tool_message(msg, config, outputs_path=str(tmp_path), sandbox=None)

        # Should be externalized: content replaced with preview + file reference
        assert "[Full read_file output saved to" in result.content
        assert "Use read_file with start_line and end_line" in result.content
        assert "x" * 20000 not in result.content  # full content not in context

    def test_small_read_file_output_preserved(self, tmp_path):
        """read_file output < threshold is kept in full."""
        config = ToolOutputConfig(
            enabled=True,
            externalize_min_chars=8000,
            exempt_tools=[],
        )
        small_content = "small file content"  # < 8K
        msg = _make_read_file_msg(small_content)

        result = _budget_tool_message(msg, config, outputs_path=str(tmp_path), sandbox=None)

        assert result.content == small_content  # unchanged

    def test_read_file_exempt_keeps_full(self, tmp_path):
        """When read_file IS exempt (legacy config), output stays full."""
        config = ToolOutputConfig(
            enabled=True,
            externalize_min_chars=8000,
            exempt_tools=["read_file"],  # exempt
        )
        large_content = "x" * 20000
        msg = _make_read_file_msg(large_content)

        result = _budget_tool_message(msg, config, outputs_path=str(tmp_path), sandbox=None)

        assert result.content == large_content  # unchanged
```

#### 步骤 1.2：修改 config.yaml

**文件**：`config.yaml`

```yaml
# 改动 1：移除 read_file exempt
tool_output:
  enabled: true
  externalize_min_chars: 8000
  preview_head_chars: 2000
  preview_tail_chars: 1000
  fallback_max_chars: 30000
  fallback_head_chars: 8000
  fallback_tail_chars: 3000
  storage_subdir: .tool-results
  exempt_tools: []  # 原为 [read_file, read_file_tool]

# 改动 2：降低 read_file 上限
sandbox:
  read_file_output_max_chars: 20000  # 原 50000
```

#### 步骤 1.3：验证

```bash
cd backend && PYTHONPATH=. uv run pytest tests/test_tool_output_read_file_externalize.py -v
cd backend && PYTHONPATH=. uv run pytest tests/ -k "tool_output" -v
```

### 2.3 验收标准
- [ ] 新增测试 3 个全通过
- [ ] 现有 tool_output 相关测试无回归
- [ ] read_file > 8K 输出被外部化，context 仅留预览
- [ ] read_file < 8K 输出全量保留

### 2.4 风险与回滚
- **风险**：外部化后 agent 判定证据不全
- **回滚**：`exempt_tools: [read_file, read_file_tool]` 恢复
- **缓解**：小文件全量返回；预览保留头尾 3K 字符

---

## 3. 阶段 2：SOUL.md 读取纪律（P0）

### 3.1 目标
- 方案 2：lead agent 引入 read_file 读取纪律，从行为层消除重复读取

### 3.2 实施步骤

#### 步骤 2.1：SOUL.md 新增原则 11

**文件**：`backend/.deer-flow/agents/eligibility-screener/SOUL.md`

在原则 10（Todolist 状态管理）后新增：

```markdown
### 11. 上下文读取纪律（防止 context 爆炸）

- **Phase 间只用 summary**：每个 Phase 开头只读 `phase{N}_summary.json`，禁止重读前序 Phase 已读取的文件
- **同一文件同一 run 最多 read_file 一次**：需再次引用时从已有 ToolMessage 回顾，不重新 read
- **判定阶段证据只来自 `extraction.json`**：禁止为补证据重读 OCR 原文（与原则 7 联动）
- **按段读不全量读**：需要文件局部内容时用 `read_file(start_line, end_line)` 按段读，不读全量
- **大文件优先 grep 定位**：先 `grep -n` 定位行号，再 `read_file(start_line, end_line)` 精准读

违反以上规则等同于流程失败--会导致 context 爆炸与 budget 硬停。
```

#### 步骤 2.2：强化 Phase 间衔接说明

在各 Phase 的"前置"步骤补充"禁止重读"提示。例如 Phase 3 前置：

```markdown
**前置**：`read_file workspace/phase2_summary.json` 获取 OCR 和标准解析结果路径。
**禁止**：重读 Phase 2 已读过的 criteria_parsed.json / ocr_records.md（从 summary 获取路径后直接用，不重读内容）。
```

### 3.3 验收标准
- [ ] SOUL.md 新增原则 11，内容完整
- [ ] 各 Phase 前置步骤含"禁止重读"提示
- [ ] 无代码改动，纯文档

### 3.4 风险与回滚
- **风险**：prompt 软约束，agent 可能不遵守
- **回滚**：删除原则 11
- **缓解**：阶段 4 的工具层去重作为硬约束兜底

---

## 4. 阶段 3：summarization 阈值（P1）

### 4.1 目标
- 方案 4：降低 summarization 触发阈值，更早压缩 tool 输出堆积

### 4.2 实施步骤

#### 步骤 3.1：TDD - 先写 tool 配对保持测试

**文件**：`backend/tests/test_summarization_tool_pair_preservation.py`（新增）

```python
"""Tests: summarization at lower threshold preserves AI/Tool message pairs (方案 4)."""

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from deerflow.agents.middlewares.summarization_middleware import DeerFlowSummarizationMiddleware


def _make_messages_with_tool_pairs(count: int) -> list:
    """Build a message list with `count` AI+Tool pairs."""
    msgs = [HumanMessage(content="start")]
    for i in range(count):
        msgs.append(AIMessage(
            content=f"calling tool {i}",
            tool_calls=[{"name": "read_file", "args": {"path": f"f{i}"}, "id": f"call_{i}"}],
        ))
        msgs.append(ToolMessage(content=f"result {i}", name="read_file", tool_call_id=f"call_{i}"))
    return msgs


class TestSummarizationToolPairPreservation:
    def test_summarization_preserves_recent_tool_pairs(self):
        """After summarization, recent AI/Tool pairs stay together."""
        # Build config with low trigger
        mw = DeerFlowSummarizationMiddleware.from_app_config(...)
        messages = _make_messages_with_tool_pairs(20)

        result = mw._summarize(messages, ...)  # 触发 summarization

        # Preserved tail must not split AI/Tool pairs
        preserved = result.preserved_messages
        for i, msg in enumerate(preserved):
            if isinstance(msg, ToolMessage):
                # Preceding message must be the matching AIMessage
                assert i > 0
                assert isinstance(preserved[i-1], AIMessage)
                # tool_call_id matches
```

#### 步骤 3.2：修改 config.yaml

```yaml
summarization:
  enabled: true
  model_name: deepseek-v4-flash
  trigger:
  - type: tokens
    value: 50000  # 原 80000
  keep:
    type: messages
    value: 30
```

#### 步骤 3.3：验证

```bash
cd backend && PYTHONPATH=. uv run pytest tests/test_summarization_tool_pair_preservation.py -v
cd backend && PYTHONPATH=. uv run pytest tests/ -k "summarization" -v
```

### 4.3 验收标准
- [ ] 新增测试通过，证明 tool 配对不破坏
- [ ] 现有 summarization 测试无回归
- [ ] config 阈值降至 50000

### 4.4 风险与回滚
- **风险**：触发过频，压缩损失上下文
- **回滚**：`value: 80000` 恢复
- **缓解**：`keep: messages 30` 保留近期上下文

---

## 5. 阶段 4：read_file 工具去重缓存（P1）

### 5.1 目标
- 方案 3：read_file_tool 增加 per-run 去重缓存，二次读同一文件返回引用

### 5.2 实施步骤

#### 步骤 4.1：设计缓存机制

**缓存 key**：`(thread_id, run_id, normalized_path, start_line, end_line)`

**缓存行为**：
- 首次读取：正常读磁盘，缓存内容
- 二次读取同一 key：返回引用消息 `[Already read: {path} ({chars} chars). Content available in prior tool message. Use a different start_line/end_line to re-read a specific section.]`
- 文件被 `write_file`/`str_replace` 修改：失效该文件所有缓存
- run 结束：缓存随 thread_state 清理

#### 步骤 4.2：TDD - 先写去重测试

**文件**：`backend/tests/test_read_file_dedup.py`（新增）

```python
"""Tests: read_file dedup cache returns reference on re-read (方案 3)."""

import pytest
from deerflow.sandbox.tools import read_file_tool


class TestReadFileDedup:
    def test_first_read_returns_full_content(self, tmp_path):
        """First read of a file returns full content."""
        f = tmp_path / "test.md"
        f.write_text("hello world")
        # ... call read_file_tool with thread context
        result = read_file_tool(path=str(f), thread_id="t1", run_id="r1")
        assert "hello world" in result

    def test_second_read_same_range_returns_reference(self, tmp_path):
        """Second read of same file+range returns reference, not content."""
        f = tmp_path / "test.md"
        f.write_text("hello world")
        read_file_tool(path=str(f), thread_id="t1", run_id="r1")  # first
        result = read_file_tool(path=str(f), thread_id="t1", run_id="r1")  # second
        assert "Already read" in result
        assert "hello world" not in result  # content not re-served

    def test_second_read_different_range_returns_content(self, tmp_path):
        """Re-read with different start_line/end_line returns content."""
        f = tmp_path / "test.md"
        f.write_text("\n".join(f"line {i}" for i in range(100)))
        read_file_tool(path=str(f), thread_id="t1", run_id="r1")  # full
        result = read_file_tool(path=str(f), thread_id="t1", run_id="r1", start_line=10, end_line=20)
        assert "line 10" in result  # different range served

    def test_write_file_invalidates_cache(self, tmp_path):
        """write_file to a cached file invalidates its cache."""
        f = tmp_path / "test.md"
        f.write_text("old content")
        read_file_tool(path=str(f), thread_id="t1", run_id="r1")
        write_file_tool(path=str(f), content="new content", ...)
        result = read_file_tool(path=str(f), thread_id="t1", run_id="r1")
        assert "new content" in result  # cache invalidated, fresh read

    def test_different_run_no_cache_sharing(self, tmp_path):
        """Cache is per-run; different run_id gets fresh read."""
        f = tmp_path / "test.md"
        f.write_text("hello")
        read_file_tool(path=str(f), thread_id="t1", run_id="r1")
        result = read_file_tool(path=str(f), thread_id="t1", run_id="r2")  # different run
        assert "hello" in result  # no cross-run caching
```

#### 步骤 4.3：实现 read_file_tool 去重缓存

**文件**：`backend/packages/harness/deerflow/sandbox/tools.py`

在 `read_file_tool` 中增加缓存逻辑：

```python
# Module-level cache: (thread_id, run_id, path, start, end) -> content chars count
_read_cache: dict[tuple, int] = {}
_cache_lock = threading.Lock()

def read_file_tool(path, ..., thread_id=None, run_id=None, start_line=None, end_line=None):
    # Resolve path, read content (existing logic)
    ...
    
    # Dedup check
    cache_key = (thread_id, run_id, normalized_path, start_line, end_line)
    with _cache_lock:
        if cache_key in _read_cache:
            cached_chars = _read_cache[cache_key]
            return f"[Already read: {path} ({cached_chars} chars). Content available in prior tool message. Use different start_line/end_line to re-read a specific section.]"
    
    # Read and cache
    content = ...
    with _cache_lock:
        _read_cache[cache_key] = len(content)
    
    return content
```

在 `write_file_tool` / `str_replace_tool` 中触发失效：

```python
def _invalidate_read_cache(path, thread_id, run_id):
    with _cache_lock:
        keys_to_remove = [k for k in _read_cache if k[0]==thread_id and k[1]==run_id and k[2]==path]
        for k in keys_to_remove:
            del _read_cache[k]
```

#### 步骤 4.4：确认 read_file_tool 能拿到 thread_id/run_id

**需核实**：read_file_tool 的签名是否已含 thread_id/run_id，或需从 runtime context 获取。若需改签名，要同步更新调用方。

### 5.3 验收标准
- [ ] 新增 5 个测试全通过
- [ ] 二次读同一文件同一范围返回引用
- [ ] 不同范围读取返回内容
- [ ] write_file 后缓存失效
- [ ] 不跨 run 缓存
- [ ] 现有 read_file 测试无回归

### 5.4 风险与回滚
- **风险**：缓存失效逻辑不全（str_replace 部分修改未失效）
- **回滚**：移除缓存逻辑，恢复原 read_file_tool
- **缓解**：write_file + str_replace 都触发失效；缓存 key 含 run_id 自动清理

---

## 6. 集成验证

### 6.1 重跑 aca54c56 会话

1. 启动服务，打开 `localhost:3000/workspace/agents/eligibility-screener/chats/aca54c56-dcda-4d6c-8568-7776fc1d8803`
2. 重新发起同样任务（3 份 PDF 入排筛选）
3. 观察：
   - runs 表总 token < 60 万
   - input 占比 < 85%
   - subagent 占比 < 60%
   - 无连续 `[TOKEN BUDGET EXCEEDED]`
   - 报告正常产出
   - 进度推进到 P5

### 6.2 查询验证

```bash
sqlite3 backend/.deer-flow/data/deerflow.db \
  "SELECT run_id, total_tokens, total_input_tokens, subagent_tokens, status
   FROM runs WHERE thread_id='aca54c56-dcda-4d6c-8568-7776fc1d8803'
   ORDER BY created_at DESC LIMIT 1;"
```

### 6.3 回归测试

```bash
cd backend && PYTHONPATH=. uv run pytest tests/ -q --ignore=tests/test_setup_wizard.py
cd frontend && pnpm check
```

---

## 7. 验收检查清单

### 阶段 1
- [ ] `test_tool_output_read_file_externalize.py` 3 测试通过
- [ ] `config.yaml` exempt_tools 为空
- [ ] `read_file_output_max_chars: 20000`

### 阶段 2
- [ ] SOUL.md 新增原则 11
- [ ] 各 Phase 前置含"禁止重读"

### 阶段 3
- [ ] `test_summarization_tool_pair_preservation.py` 通过
- [ ] `summarization.trigger.value: 50000`

### 阶段 4
- [ ] `test_read_file_dedup.py` 5 测试通过
- [ ] read_file_tool 实现缓存
- [ ] write_file/str_place 触发失效

### 集成
- [ ] 重跑会话总 token < 60 万
- [ ] input 占比 < 85%
- [ ] 报告正常产出
- [ ] 现有测试无回归

---

## 8. 实施顺序与决策点

### 8.1 推荐顺序

```
阶段 1（P0，2h）──> 阶段 2（P0，1h）──> 阶段 3（P1，0.5h）
                                              │
                                              ▼
                                         集成验证（1h）
                                              │
                                              ▼
                                   阶段 4（P1，4h，独立评估）
```

### 8.2 决策点

**决策点 1**（阶段 1 后）：观察 read_file 外部化是否影响 agent 判定。
- 若报告产出正常 -> 继续阶段 2
- 若判定证据不全 -> 调高 `externalize_min_chars` 至 12000，或对特定文件 exempt

**决策点 2**（阶段 4 前）：评估阶段 1-3 效果。
- 若已达成 token < 60 万目标 -> 阶段 4 可选（作为硬约束兜底）
- 若仍超 -> 实施阶段 4

**决策点 3**（阶段 4 中）：若 read_file_tool 签名改动影响大。
- 改从 runtime context 获取 thread_id/run_id（不改签名）
- 或放弃阶段 4，仅靠 prompt 纪律（阶段 2）

---

## 9. 文档更新

实施完成后更新：
- [ ] `docs/eligibility-screener-fix-changelog.md` 新增第 8 章（context 爆炸优化）
- [ ] `docs/context-explosion-analysis-and-requirements.md` 补充实测验证结果
- [ ] `backend/AGENTS.md` 若涉及 tool_output 配置说明变更

---

## 10. 附录：预期效果对比

| 指标 | 优化前（Run 2） | 阶段 1-3 后 | 全部实施后 |
|------|------|------|------|
| 单 run 总 token | 3,021,765 | ~800,000 | < 600,000 |
| input 占比 | 97.7% | ~90% | < 85% |
| 每次调用 input | 177,751 | ~100,000 | < 80,000 |
| subagent 占比 | 84% | ~70% | < 60% |
| budget 硬停循环 | 3 次 | 0-1 次 | 0 次 |
| read_file 重复读 | 多次 | 减少 | 0 次（缓存） |
| 报告产出 | 卡 P1 | 正常 | 正常 |

---

## 11. 风险登记册

| ID | 风险 | 概率 | 影响 | 阶段 | 缓解 | 负责 |
|----|------|------|------|------|------|------|
| R1 | read_file 外部化致证据不全 | 中 | 高 | 1 | 小文件全量；预览头尾；按段读 | - |
| R2 | prompt 纪律 agent 不遵守 | 中 | 中 | 2 | 阶段 4 工具层兜底 | - |
| R3 | summarization 频繁致上下文丢失 | 中 | 中 | 3 | keep=30；DurableContext | - |
| R4 | 缓存失效 bug | 中 | 中 | 4 | write/str_replace 失效；run_id 隔离；单测 | - |
| R5 | read_file_tool 签名改动影响调用方 | 低 | 中 | 4 | 从 runtime context 获取 | - |
| R6 | 集成验证不达标 | 中 | 高 | 验证 | 决策点 2 调整方案 | - |
