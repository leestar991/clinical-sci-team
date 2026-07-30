# 长任务上下文优化 — 详细开发计划

## 1. 问题背景

在 eligibility-screener agent 处理入排筛选任务时，需要对 30-40 张扫描件图片进行 OCR 识别。当前流程导致以下问题：

| 问题 | 根因 | 影响 |
|------|------|------|
| `Payload Too Large` | base64 图片数据在消息历史中累积 | agent 运行中断 |
| `inputs are too large to trace` | 请求体超 AI Gateway tracing 缓冲区 | agent 运行中断 |
| checkpoint 数据暴涨 (21+ GB/thread) | viewed_images + messages 中 base64 持久化 | 前端无法加载、磁盘爆满 |

## 2. 已有的上下文管理机制

| 机制 | 位置 | 作用 | 不足 |
|------|------|------|------|
| SummarizationMiddleware | `middlewares/summarization_middleware.py` | 120K tokens 时压缩旧消息 | 触发前可能已累积 10+ 张图片 base64 |
| ToolOutputBudgetMiddleware | `middlewares/tool_output_budget_middleware.py` | 工具输出 >12K chars 外部化 | 不处理 ViewImageMiddleware 注入的 HumanMessage |
| ViewImageMiddleware | `middlewares/view_image_middleware.py` | 注入 base64 后清除 state | 已注入到 messages 中的 base64 不会被清除 |
| DurableContextMiddleware | `middlewares/durable_context_middleware.py` | 跨 summarization 保持关键信息 | 不涉及图片处理 |

## 3. 解决方案：`wrap_model_call` In-Flight 清理

### 3.1 核心思路

在每次发给 LLM 之前（`wrap_model_call` hook），对请求中的消息列表做 **in-flight** 处理：
- 识别历史轮次中由 ViewImageMiddleware 注入的图片 HumanMessage
- 将其中的 base64 数据替换为轻量路径摘要
- **只修改本次 LLM 请求**，不改 checkpoint state

### 3.2 为什么选择 `wrap_model_call` 而不是修改 state

| 方案 | 优点 | 缺点 |
|------|------|------|
| **wrap_model_call (推荐)** | 不改 state 结构、向后兼容、实现简单 | checkpoint 中仍保留 base64（但通过 summarization 最终清理） |
| 修改 state (before_model) | checkpoint 立即缩小 | 需要 RemoveMessage 操作，可能影响 checkpoint 连续性 |
| 外部化 base64 到文件 | 最彻底 | 实现复杂，跨中间件协调 |

### 3.3 关于 checkpoint 膨胀

`wrap_model_call` 不直接减小 checkpoint，但结合以下已有机制足够：
1. `viewed_images: {}` 清除（已实现）— 防止 state 中 viewed_images 字段累积
2. SummarizationMiddleware — 当 tokens 超阈值时清除旧消息（包括含 base64 的 HumanMessage）
3. 建议将 summarization trigger 从 120K 降低到 80K — 更早清理图片消息

## 4. 实现详细设计

### 4.1 修改文件

```
backend/packages/harness/deerflow/agents/middlewares/view_image_middleware.py
```

### 4.2 新增方法：`wrap_model_call`

```python
from typing import override
from langchain_core.messages import AIMessage, HumanMessage

class ViewImageMiddleware(AgentMiddleware[ViewImageMiddlewareState]):
    # ... 现有代码 ...

    def _is_image_injection_message(self, msg) -> bool:
        """判断一条消息是否是 ViewImageMiddleware 注入的图片消息。"""
        if not isinstance(msg, HumanMessage):
            return False
        content = msg.content
        if not isinstance(content, list):
            return False
        # 检查是否包含 image_url 类型的 content block
        has_image = any(
            isinstance(block, dict) and block.get("type") == "image_url"
            for block in content
        )
        # 检查是否有标志性文本
        has_marker = any(
            isinstance(block, dict) 
            and block.get("type") == "text" 
            and "Here are the images you've viewed" in block.get("text", "")
            for block in content
        )
        return has_image and has_marker

    def _extract_image_paths_from_content(self, content: list) -> list[str]:
        """从图片注入消息的 content blocks 中提取文件路径。"""
        paths = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text", "")
                # 格式为: "\n- **{path}** ({mime_type})"
                if text.startswith("\n- **") and "**" in text[5:]:
                    path = text.split("**")[1]
                    paths.append(path)
        return paths

    def _create_lightweight_image_reference(self, msg: HumanMessage) -> HumanMessage:
        """将含 base64 的图片消息替换为轻量路径引用。"""
        paths = self._extract_image_paths_from_content(msg.content)
        summary_text = f"[Previously viewed images: {', '.join(paths)}]" if paths else "[Previously viewed images]"
        return HumanMessage(
            content=[{"type": "text", "text": summary_text}],
            id=msg.id,
            additional_kwargs=msg.additional_kwargs,
        )

    def _find_current_turn_boundary(self, messages: list) -> int:
        """找到当前轮次的起始位置（最后一个 AIMessage 的 index）。"""
        for i in range(len(messages) - 1, -1, -1):
            if isinstance(messages[i], AIMessage):
                return i
        return len(messages)

    @override
    def wrap_model_call(self, state: ViewImageMiddlewareState, runtime):
        """在发给 LLM 之前，清理历史轮次的 base64 图片数据。
        
        只保留最近一轮的图片 base64（LLM 需要看到它来生成 OCR 结果），
        历史轮次的图片消息替换为轻量路径引用。
        
        这只修改本次 LLM 请求的 payload，不改 checkpoint state。
        """
        messages = state.get("messages", [])
        if not messages:
            return None

        # 找到当前轮次边界
        turn_boundary = self._find_current_turn_boundary(messages)

        # 遍历历史消息，替换图片注入消息
        modified_messages = []
        changed = False
        for i, msg in enumerate(messages):
            if (i < turn_boundary 
                and self._is_image_injection_message(msg)):
                # 替换为轻量版本
                modified_messages.append(self._create_lightweight_image_reference(msg))
                changed = True
            else:
                modified_messages.append(msg)

        if changed:
            return {"messages": modified_messages}
        return None

    @override
    async def awrap_model_call(self, state: ViewImageMiddlewareState, runtime):
        """异步版本 - 逻辑与同步版相同。"""
        return self.wrap_model_call(state, runtime)
```

### 4.3 逻辑流程图

```
每次 LLM 调用前:

messages = [...msg1, ...msg2, ..., AIMessage(view_image calls), ToolMessage, HumanMessage(base64 images)]
                                   ↑ turn_boundary

处理:
1. 找到 turn_boundary (最后一个 AIMessage 的位置)
2. 遍历 turn_boundary 之前的消息:
   - 如果是图片注入消息 → 替换为 "[Previously viewed images: path1, path2]"
   - 否则 → 保留不动
3. turn_boundary 之后的消息（含当前轮的图片）→ 全部保留

结果:
- 当前轮: LLM 看到完整 base64 → 可以做 OCR
- 历史轮: 只看到路径引用 → 节省 ~2-5 MB/轮 * N轮
```

### 4.4 效果估算

| 场景 | 修改前 | 修改后 |
|------|--------|--------|
| OCR 第 5 轮 (已看 8 张) | ~16-40 MB base64 在上下文 | ~2-5 MB (仅当前轮) + 200 bytes 引用 |
| OCR 第 20 轮 (已看 39 张) | ~78-195 MB → 触发 Payload Too Large | ~2-5 MB + 1 KB 引用 |
| checkpoint 大小 (512 turns) | 21 GB | 仍大（需 summarization），但 LLM 请求不超限 |

## 5. 配置优化建议（config.yaml 调整）

```yaml
# 建议调整：
summarization:
  trigger:
  - type: tokens
    value: 80000    # 从 120000 降至 80000，更早触发压缩（清理 base64 消息）

tool_output:
  externalize_min_chars: 8000    # 从 12000 降至 8000，更积极外部化
```

## 6. 测试计划

### 6.1 单元测试

新增测试文件：`backend/tests/test_view_image_middleware_context_cleanup.py`

```python
# 测试用例:
def test_wrap_model_call_strips_historical_base64():
    """历史轮次的图片 base64 应被替换为路径引用"""

def test_wrap_model_call_preserves_current_turn_images():
    """当前轮次的图片 base64 应保留完整"""

def test_wrap_model_call_no_change_without_images():
    """没有图片消息时返回 None（不修改）"""

def test_is_image_injection_message_detection():
    """正确识别 ViewImageMiddleware 注入的消息"""

def test_extract_image_paths():
    """正确提取图片路径列表"""
```

### 6.2 集成测试

1. 启动新的 eligibility-screener 会话
2. 上传 3 个 PDF（试验方案 + 筛选期病历 + 筛选期检查）
3. 观察 OCR 阶段是否正常完成
4. 确认不再触发 `Payload Too Large` 或 `inputs are too large to trace`
5. 检查最终报告输出正确

### 6.3 回归测试

```bash
cd backend && make test
```

## 7. 实施步骤

| 步骤 | 动作 | 预计耗时 |
|------|------|----------|
| 1 | 修改 `view_image_middleware.py`，添加 `wrap_model_call` + `awrap_model_call` | 30 min |
| 2 | 添加辅助方法 `_is_image_injection_message` / `_extract_image_paths_from_content` / `_create_lightweight_image_reference` / `_find_current_turn_boundary` | 20 min |
| 3 | 编写单元测试 | 30 min |
| 4 | 运行 `make test` 确认无回归 | 10 min |
| 5 | 实际启动 eligibility-screener 会话验证 | 15 min |
| 6 | (可选) 调整 config.yaml 的 summarization trigger | 5 min |

## 8. 风险评估

| 风险 | 影响 | 缓解 |
|------|------|------|
| wrap_model_call 的 messages 修改是否被 LangGraph 正确处理 | 如果不支持，图片清理不生效 | 参考 ToolOutputBudgetMiddleware 的 wrap_model_call 实现模式 |
| 路径引用是否足够让 LLM 理解上下文 | LLM 可能忘记之前图片内容 | OCR 结果已写入 ocr/ 目录的 .md 文件，LLM 可 read_file 回顾 |
| 当前轮边界判断错误 | 可能误清当前轮图片 | 用最后一个 AIMessage 作为保守边界 |

## 9. 未来扩展方向

- **Phase 2**: 将 base64 从 checkpoint messages 中也移除（需要 before_model 阶段用 RemoveMessage 替换）
- **Phase 3**: 通用的「大型 content block 外部化」机制——不仅限于图片，对任何超大 HumanMessage/ToolMessage content block 自动外部化到文件
