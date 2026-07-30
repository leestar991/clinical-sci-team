# 子代理 timeout 与 run watchdog 优化计划（v2 · 已按代码复核修订）

> 来源：[eligibility-screener-monitoring-session-d5fe20ec.md](../eligibility-screener-monitoring-session-d5fe20ec.md)（会话 `d5fe20ec` 监控，2026-07-14）
>
> 关联：[eligibility-screener-monitoring-optimization-plan.md](./eligibility-screener-monitoring-optimization-plan.md)、[eligibility-screener-monitoring-optimization-changelog.md](../eligibility-screener-monitoring-optimization-changelog.md)
>
> 状态：待评审 / 待实施
>
> **v2 修订说明**：v1 的两处根因/落点与当前代码不符，已按逐行复核结果修正：
> 1. **P0-3 根因更正**——`task_tool` 轮询循环里的 `writer(...)` 是**消息增量门控**（仅在子代理产出新 AI 消息时写 `task_running`），并非无条件每 5s 写；且子代理 `run_config["callbacks"]` 只有 `[collector] + tracing`，**不含**父级 `_HeartbeatCallbackHandler`。因此重置 lead watchdog 的真正来源是"子代理真实进度（新消息）"，不是周期噪声。
> 2. **B1 落点更正**——`task_tool` 实际走 `execute_async()` → 线程池 `run_task()`（`executor.py:855-882`），不是 v1 指向的 `_execute_in_isolated_loop`。强制中断必须落在 `run_task` 的超时分支，并处理跨线程 cancel 编排。
> 3. **C1 提级**——因 A1 若"停止子代理进度重置 lead watchdog"，会让合法长子代理在 600s 被 run watchdog 误杀，故 C1（子代理级 inactivity watchdog）由 P2 加固提级为**主机制**，A1 相应弱化为"只透传真实进度、不做停滞判定"。

---

## 1. 背景与目标

监控会话 `d5fe20ec` 中，eligibility-screener 在 Phase 2 派发 3 个 `task` 子代理（入排解析 + 2 个 OCR 分片）后，子代理全部卡在 gpt-5-4 LLM 调用（ai-gateway 慢响应）不返回。run 级 inactivity watchdog 最终在 **11:22:33**（created 10:50:14 后约 32 分钟 ≈ 1920s）将 run 置为 `timeout` 终止，thread 已可用。但暴露两个独立缺陷：

1. **P0-2 子代理 timeout 失效**：配置的子代理 `timeout_seconds=1800` 形同虚设——超时分支的 `cancel_event`（协作式，卡在 `await LLM` 时不检查）与 `execution_future.cancel()`（对已运行协程无效）都打不断卡死的 astream 协程，实际靠 run 级 watchdog 兜底连带终止。
2. **P0-3 watchdog 触发延迟**：watchdog 配置 600s，实际 ~1920s 才触发。**更正后的根因**：子代理在卡死前会分批产出 AI 消息，每条新消息触发 `task_tool` 写一次 `task_running` custom chunk → lead astream 消费 → `activity_event.set()`，把 watchdog 计时反复重置到"最后一条消息 + 600s 窗口"；叠加 gpt-5-4 `max_retries=5` 的重试放大，拖到 ~1920s。（v1 归因的"每 5s 周期写"在代码中并不存在。）

### 1.1 优化目标

| 目标 | 衡量指标 |
|------|----------|
| 卡死子代理被就地终止 | 单个子代理停滞后由其自身 inactivity watchdog 在 ~300s cancel，状态变 TIMED_OUT，不拖累兄弟子代理 |
| 优雅降级 | 3 子代理中 1 个卡死时，另外 2 个正常完成并被 lead 综合，而非整 run 被连带 kill |
| 子代理 timeout 真正可强制中断 | 子代理 timeout/inactivity 触发后 astream 协程收到 CancelledError（跨线程编排到独立 loop） |
| 合法长子代理不被误杀 | 长 OCR/多轮子代理（真实产出）不被 lead run watchdog 在 600s 误终止 |
| 减少 LLM 卡死放大 | gpt-5-4 单次子代理 LLM 调用最坏耗时 < subagent timeout |
| 子代理可观测 | 子代理 start/end/timeout 落盘，断连后可事后分析 |

### 1.2 现状基线（已按代码逐行核对）

| 配置/机制 | 当前值 | 位置 |
|-----------|--------|------|
| `run_inactivity_timeout_seconds` | 600 | `config.yaml:6` |
| `subagents.timeout_seconds` | 1800（全局默认） | `config.yaml:178` |
| `subagents.agents.*.max_turns` | quality-control/report-writer 150，其余按子代理定义 | `config.yaml:194-224` |
| gpt-5-4 `request_timeout` | 600 | `config.yaml:76` |
| gpt-5-4 `max_retries` | 5 | `config.yaml:77` |
| `run_events.backend` | memory（不落盘） | `config.yaml:347` |

**关键代码事实（v2 已复核，标注 ✅ 确认 / ⚠️ v1 有误）**：

- ✅ **run watchdog 心跳源**：`_inactivity_watchdog`（`worker.py:81`）等待 `activity_event`，600s 无 set 则 `abort_event.set()` + `record.task.cancel()`。该 cancel 是打断卡死 LLM 的**唯一**手段（否则要等模型自身 `request_timeout`）。`activity_event` 由两处重置：
  1. `_stream_once` 每消费一个 lead astream chunk（含子代理 `task_running` custom chunk）调 `activity_event.set()`。
  2. `_HeartbeatCallbackHandler`（`worker.py:156-205`）在 `on_tool_start`/`on_tool_end`/`on_llm_start`/`on_llm_end`/`on_llm_new_token`/`on_chain_start` 均 ping。
- ⚠️ **task 工具轮询（v1 描述有误）**：`task_tool.py` 轮询循环里 `writer(...)` 只在两种情况写：① 启动时 `task_started` 一次；② `current_message_count > last_message_count`（子代理产出新 AI 消息）时写 `task_running`；③ 终态各写一次。循环体其余只有 `await asyncio.sleep(5)`，**没有无条件周期写**。因此 lead watchdog 被重置的真实来源 = 子代理**真实进度信号**，不是噪声。
- ⚠️ **子代理 callbacks 不含父心跳（v1 未提）**：子代理 `run_config["callbacks"] = [collector] + tracing_callbacks`（`executor.py:556-569`），**不包含**父级 `_HeartbeatCallbackHandler`。故子代理 LLM 生命周期事件不会直接 ping 父 `activity_event`；唯一透传通道就是上面的 `task_running` custom chunk。
- ✅ **子代理 timeout 协作式失效（P0-2）**：`_aexecute` 内 `async for chunk in agent.astream(...)`（`executor.py:627`）只在 chunk 迭代边界检查 `cancel_event`（`executor.py:632`）；卡在 `await LLM` 时不检查。`future.cancel()` 对已运行协程无效。
- ⚠️ **实际执行路径是 `execute_async`（v1 B1 落点有误）**：task_tool 调 `executor.execute_async()`（`task_tool.py:344`）→ 线程池 `run_task()`（`executor.py:855-882`）→ `execution_future.result(timeout=timeout_seconds)`。超时分支同样是 `cancel_event.set()` + `try_set_terminal(TIMED_OUT)` + `execution_future.cancel()`，与 `_execute_in_isolated_loop`（同步 `execute()` 路径，`executor.py:747-769`）是两条独立代码。**强制中断必须落在 `run_task`**。注意 `run_task` 在 `_scheduler_pool` 线程、`_aexecute`/astream 在独立事件循环线程，跨线程 cancel 需 `loop.call_soon_threadsafe`。
- ✅ **LLM 重试放大**：gpt-5-4 `max_retries=5` × `request_timeout=600s`，单次子代理 LLM 调用最坏 3000s，远超 subagent timeout 1800s，重试期间无法取消。每次重试 `on_llm_start` 还会 ping lead watchdog（叠加 P0-3 放大）。

---

## 2. 根因链路（代码级 · v2 更正）

### 2.1 P0-3 watchdog 触发延迟（600s 配置 → ~1920s 实际）

```
子代理执行期间分批产出 AI 消息（每完成一轮/一步）
  └─ task_tool 检测 message_count 增长 → writer 写 task_running（消息门控，非周期）
      └─ lead astream 产出 custom chunk → _stream_once 消费 → activity_event.set()
          └─ run watchdog 计时被重置到"最后一条新消息 + 600s"
子代理最终卡在 gpt-5-4 LLM 调用（ai-gateway 无响应）
  └─ 卡死期间无新消息 → task_tool 不再写 → 本应 600s 后触发
      └─ 但 gpt-5-4 max_retries=5 每次重试触发 on_llm_start
          └─ 若该心跳经由子代理路径可达父 activity_event 则继续放大
             （注：子代理 callbacks 不含父心跳，主放大来自"最后一批消息"的窗口 + 子代理 1800s timeout 边界事件）
  └─ 直到子代理 1800s timeout 后 task_tool 收到终态并停止 → 叠加约一个窗口 → 实际 ≈ 1920s
```

**更正要点**：真正的问题不是"周期噪声重置"，而是 **run watchdog 无法区分"子代理在真实产出（应保活）"与"子代理已卡死（应触发）"**——两者都表现为一段时间无新消息，只是后者会一直无消息直到 1800s。因此单靠"收紧 lead 心跳"无法既保活合法长子代理、又快速杀掉卡死子代理，必须在子代理侧引入停滞判定（见方案 C1）。

### 2.2 P0-2 子代理 timeout 无法强制中断

```
execute_async → 线程池 run_task（executor.py:855-882）
  execution_future = submit_to_isolated_loop(self._aexecute(...))
  execution_future.result(timeout=1800)
    └─ _aexecute 内 async for chunk in agent.astream(...)（executor.py:627）
        └─ 卡在 await LLM（gpt-5-4 max_retries=5 × 600s）
            └─ cancel_event 不被检查（只在 chunk 边界，executor.py:632）
超时后（run_task 超时分支）:
  result_holder.cancel_event.set()   # 协作式，astream 卡住时无效
  execution_future.cancel()          # 对已运行协程无效
  └─ astream 协程继续卡 LLM 重试
      └─ 直到 run 级 watchdog（被 2.1 延迟到 ~1920s）cancel 整个 run task 才连带终止
```

**结论**：子代理 1800s timeout 设计失效，实际靠 run 级 watchdog 兜底（且该 watchdog 还被延迟），且兜底是"整 run 连带 kill"而非"就地终止卡死子代理"。

---

## 3. 优化方案（v2）

> 设计原则：**停滞判定下沉到子代理自身**（C1 为主），**lead run watchdog 只做兜底**，**真实进度继续透传**（A1 弱化，不再"停止透传"）。B 组让子代理超时真正可强制中断。

### 方案 A — lead 心跳精确化（P0，配合 C1）

**目标**：lead run watchdog 只反映 lead 主循环 + 子代理**真实进度**的活动；不因 LLM 重试等非进度事件误重置，也不因"移除进度透传"而误杀合法长子代理。

#### A1（修订）：保留子代理真实进度透传，不改为"停止推送"

- **保留** `task_tool` 现有的消息门控 `task_running` 推送（子代理产出新消息 = 真进度，应当保活 lead watchdog）。**不采纳** v1 的"移除周期推送"——代码本就没有周期推送，且移除真实进度透传会误杀合法长子代理。
- 停滞检测交给 C1（子代理侧），而非在 lead 侧靠"缺少心跳"推断。
- （可选清理）确认轮询循环无其它隐式周期写；`task_started`/终态事件保持不变。

> 备选 A1'（心跳分类，非必需）：为 lead 引入 `subagent_activity_event` 与 `activity_event` 分离，watchdog 只看后者。鉴于 C1 已在子代理侧解决停滞判定，A1' 收益有限，暂不采纳。

#### A2：`_HeartbeatCallbackHandler` 收紧重置源（`worker.py:189-205`）

针对 LLM 重试放大（每次重试 `on_llm_start` 都 ping）：

- **保留** `on_llm_new_token`（真实 token 流 = 真进度）。
- **保留** `on_tool_start`/`on_tool_end`（工具执行 = 进度）。
- **`on_llm_start` 改为按 `run_id` 去重**：同一 LLM run 的首次调用 ping，重试不 ping。保留首次 ping 以覆盖 reasoning 模型"长思考后才出首 token"的场景。
- **移除** `on_llm_end` 的 ping（重试结束不是进度）。
- **移除** `on_chain_start` 的 ping（链路边界事件，非进度）。

**风险与缓解**：长而无 token 流的 LLM 调用可能误触发——保留 `on_llm_start` 首次 ping + `on_llm_new_token` 覆盖流式思考即可缓解。

---

### 方案 B — 子代理 timeout 强制中断（P0，核心，落点更正）

**目标**：子代理 timeout/inactivity 触发后，强制 cancel 卡在 LLM 的 astream 协程，而非依赖无效的协作式 `cancel_event` + `future.cancel()`。

#### B1（修订落点）：`_aexecute` 持有 astream Task 引用，`run_task` 超时分支跨线程强制 cancel

1. **在 `_aexecute`（`executor.py:627` 附近）** 把 `async for chunk in agent.astream(...)` 包成显式可 cancel 的迭代，并把当前运行的 astream task 引用挂到 `result_holder`（供外部跨线程 cancel）：

```python
# 伪代码（运行在独立事件循环线程内）
result_holder._astream_task = asyncio.current_task()  # 或包裹 astream 的子 task
result_holder._astream_loop = asyncio.get_running_loop()
try:
    async for chunk in agent.astream(state, config=run_config, context=context, stream_mode="values"):
        if result_holder.cancel_event.is_set():
            break
        ...  # 处理 chunk
except asyncio.CancelledError:
    # 强制中断卡在 await LLM 的协程后清理
    raise
```

2. **在 `run_task` 超时分支（`executor.py:868-880`）** 增加跨线程强制 cancel（`run_task` 在线程池线程，astream 在独立 loop 线程，必须 `call_soon_threadsafe`）：

```python
except FuturesTimeoutError:
    result_holder.cancel_event.set()
    astream_task = getattr(result_holder, "_astream_task", None)
    astream_loop = getattr(result_holder, "_astream_loop", None)
    if astream_task is not None and astream_loop is not None and not astream_task.done():
        astream_loop.call_soon_threadsafe(astream_task.cancel)
    result_holder.try_set_terminal(SubagentStatus.TIMED_OUT, error=...)
    execution_future.cancel()
```

3. **同步 `execute()` 路径的 `_execute_in_isolated_loop`（`executor.py:764-768`）** 做等价改造，保持两条路径行为一致。

> 备选 B1'：用 `asyncio.wait_for(astream 迭代, timeout)` 在子代理 loop 内部直接超时并 cancel。改动更集中（无需跨线程编排），但需处理迭代中途 cancel 的资源清理（`await astream.aclose()`）。**推荐优先评估 B1'**，其跨线程复杂度更低；若与现有 future 超时/轮询模型冲突再退回 B1。

#### B2：子代理 LLM 单步超时（`executor.py` / `create_chat_model`）

#### B2：子代理 LLM 单步超时（**实现中已被 C1 吸收**）

在 `stream_mode="values"` 下一个 chunk = 一个 super-step（LLM 调用 + 工具）。C1 的 per-chunk `asyncio.wait_for(inactivity_timeout)` 天然实现"单步超时"——单个 super-step 超过 `inactivity_timeout_seconds` 即被 cancel。故 B2 不单列实现，由 C1 覆盖；更细的纯 LLM 流层兜底另有 `models/factory.py:stream_chunk_timeout`（默认 240s）。

#### B3：config 下调 gpt-5-4 `max_retries` 5 → 2（`config.yaml:77`）

单次子代理 LLM 调用最坏 2×600=1200s，配合 C1（300s inactivity）与 B1 强制 cancel，卡死能在数百秒内被就地终止；同时减少 A2 场景的重试放大。

---

### 方案 C — 子代理级 watchdog 与可观测性（提级为主机制）

#### C1（提级 P0/P1）：子代理级 inactivity watchdog（`executor.py` `_aexecute`）

**这是修订后的核心机制**：给每个子代理 astream 加独立 inactivity watchdog——连续 N 秒（默认 300s）无新 chunk 则自动 `astream_task.cancel()`，子代理状态置 TIMED_OUT。

- 与 A1 配合：子代理**真实进度**（新 chunk/新消息）既重置自身 watchdog、又透传给 lead watchdog；**停滞**只由子代理自身 watchdog 判定并就地终止，不拖累兄弟子代理，也不误杀合法长子代理。
- 与 B1 复用同一 `_astream_task` cancel 机制（inactivity 到期 = 在子代理 loop 内 `astream_task.cancel()`，无需跨线程）。
- 阈值：默认 300s，允许按子代理类型差异化（OCR 子代理可放宽，见决策点 3）。

#### C2：`run_events.backend` memory → db（`config.yaml:355`）

子代理 `subagent.start`/`subagent.end`/timeout 落盘，否则断连后子代理状态丢失、无法事后分析（本次监控即因此无法看子代理 step）。**注意**：`run_events.backend` 的合法值是 `memory`/`db`/`jsonl`（无 `sqlite`）；本仓库 `database.backend=sqlite`，故设为 `db` 即经共享引擎持久化到 sqlite。

---

## 4. 实施优先级与排期（v2）

| 优先级 | 方案 | 改动位置 | 类型 | 预计 | 依赖 |
|--------|------|----------|------|------|------|
| **P0** | A2 heartbeat 收紧（on_llm_start 去重、移除 on_llm_end/on_chain_start） | `worker.py` | 后端 | 1h | - |
| **P0** | B3 gpt-5-4 max_retries 5→2 | `config.yaml` | config | 5min | - |
| **P0** | C1 子代理 inactivity watchdog（300s，就地 cancel） | `executor.py` | 后端 | 3h | - |
| **P0** | B1/B1' astream Task 强制 cancel（落点 `run_task` + `_aexecute`） | `executor.py` | 后端 | 3h | C1 复用 cancel 机制 |
| **P1** | A1 确认保留真实进度透传（不移除），清理隐式周期写 | `task_tool.py` | 后端 | 0.5h | C1 |
| **P1** | B2 LLM 单步超时 | `executor.py` | 后端 | 2h | B1 |
| **P2** | C2 run_events 落盘 | `config.yaml` | config | 5min | - |

**建议分批**：

- **批次1（P0，止血）**：A2 + B3 + C1。C1 直接让卡死子代理在 ~300s 就地终止（就算 B1 未完成，C1 的 astream_task.cancel 在同 loop 内即可生效），从根上解决"卡死拖到 1920s + 连带 kill"。回归风险中等（需测正常完成路径）。
- **批次2（P0/P1，强制中断补全）**：B1/B1' + B2 + A1。补齐外部 timeout 路径的强制 cancel 与 lead 侧透传语义确认。
- **批次3（P2，加固）**：C2。

> **相较 v1 的排期变化**：C1 从 P2 提到 P0 并前置；A1 从"移除周期推送（P0）"降级为"确认保留透传 + 清理（P1）"；B1 落点从 `_execute_in_isolated_loop` 更正为 `run_task`（+ `_aexecute`）并优先评估 B1'。

---

## 5. 测试方案（v2）

### 5.1 单元/集成测试（新增）

| 测试文件 | 覆盖 | 用例 |
|----------|------|------|
| `test_subagent_inactivity_watchdog.py` | C1 | 子代理 astream mock 连续 >300s 无新 chunk（卡在 LLM），断言子代理自身在 ~300s `astream_task.cancel()`，状态 TIMED_OUT；另测有 chunk 持续产出时不误触发 |
| `test_subagent_forced_cancel.py` | B1/B1' | 子代理卡在 LLM（mock），外部 timeout 触发后经 `run_task` 超时分支跨线程 cancel，astream 协程收 CancelledError，状态 TIMED_OUT |
| `test_run_watchdog_graceful_degradation.py` | A1+C1 | 3 子代理 mock：2 正常完成、1 卡死；断言卡死者被 C1 就地终止、另 2 结果回传、lead 继续综合，run **未**被整体 kill |
| `test_heartbeat_callback_ignores_llm_retry.py` | A2 | mock LLM 重试（多次 on_llm_start/on_llm_end），断言 activity_event 首次 ping 后不被重试重置（仅 on_llm_new_token / on_llm_start 首次重置） |
| `test_long_subagent_not_killed.py` | A1 回归 | 合法长子代理（持续产出新消息但总时长 >600s）不被 lead run watchdog 误杀 |

### 5.2 回归测试

```bash
cd backend && make test   # 重点：test_run_worker_rollback.py、test_subagent_*、既有 watchdog 相关用例
```

需确认：正常子代理完成路径、长但合法的工具调用（bash/OCR 数分钟）、多轮持续产出的长子代理均不受影响。

### 5.3 集成验证（重跑 d5fe20ec 场景）

用相同输入（试验方案 + 篮选期病历 + 筛选期检查）重跑，人为模拟 ai-gateway 慢响应，观测：
1. **就地终止**：卡死子代理由 C1 在 ~300s 终止（对比本次 1920s），状态 TIMED_OUT。
2. **优雅降级**：非全部卡死时，正常子代理完成并被 lead 综合，run 不被整体 kill。
3. **强制中断**：外部 timeout 路径下 astream 协程确实被 cancel（B1/B1'）。
4. **无正常误杀**：正常完成子代理、长 OCR 工具调用、持续产出的长子代理均不被误终止。

---

## 6. 风险评估（v2）

| 风险 | 影响 | 缓解 |
|------|------|------|
| C1 阈值（300s）对慢但合法的子代理（长 LLM 思考、大 OCR）偏紧 | 合法子代理被误杀 | 阈值可配置 + 按子代理类型差异化；`on_llm_new_token`/chunk 产出即重置 C1；OCR 子代理放宽 |
| B1 跨线程 cancel 编排出错（run_task 线程 vs astream loop 线程） | cancel 不生效或崩溃 | 优先评估 B1'（同 loop `wait_for`，无跨线程）；B1 必须用 `call_soon_threadsafe`；充分单测 |
| A2 收紧误杀 reasoning 模型长思考 | 正常 LLM 调用被误终止 | 保留 on_llm_start 首次 ping + on_llm_new_token |
| B1 astream Task 重构破坏正常完成路径 | 子代理正常执行失败 | 充分回归 test_subagent_*；保留 async for 语义，仅外包 Task + cancel 引用 |
| B3 max_retries 下调导致瞬时网络抖动失败 | 子代理偶发失败 | 2 次重试仍覆盖大多数抖动；ai-gateway 稳定性是根因 |
| C2 run_events 改 sqlite 增加写开销 | run 性能略降 | sqlite 已是 database.backend 选项，put_batch 批写 |

---

## 7. 涉及文件清单（v2）

### 7.1 代码改动

| 文件 | 任务 | 类型 |
|------|------|------|
| `backend/packages/harness/deerflow/subagents/executor.py` | C1（子代理 inactivity watchdog）、B1/B1'（`_aexecute` 持有 astream task + `run_task`/`_execute_in_isolated_loop` 超时分支强制 cancel）、B2（单步超时） | 后端核心 |
| `backend/packages/harness/deerflow/runtime/runs/worker.py` | A2（heartbeat 收紧：on_llm_start 去重、移除 on_llm_end/on_chain_start ping） | 后端 |
| `backend/packages/harness/deerflow/tools/builtins/task_tool.py` | A1（确认保留 `task_running` 真实进度透传，清理隐式周期写；**不**移除进度推送） | 后端 |
| `config.yaml`（gitignored） | B3、C2（max_retries 5→2；run_events.backend memory→sqlite） | config |
| `config.example.yaml` | B3、C2 注释同步 | config |

### 7.2 测试新增

| 文件 | 任务 |
|------|------|
| `backend/tests/test_subagent_inactivity_watchdog.py` | C1 |
| `backend/tests/test_subagent_forced_cancel.py` | B1/B1' |
| `backend/tests/test_run_watchdog_graceful_degradation.py` | A1+C1 |
| `backend/tests/test_heartbeat_callback_ignores_llm_retry.py` | A2 |
| `backend/tests/test_long_subagent_not_killed.py` | A1 回归 |

### 7.3 文档同步

- 实施后新增 `docs/subagent-timeout-watchdog-optimization-changelog.md` 记录逐文件变更与测试结果。
- 更新 `backend/AGENTS.md` 中 inactivity watchdog 与 subagent executor 相关小节（新增子代理级 watchdog 说明）。
- 更新 `docs/eligibility-screener-monitoring-session-d5fe20ec.md` §4.2/§4.3 标注已实施。

---

## 8. 决策点（需评审确认）

1. **C1 vs 纯 lead 心跳收紧**：v2 主张 C1（子代理侧停滞判定）为主，因为纯收紧 lead 心跳无法既保活合法长子代理、又快杀卡死子代理。确认采纳 C1 为主机制。
2. **B1 vs B1'**：推荐优先 B1'（子代理 loop 内 `asyncio.wait_for` 包裹 astream 迭代 + `aclose()` 清理，无跨线程编排）；B1（跨线程 `call_soon_threadsafe` cancel）作为兼容 future 超时模型的退路。
3. **C1 阈值**：默认 300s，是否按子代理类型差异化（OCR/研究类子代理放宽）？
4. **A2 heartbeat 收紧力度**：确认"保留 on_llm_start 首次 ping + 去重重试、移除 on_llm_end/on_chain_start"，兼顾 reasoning 模型长思考安全。
5. **C2 run_events 落盘范围**：全量落盘还是仅 subagent 事件落盘？

> 建议默认推进批次1（A2 + B3 + C1）止血，批次2（B1/B1' + B2 + A1）补齐强制中断与透传语义，批次3（C2）按观测需求决定。
