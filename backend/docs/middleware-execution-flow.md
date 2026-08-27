# Middleware 执行流程

## Middleware 列表

`create_deerflow_agent` 通过 `RuntimeFeatures` 组装的完整 middleware 链（默认全开时）：

> **权威顺序在 [`backend/AGENTS.md`](../AGENTS.md) 的 Middleware Chain 一节**（共 29 项，含每一项的
> 触发条件与设计理由）。下表只补充「各钩子由谁实现 + 主/子 agent 归属」这一层信息，编号与该节对齐。

| # | Middleware | `before_agent` | `before_model` | `after_model` | `after_agent` | `wrap_model_call` | `wrap_tool_call` | 主 Agent | Subagent | 来源 |
|---|-----------|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|------|
| 1 | InputSanitizationMiddleware | | | | | ✓ | | ✓ | ✓ | 始终开启 |
| 2 | ToolOutputBudgetMiddleware | | | | | | ✓ | ✓ | ✓ | `tool_output` |
| 3 | ThreadDataMiddleware | ✓ | | | | | | ✓ | ✓ | `sandbox` |
| 4 | UploadsMiddleware | ✓ | | | | | | ✓ | ✗ | `sandbox` |
| 5 | SandboxMiddleware | ✓ | | | ✓ | | | ✓ | ✓ | `sandbox` |
| 6 | DanglingToolCallMiddleware | | | | | ✓ | | ✓ | ✓ | 始终开启 |
| 7 | LLMErrorHandlingMiddleware | | | | | ✓ | | ✓ | ✓ | 始终开启 |
| 8 | GuardrailMiddleware | | | | | | ✓ | ✓ | ✓ | `guardrails` |
| 9 | SandboxAuditMiddleware | | | | | | ✓ | ✓ | ✓ | 始终开启 |
| 10 | ReadBeforeWriteMiddleware | | | | | | ✓ | ✓ | ✓ | `read_before_write` |
| 10a | ReadFilePolicyMiddleware | | | | | | ✓ | ✓ | ✓ | `read_file_policy` |
| 10b | BashWritePolicyMiddleware | | | | | | ✓ | ✓ | ✓ | `bash_write_policy` |
| 11 | ReadFileDedupMiddleware | | | | | | ✓ | ✓ | ✓ | `read_file_dedup` |
| 12 | ToolErrorHandlingMiddleware | | | | | | ✓ | ✓ | ✓ | 始终开启 |
| 16 | SummarizationMiddleware | | ✓ | | | ✓ | | ✓ | ⚙ | `summarization` |
| 17 | TodoMiddleware | | ✓ | ✓ | | ✓ | | ✓ | ✗ | `plan_mode` 参数 |
| 19 | TitleMiddleware | | | ✓ | | | | ✓ | ✗ | `auto_title` |
| 20 | MemoryMiddleware | | | | ✓ | | | ✓ | ✗ | `memory` |
| 21 | ViewImageMiddleware | | ✓ | | | | | ✓ | ⚙ | `vision` |
| 22 | DeferredToolFilterMiddleware | | ✓ | | | | | ✓ | ⚙ | `tool_search` |
| 24 | SubagentLimitMiddleware | | | ✓ | | | | ✓ | ✗ | `subagent` |
| 25 | LoopDetectionMiddleware | ✓ | | ✓ | ✓ | ✓ | | ✓ | ⚙ | 始终开启 |
| 26 | TokenBudgetMiddleware | | ✓ | | | | | ✓ | ⚙ | `token_budget` |
| 29 | ClarificationMiddleware | | | | | | ✓ | ✓ | ✗ | 始终最后 |

**Subagent 列的 ⚙**：子代理**按 `subagents.*` 配置**接入，且**默认关闭**。子代理运行时长期只有共享基座
（第 1–12 项）而没有 Summarization / LoopDetection / TokenBudget —— 一个判定 task 因此在 83 步内跑到
6.36M token，没有任何一方压缩它的上下文、限它的预算、或打断它的门禁脚本循环（治理见
`docs/eligibility-screener-gate-loop-optimization-changelog.md` Phase 1）。开启顺序有纪律：
`token_budget → loop_detection → summarization → read_file_dedup`，**一次只开一个**。

**10a / 10b 的顺序不是随意的**：两条策略排在 `ReadFileDedupMiddleware` **之前**，因为被拦下的调用
不该到达 sandbox，dedup 的账本也只应记录真实发生过的读。三者都只实现 `wrap_tool_call`，
**不新增图节点**，所以 `recursion_limit / 真实回合` 倍率（实测 4.03–4.05，见 `config.yaml`
的 `max_turns` 口径注释）不受影响 —— 这一点由
`tests/test_read_file_policy_middleware.py::TestNoGraphNode` 与
`tests/test_bash_write_policy_middleware.py::TestNoGraphNode` 钉住。

**Summarization 的 `wrap_model_call`（2026-08-12 新增）**：压缩把摘要写进
`state["summary_text"]` 而不是插回 messages，而该通道的渲染者 `DurableContextMiddleware`
**只挂在主 agent**。于是子代理的压缩长期是**净删除** —— 消息删掉、摘要写进没人读的通道。
现在 `wrap_model_call` 在 `is_subagent` 时把摘要作为隐藏 `<task_progress_summary>` 块注入
当次模型调用（配置 `subagents.summarization.inject_summary_message`，默认开）。
故障与验证见 `docs/eligibility-screener-subagent-context-and-artifact-gate-changelog.md`。

**压缩是一次交换，右手为空就不能做（2026-08-13）**：`_maybe_summarize` 的返回值会删掉所有
未保留的消息并把 `summary_text` 放到它们的位置。`_summarize_with` 返回
`response.text.strip()`，摘要模型只回空白时得到 `""` —— 假值但**不是 `None`**，而旧守卫只挡
`None`，交换于是照做：消息删了、`summary_text` 被覆写成空、子代理注入端因通道为空而跳过。
同一类净删除，另一条路径。现由 `_summary_is_usable` 阻断（无开关：空摘要换走历史是数据丢失，
不是可选行为）。⚠️ `config.yaml` 里的 `min_summary_chars` 等 7 个键**没有对应字段**，pydantic
直接忽略，不要当作守卫已配置。

**dedup 的引用必须指向还拿得到的正文（2026-08-13）**：`ReadFileDedupMiddleware`（第 11 项）
返回的引用让模型「翻回上一次读取」，但 Summarization（第 16 项）可能已经把那条 `ToolMessage`
删掉了 —— 两个中间件各自正确，组合起来给出一条悬空指针，而 `read_file` 是模型取回该内容的
唯一手段。现在命中后先按 `tool_call_id` 查 transcript：查得到（或首读已外部化到磁盘）才给
引用，查得到消息集合但首读不在其中就放行正文；**无 `state` / 无 `messages` 时保持原行为**
（那是「未知」，不能读成「已丢」）。会话 `7512ebd2`：判定子代理被这样挡掉
`judgment-schema.md` 与 `schema_example.json`，自创输出 schema 与文件名，整单被产物闸作废。

表中省略的编号（13–15、18、23、27–28）是主 agent 专属且不参与 `wrap_tool_call`/`wrap_model_call`
的上下文类中间件，说明见 `AGENTS.md`。

## 执行流程

LangChain `create_agent` 的规则：
- **`before_*` 正序执行**（列表位置 0 → N）
- **`after_*` 反序执行**（列表位置 N → 0）

```mermaid
graph TB
    START(["invoke"]) --> TD

    subgraph BA ["<b>before_agent</b> 正序 0→N"]
        direction TB
        TD["[0] ThreadData<br/>创建线程目录"] --> UL["[1] Uploads<br/>扫描上传文件"] --> SB["[2] Sandbox<br/>获取沙箱"] --> LD_BA["[12] LoopDetection<br/>清理 stale warning"]
    end

    subgraph BM ["<b>before_model</b> 正序 0→N"]
        direction TB
        VI["[10] ViewImage<br/>注入图片 base64"]
    end

    subgraph WM ["<b>wrap_model_call</b>"]
        direction TB
        DTC_WM["[3] DanglingToolCall<br/>补悬空 ToolMessage"] --> LD_WM["[12] LoopDetection<br/>注入当前 run warning"]
    end

    LD_BA --> VI
    VI --> DTC_WM
    LD_WM --> M["<b>MODEL</b>"]

    subgraph AM ["<b>after_model</b> 反序 N→0"]
        direction TB
        LD["[12] LoopDetection<br/>检测循环/排队 warning"] --> SL["[11] SubagentLimit<br/>截断多余 task"] --> TI["[8] Title<br/>生成标题"]
    end

    M --> LD

    subgraph AA ["<b>after_agent</b> 反序 N→0"]
        direction TB
        LD_CLEAN["[12] LoopDetection<br/>清理 pending warning"] --> MEM["[9] Memory<br/>入队记忆"] --> SBR["[2] Sandbox<br/>释放沙箱"]
    end

    TI --> LD_CLEAN
    SBR --> END(["response"])

    classDef beforeNode fill:#a0a8b5,stroke:#636b7a,color:#2d3239
    classDef modelNode fill:#b5a8a0,stroke:#7a6b63,color:#2d3239
    classDef wrapModelNode fill:#a8a0b5,stroke:#6b637a,color:#2d3239
    classDef afterModelNode fill:#b5a0a8,stroke:#7a636b,color:#2d3239
    classDef afterAgentNode fill:#a0b5a8,stroke:#637a6b,color:#2d3239
    classDef terminalNode fill:#a8b5a0,stroke:#6b7a63,color:#2d3239

    class TD,UL,SB,LD_BA,VI beforeNode
    class DTC_WM,LD_WM wrapModelNode
    class M modelNode
    class LD,SL,TI afterModelNode
    class LD_CLEAN,SBR,MEM afterAgentNode
    class START,END terminalNode
```

## 时序图

```mermaid
sequenceDiagram
    participant U as User
    participant TD as ThreadDataMiddleware
    participant UL as UploadsMiddleware
    participant SB as SandboxMiddleware
    participant LD as LoopDetectionMiddleware
    participant VI as ViewImageMiddleware
    participant DTC as DanglingToolCallMiddleware
    participant M as MODEL
    participant SL as SubagentLimitMiddleware
    participant TI as TitleMiddleware
    participant MEM as MemoryMiddleware

    U ->> TD: invoke
    activate TD
    Note right of TD: before_agent 创建目录

    TD ->> UL: before_agent
    activate UL
    Note right of UL: before_agent 扫描上传文件

    UL ->> SB: before_agent
    activate SB
    Note right of SB: before_agent 获取沙箱

    SB ->> LD: before_agent
    activate LD
    Note right of LD: before_agent 清理同 thread 旧 run 的 pending warning
    LD ->> VI: before_model
    activate VI
    Note right of VI: before_model 注入图片 base64

    VI ->> DTC: wrap_model_call
    activate DTC
    Note right of DTC: wrap_model_call 补悬空 ToolMessage
    DTC ->> LD: wrap_model_call
    Note right of LD: wrap_model_call drain 当前 run warning 并追加到末尾
    LD ->> M: messages + tools
    activate M
    M -->> LD: AI response
    deactivate M

    Note right of LD: after_model 检测循环；warning 入队，hard-stop 清 tool_calls
    LD -->> SL: after_model
    deactivate LD

    activate SL
    Note right of SL: after_model 截断多余 task
    SL -->> TI: after_model
    deactivate SL

    activate TI
    Note right of TI: after_model 生成标题
    TI -->> DTC: done
    deactivate TI

    deactivate DTC

    VI -->> SB: done
    deactivate VI

    Note right of LD: after_agent 清理当前 run 未消费 warning

    Note right of MEM: after_agent 入队记忆

    Note right of SB: after_agent 释放沙箱
    SB -->> UL: done
    deactivate SB

    UL -->> TD: done
    deactivate UL

    TD -->> U: response
    deactivate TD
```

## 洋葱模型

列表位置决定在洋葱中的层级 — 位置 0 最外层，位置 N 最内层：

```
进入 before_*：   [0] → [1] → [2] → ... → [10] → MODEL
退出 after_*：    MODEL → [13] → [11] → ... → [6] → [3] → [2] → [0]
                          ↑ 最内层最先执行
```

> [!important] 核心规则
> 列表最后的 middleware，其 `after_model` **最先执行**。
> ClarificationMiddleware 在列表末尾，所以它第一个拦截 model 输出。

## 对比：真正的洋葱 vs DeerFlow 的实际情况

### 真正的洋葱（如 Koa/Express）

每个 middleware 同时负责 before 和 after，形成对称嵌套：

```mermaid
sequenceDiagram
    participant U as User
    participant A as AuthMiddleware
    participant L as LogMiddleware
    participant R as RateLimitMiddleware
    participant H as Handler

    U ->> A: request
    activate A
    Note right of A: before: 校验 token

    A ->> L: next()
    activate L
    Note right of L: before: 记录请求时间

    L ->> R: next()
    activate R
    Note right of R: before: 检查频率

    R ->> H: next()
    activate H
    H -->> R: result
    deactivate H

    Note right of R: after: 更新计数器
    R -->> L: result
    deactivate R

    Note right of L: after: 记录耗时
    L -->> A: result
    deactivate L

    Note right of A: after: 清理上下文
    A -->> U: response
    deactivate A
```

> [!tip] 洋葱特征
> 每个 middleware 都有 before/after 对称操作，`activate` 跨越整个内层执行，形成完美嵌套。

### DeerFlow 的实际情况

不是洋葱，是管道。大部分 middleware 只用一个钩子，不存在对称嵌套。多轮对话时 before_model / after_model 循环执行：

```mermaid
sequenceDiagram
    participant U as User
    participant TD as ThreadData
    participant UL as Uploads
    participant SB as Sandbox
    participant LD as LoopDetection
    participant VI as ViewImage
    participant DTC as DanglingToolCall
    participant M as MODEL
    participant SL as SubagentLimit
    participant TI as Title
    participant MEM as Memory

    U ->> TD: invoke
    Note right of TD: before_agent 创建目录
    TD ->> UL: .
    Note right of UL: before_agent 扫描文件
    UL ->> SB: .
    Note right of SB: before_agent 获取沙箱
    SB ->> LD: .
    Note right of LD: before_agent 清理 stale pending warning

    loop 每轮对话（tool call 循环）
        SB ->> VI: .
        Note right of VI: before_model 注入图片
        VI ->> DTC: .
        Note right of DTC: wrap_model_call 补悬空工具结果
        DTC ->> LD: .
        Note right of LD: wrap_model_call 注入当前 run warning
        LD ->> M: messages + tools
        M -->> LD: AI response
        Note right of LD: after_model 检测循环/排队 warning
        LD -->> SL: .
        Note right of SL: after_model 截断多余 task
        SL -->> TI: .
        Note right of TI: after_model 生成标题
    end

    Note right of LD: after_agent 清理当前 run pending warning
    LD -->> MEM: .
    Note right of MEM: after_agent 入队记忆
    MEM -->> SB: .
    Note right of SB: after_agent 释放沙箱
    SB -->> U: response
```

> [!warning] 不是洋葱
> 大部分 middleware 只用一个阶段。SandboxMiddleware 使用 `before_agent`/`after_agent` 做资源获取/释放；LoopDetectionMiddleware 也使用这两个钩子，但用途是清理 run-scoped pending warnings，不是资源生命周期对称。`before_agent` / `after_agent` 只跑一次，`before_model` / `after_model` / `wrap_model_call` 每轮循环都跑。

硬依赖只有 2 处：

1. **ThreadData 在 Sandbox 之前** — sandbox 需要线程目录
2. **Clarification 在列表最后** — `wrap_tool_call` 处理 `ask_clarification` 时优先拦截，并通过 `Command(goto=END)` 中断执行

### 结论

| | 真正的洋葱 | DeerFlow 实际 |
|---|---|---|
| 每个 middleware | before + after 对称 | 大多只用一个钩子 |
| 激活条 | 嵌套（外长内短） | 不嵌套（串行） |
| 反序的意义 | 清理与初始化配对 | 影响 `after_model` / `after_agent` 的执行优先级 |
| 典型例子 | Auth: 校验 token / 清理上下文 | ThreadData: 只创建目录，没有清理 |

## 关键设计点

### ClarificationMiddleware 为什么在列表最后？

位置最后使它在工具调用包装链中优先拦截 `ask_clarification`。如果命中，它返回 `Command(goto=END)`，把格式化后的澄清问题写成 `ToolMessage` 并中断执行。

### SandboxMiddleware 的对称性

`before_agent`（正序第 3 个）获取沙箱，`after_agent`（反序第 1 个）释放沙箱。外层进入 → 外层退出，天然的洋葱对称。

### LoopDetectionMiddleware 为什么同时用多个钩子？

`after_model` 只做检测：重复工具调用达到 warning 阈值时，把 warning 放入 `(thread_id, run_id)` 作用域的 pending 队列。真正注入发生在下一次 `wrap_model_call`：此时上一轮 `AIMessage(tool_calls)` 对应的 `ToolMessage` 已经在请求里，warning 追加在末尾，不会破坏 OpenAI/Moonshot 的 tool-call pairing。`before_agent` 清理同一 thread 下旧 run 的残留 warning，`after_agent` 清理当前 run 没被消费的 warning。
