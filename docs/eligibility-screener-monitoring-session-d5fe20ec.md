# 会话监控记录：d5fe20ec（eligibility-screener）

> 监控时间：2026-07-14 19:12（CST），2026-07-14 19:30 复核修正
>
> 会话：`http://localhost:3000/workspace/agents/eligibility-screener/chats/d5fe20ec-2a03-41da-9b2c-1414fef302a0`
>
> thread_id：`d5fe20ec-2a03-41da-9b2c-1414fef302a0`　run_id：`22aabf03-70e4-42ed-b4e5-bb179f1a3ad5`
>
> 数据来源：`backend/.deer-flow/data/deerflow.db`（runs）、`backend/.deer-flow/checkpoints.db`（checkpoints）。`run_events.backend=memory` 未落盘，子代理 step 不可查。
>
> 状态：**仅观察记录，未修改任何代码/配置**（回退动作见 §4.1，已单独执行）。

---

## 0. 一句话结论（已修正）

run 在 Phase 2 派发 3 个 `task` 子代理后全部 `in_progress` 不返回，卡在 gpt-5-4 LLM 调用。run 级 inactivity watchdog 于 **11:22:33**（created 10:50:14 后约 32 分钟）将 run 置为 `timeout` 终止，thread 已释放可用。**run 非僵死 8 小时**（初版 19:12 监控误判，系 DB 旧快照；19:30 复核修正）。同时发现上一轮监控优化（#10 write_todos 去重）在生产环境破坏了 `write_todos` 工具，todos 从未写入（已回退）。

---

## 1. Run 概览（终态）

| 字段 | 值 |
|------|-----|
| status | `timeout`（watchdog 终止，terminal） |
| created_at | 2026-07-14 10:50:14 |
| updated_at | 2026-07-14 11:22:33（watchdog 触发） |
| error | `Run timed out after 600s of inactivity` |
| 模型 | gpt-5-4（fosunpharma ai-gateway，request_timeout 600s，max_retries 5） |
| recursion_limit | 1000 |
| llm_call_count | 9 |
| total_tokens | 521,882 |
| total_input / output | 504,128 / 17,754（**input 占 96.6%**） |
| lead_agent_tokens | 245,010 |
| subagent_tokens | 276,474 |
| middleware_tokens | 398（deepseek-v4-flash，title 生成，非 summarize） |
| message_count | 32（主 agent） |
| checkpoint step | 111，source=loop，writes=[] |

token_usage_by_model：
```
gpt-5.4-2026-03-05:    input 503,921  output 17,563  total 521,484
deepseek-v4-flash:     input 207      output 191     total 398
```

thread 状态：`timeout`（正常终态，worker.py:773 `final_status = "idle" if success else record.status.value`）。**无 pending/running/interrupted run 残留，新 run 可创建**。

---

## 2. 问题清单

### 🔴 P0-1　write_todos 工具在生产环境失效（上一轮 #10 改动引入的回归）

**证据**（checkpoint messages）：
- msg[3] AIMessage 发起 `write_todos` -> msg[4] ToolMessage：
  `Error: Tool 'write_todos' failed with TypeError: _adedup_write_todos() missing 1 required positional argument: 'runtime'. Continue with available context, or choose an alternative tool.`
- msg[7] AIMessage 再次 `write_todos` -> msg[8] 同样报错。
- `todos` channel 最终值 = `None`。

**根因**：上一轮监控优化的 #10（`TodoMiddleware` 用 `_dedup_write_todos`/`_adedup_write_todos` 包装基类 `write_todos`）在生产 graph 的 **async 执行路径**下，`ToolRuntime` 仍未被注入到 `_adedup_write_todos`。单元测试 `test_todo_middleware.py::TestTodoMiddlewareAgentGraphIntegration` 用 sync `graph.invoke` 跑通，掩盖了 async 路径的注入失败。`__annotations__` 覆盖为真实类型对象的修复对 sync 路径有效、对生产 async 路径无效。

**影响**：todos 从未写入，`TodoMiddleware` 的上下文丢失检测 / 提前退出阻止全部失效，agent 失去阶段跟踪能力。SOUL.md 原则 10（Phase 1 初始化 todos）无法执行。

**处置**：已回退 #10（见 §4.1）。

---

### 🔴 P0-2　3 个 task 子代理 in_progress 不返回（run 被 watchdog 终止）

**证据**：
- msg[31] AIMessage 并发派发 3 个 `task`（subagent_type=`general-purpose`）：
  1. 解析入排标准（/criteria-parser）
  2. OCR 病历 1-3 页
  3. OCR 病历 4-6 页
- `delegations` channel：3 条全部 `status=in_progress`，`created_at=2026-07-14T10:52:33Z`，无任何 terminal 状态。
- `__pregel_tasks`：3 个 `Send(node='tools', ...)` 仍挂起。
- run `updated_at` 当时观测停在 10:54:53（派发后约 2 分 20 秒）。**后经 DB 复核修正**：watchdog 实际在 11:22:33 将 run 置为 `timeout`（见 P0-3），并非僵死 8 小时。
- `checkpoints.db` 中该 thread 113 个 checkpoint 全部 `checkpoint_ns=''`（主 agent），**无子代理 checkpoint**（子代理用内存 checkpointer 或 `stream_subgraphs=false`，未落盘）。

**推断**：3 个子代理在独立 event loop 后台线程执行，卡在 gpt-5-4 LLM 调用（ai-gateway 无响应/慢）。子代理 `max_retries=5` × `request_timeout=600s` 的重试循环会耗到 ~50 分钟。最终由 run 级 inactivity watchdog（600s 无 stream 事件）在 11:22 连带终止整个 run。

---

### 🟠 P0-3（已修正）　run 被 watchdog 终止，但子代理级超时未独立生效

> **2026-07-14 修正**：初版监控（19:12）误判 run "僵死 8 小时 / watchdog 失效"。经 DB 复核，run 实际于 **11:22:33**（created 10:50:14 后约 32 分钟）被 inactivity watchdog 置为 `timeout`（`error = "Run timed out after 600s of inactivity"`），run 已 terminal，thread 已可用。

配置：
- `run_inactivity_timeout_seconds: 600`（10 分钟无 stream 事件应 `task.cancel()`）
- `subagents.timeout_seconds: 1800`（executor `future.result(timeout=1800)`）
- gpt-5-4 `request_timeout: 600`，`max_retries: 5`

**实际（修正后）**：
- ✅ **run 级 inactivity watchdog 生效**：11:22:33 将 run 置 `timeout`，error 记录 "Run timed out after 600s of inactivity"。run 已 terminal，thread 已释放（无 pending/running/interrupted 残留，新 run 可创建）。
- ⚠️ **子代理级超时（1800s）未独立生效**：run 在 600s 就被 watchdog 终止，子代理 `future.result(timeout=1800)` 未等到自身超时就被 run 级 cancel 连带终止。子代理卡死时**只能靠 run 级 watchdog（600s）兜底，子代理自身的 1800s timeout 形同虚设**（永远先被 run watchdog 触发）。
- ⚠️ **watchdog 触发延迟**：配置 600s，实际 32 分钟（1920s）才触发。`activity_event` 在子代理后台线程仍有零星事件重置 watchdog 计时，拖长实际终止时间。不影响最终终止，但延长卡死窗口。

**修正后的影响**：thread 已可用（非"永久转圈"）。但子代理卡死场景下，run 级 watchdog 是唯一兜底，且触发延迟可达数倍于配置值。子代理自身 timeout 设计失效。

**处置建议（修正后）**：
1. ~~短期：手动释放僵死 run~~ **不需要**--run 已被 watchdog 自动终止，thread 已可用。
2. 中期：子代理 timeout 需与 run watchdog 解耦独立生效（当前子代理 1800s 永远先被 run 600s 触发，设计失效）。
3. 中期：排查 watchdog 触发延迟（600s 配置 -> 1920s 实际），确认 `activity_event` 是否被子代理后台零星事件误重置。

---

### 🟠 P1-4　summarize 从未生效，context 单调膨胀

**证据**：9 轮 lead-agent LLM 调用 input_tokens 单调递增，`summary_text` channel 始终 `None`：

| msg | tool_calls | input_tokens |
|-----|-----------|--------------|
| 3 | write_todos, ls, bash | 19,583 |
| 7 | write_todos, bash, grep | 19,947 |
| 11 | read_file ×3 | 23,916 |
| 15 | write_file, bash, present_files | 29,183 |
| 19 | read_file ×3 | 31,689 |
| 23 | bash ×2, present_files | 34,970 |
| 27 | read_file, glob ×2 | 36,975 |
| 31 | task ×3（卡死） | 40,913 |

- deepseek-v4-flash 仅 398 token（input 207 + output 191），是 title 生成（`title.model_name: deepseek-v4-flash`），**不是 summarize**。
- **经 §3.2 验证**：`count_tokens_approximately` 估算 messages 总量=**15285** < trigger 50000，reported input 最大 40913 < 50000，`_should_summarize` 两路径均不触发。summarize 未生效与 #2/#3 守卫**无关**（守卫无机会生效）。

---

### 🟠 P1-5　input token 爆炸

total_input 504k / output 17.7k，input 占 96.6%。lead 245k + subagent 276k。典型"输入堆积、产出极少"。子代理卡死消耗（276k）占大头，修复 P0-2 后自然缓解。8 轮主 agent 调用 input 从 19.5k 涨到 40.9k，每轮净增 ~3k，说明历史在累积未被压缩（但未达 50k trigger，summarize 不触发属正常）。

---

### 🟡 P2-6　流程执行情况

- **Phase 1（预处理）正常完成**：ls/mkdir -> PDF 类型判定（pdf_classification.json）-> grep+read 提取入排章节（eligibility_criteria_raw.md）-> pdf_to_image.py 提取图片（筛选期病历 13 页、筛选期检查 27 页）-> present_files。
- **artifacts**：仅 `/mnt/user-data/outputs/pdf_classification.json` + `/mnt/user-data/outputs/eligibility_criteria_raw.md`（Phase 1 产出）。
- **Phase 2 卡死**：派发 3 子代理（入排解析 + 2 个 OCR 分片）后无返回，run 被 watchdog 终止。
- **todos 为空**（P0-1 回归导致），无阶段进度可见。**已回退 #10 修复**。
- **summary_text 为空**（P1-4，trigger 未达，正常）。
- 注意：SOUL.md Phase 2 要求"第一批 = 入排解析 + 前 2 个 OCR 分片"，实际派发 = 入排解析 + OCR1-3 + OCR4-6（2 个 OCR 分片），符合。

---

## 3. 问题与上一轮改动的关联分析（已验证）

### 3.1 验证方法

- 从 checkpoint 取出该 run 的 32 条 messages，用生产 token_counter（`count_tokens_approximately`）估算总量。
- 检查 `_should_summarize` 的两条触发路径（parent 实现）：`total_tokens >= 50000`（估算）或 `_should_summarize_based_on_reported_tokens`（reported input_tokens ≥ 50000）。
- 核对 `delegations` / `todos` / `summary_text` channel 的最终值。
- DB 复核 run 终态（status/updated_at/error）。

### 3.2 关联结论

| 问题 | 关联的上一轮改动 | 结论 | 证据 |
|------|------------------|------|------|
| **P0-1** write_todos 失效 | **#10 write_todos 去重** | 🔴 **确定回归**，已回退 | msg[4]/[8] 报 `_adedup_write_todos() missing 1 required positional argument: 'runtime'`；todos channel=None。async 生产路径 `ToolRuntime` 注入失败，sync 单测未覆盖。 |
| **P1-4** summarize 未生效 | #2/#3 守卫 | ✅ **无关**，无需回退代码 | `count_tokens_approximately` 估算 messages 总量=**15285** < trigger 50000；reported input 最大 40913 < 50000。`_should_summarize` 两路径都不触发，守卫无机会生效。middleware_tokens 398 是 title 生成（deepseek-v4-flash），非 summarize。 |
| **P1-5** input 爆炸 | 无直接关联 | 子代理 276k + lead 245k；子代理卡死消耗占大头。修复 P0-2 后自然缓解。 | token_usage_by_model：gpt-5.4 input 503921 / output 17563 |
| **P0-2** 子代理卡死 | 无（预存） | gpt-5-4 网关慢响应 + max_retries=5 放大；3 个 general-purpose 子代理 in_progress 不返回，run 被 watchdog 终止 | delegations 3 条全 in_progress，created_at 10:52:33；run 11:22:33 timeout |
| **P0-3** watchdog（已修正） | 无（预存） | run 级 watchdog **生效**（11:22 timeout）；但子代理级 1800s timeout 未独立生效（先被 run 600s 触发），且 watchdog 触发延迟 600s->1920s | run status=timeout, error="Run timed out after 600s of inactivity" |

### 3.3 #3 质量门槛的理论风险（ precautionary 回退理由）

虽经 3.2 验证 #2/#3 与本会话无关，但 #3 摘要质量门槛存在理论风险：若未来 summarize trigger 达标（messages > 50k）且摘要模型（deepseek-v4-flash）持续返回低质量摘要，`_is_low_quality_summary` 会反复跳过压缩 -> **永不压缩 + 重复调用摘要模型浪费 token + context 持续膨胀**。`_has_compacted` 因未成功而保持 False，cooldown 不生效，形成"反复触发却永不压缩"的死循环。按"优先回退未经充分生产验证的改动"原则，#2/#3 守卫 config 一并关闭。

---

## 4. 优化方案

### 4.1 已执行：回退（本轮）

| 项 | 处置 | 范围 |
|----|------|------|
| **#10 write_todos 去重** | ✅ **代码回退** | `git checkout HEAD -- todo_middleware.py`（恢复基类 write_todos）+ 删除 `test_todo_dedup.py`。TodoMiddleware 恢复原始（仅上下文丢失检测 + 提前退出阻止）。 |
| **#2/#3 守卫 config** | ✅ **config 关闭**（代码保留） | `config.yaml` / `config.example.yaml`：`cooldown_calls=0`、`min_messages_to_summarize=0`、`min_summary_chars=0`、`min_summary_body_chars=0`。`SummarizationConfig` 字段与中间件 `__init__` 保留（默认即 0），待独立验证后重新启用。 |
| #2-B keep(tokens 25000) | 保留 | 无害改进，token-based 保留窗口。 |
| #3-B summary_prompt | 保留 | 改善摘要质量（治本），无害。 |
| #5-A max_turns 150 | 保留 | 子代理卡死与 max_turns 无关（卡在 LLM 调用）。 |
| #7-A ls 隐藏 .tool-results | 保留 | 本会话未观察到违规。 |
| #4/#6/#8/#9 prompt | 保留 | 无害，未到 QC/判定阶段无法验证但不影响。 |

**验证**：`test_todo_middleware.py` + `test_summarization_*.py` 共 119 passed；config 解析守卫全 0。

### 4.2 待执行：P0-2 子代理卡死（预存问题，独立优化）

**根因**：3 个 general-purpose 子代理在独立 event loop 后台线程执行，卡在 gpt-5-4 LLM 调用（ai-gateway 慢/无响应）。`max_retries=5` × `request_timeout=600s` 单次子代理可卡 ~50 分钟。子代理 `timeout_seconds=1800` 由 `future.result(timeout=1800)` 强制，但 run 级 watchdog（600s）先触发终止整个 run，子代理自身 timeout 永远等不到。

**方案**：
1. **[config] 下调 gpt-5-4 `max_retries` 5->2**：网关慢响应时减少卡死放大（单子代理最坏 2×600=20 分钟 < subagent 1800s）。
2. **[后端] subagent timeout 与 run watchdog 解耦**：在 `SubagentExecutor` 后台线程内部用 `asyncio.wait_for` 包裹整体执行（独立于 run watchdog），超时即 cancel 子代理 LLM 调用并返回 terminal 状态，避免子代理卡死拖垮整个 run。
3. **[后端] 子代理 LLM 调用层加 timeout**：子代理 create_chat_model 时 `request_timeout` 之外，在 agent recursion 层加单步超时，避免单次 LLM 调用无限重试。
4. **[可观测] 子代理 step 落盘**：`run_events.backend` 由 memory 改 sqlite，或至少把 `subagent.start`/`subagent.end`/timeout 落盘，否则断连后子代理状态丢失、无法事后分析。

### 4.3 待执行：P0-3 watchdog 触发延迟 + 子代理 timeout 失效（预存问题，独立优化）

**根因（修正后）**：run 级 inactivity watchdog 生效但延迟严重（600s 配置 -> 1920s 实际），因 `activity_event` 被子代理后台线程零星事件误重置。子代理级 1800s timeout 设计失效（永远先被 run 600s 触发）。

**方案**：
1. **[后端] watchdog 心跳源收紧**：`activity_event` 只应由 lead-agent 主循环事件重置，子代理后台线程事件不应重置 run 级 watchdog（否则子代理卡死时零星重试事件会无限拖延 run 终止）。
2. **[后端] 子代理 timeout 独立**：见 §4.2 方案 2，子代理超时独立生效，不依赖 run watchdog 连带。
3. **[可观测] watchdog 触发时间记录**：记录 `watchdog_armed_at` -> `watchdog_fired_at`，便于监控触发延迟。

### 4.4 释放僵死 run：不需要

经 DB 复核，run `22aabf03` 已于 11:22:33 被 watchdog 置为 `timeout`（terminal），thread 已释放（无 pending/running/interrupted 残留）。**无需手动释放**。thread 当前 `timeout` 状态是正常终态，新 run 创建时会自动改回 `running`（services.py:518）。

### 4.5 #10 若未来重做的正确方式

若要重新实现 write_todos 去重，需：
1. 复用 langchain 基类**同款**模块级函数模式（`_write_todos(runtime: ToolRuntime[ContextT, PlanningState[ResponseT]], todos)`），注解用真实类型对象（非 future 字符串）。
2. **必须补 async 路径集成测试**（`graph.ainvoke` / `astream`），现有 sync `graph.invoke` 测试无法覆盖生产 async 注入。
3. 验证 `ToolNode._get_all_injected_args` 在 async `_execute_tool_async` 路径正确注入 `runtime`。

---

## 5. 附：数据提取命令

```bash
# run 终态
sqlite3 backend/.deer-flow/data/deerflow.db \
  "SELECT run_id,status,error,datetime(created_at),datetime(updated_at) FROM runs WHERE thread_id='d5fe20ec-2a03-41da-9b2c-1414fef302a0';"

# 确认无 inflight 残留（thread 是否可用）
sqlite3 backend/.deer-flow/data/deerflow.db \
  "SELECT run_id,status FROM runs WHERE thread_id='d5fe20ec-2a03-41da-9b2c-1414fef302a0' AND status IN ('pending','running','interrupted');"

# 最新 checkpoint 消息（需 langgraph SqliteSaver）
cd backend && .venv/bin/python -c "
from langgraph.checkpoint.sqlite import SqliteSaver
saver = SqliteSaver(sqlite3.connect('.deer-flow/checkpoints.db', check_same_thread=False))
tup = saver.get_tuple({'configurable':{'thread_id':'d5fe20ec-2a03-41da-9b2c-1414fef302a0'}})
print(tup.checkpoint['channel_values'].keys())
"
```
