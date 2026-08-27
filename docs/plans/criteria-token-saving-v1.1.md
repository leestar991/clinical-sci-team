# Eligibility-Screener Token and Latency Optimization Implementation Plan (v1.1)

> **版本说明**:本文件是 [`criteria-token-saving.md`](./criteria-token-saving.md)(v1.0)的修订版,**不替换** v1.0。v1.0 的基线、质量闸、TDD 纪律仍然有效;v1.1 修正了若干处对当前代码现状的误判,补全了隐藏成本,并调整了实施顺序。
>
> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 eligibility-screener 单次完整筛选从 34.4M token、约 62 分钟,分阶段降低至不高于 8M token、25 分钟,同时保持 OCR、标准解析、逐条判定和 QC 质量闸不退化。

**Architecture:** 保留 LLM 负责标准语义拆分、临床判定和语义 QC;将阶段编排、模式状态、OCR 批处理、文件搬运、结构闸、批量修订、合并和报告构建改为结构化状态或确定性工具。先消除子代理五技能全文继承和状态丢失,再接入子代理本地预算与版本感知读取缓存,最后将通用 Agent 循环收敛为专用结构化调用和运行时任务队列。

**Tech Stack:** Python 3.12、LangGraph/LangChain middleware、Pydantic、FastAPI Gateway、PostgreSQL RunEventStore/checkpointer、pytest、ruff、DeerFlow sandbox tools、TextIn `parse_document`。

## v1.1 相对 v1.0 的主要修订

| # | 修订点 | 依据(file:line) |
|---|---|---|
| R1 | **Task 1**:usage 采集层已存在(`SubagentTokenCollector`、`SubagentResult.token_usage_records`、RunJournal 回填),`analyze_run` 可直接从 RunJournal 聚合,不必改 `subagent.end` 事件 schema | `subagents/token_collector.py:16`、`executor.py:94`、`task_tool.py:171` |
| R2 | **Task 2**:`SubagentConfig.skills` 字段**已存在**,无需新增 schema;Task 2 主要是配置启用 + 防重复读取 + 测试。并显式说明 `skills=[]` 会顺带解除 tool_policy 并集过滤、使 `parse_document` 自动回归(修复 OCR 根因) | `subagents/config.py:37`、`config/subagents_config.py:130-142`、`skills/tool_policy.py:34-35` |
| R3 | **Task 3**:空 summary 数据丢失 bug(`not summary.strip()` 缺失)应作为**可独立先行**的修复项,不绑定 Task 3 整体进度 | `summarization_middleware.py:271-280` |
| R4 | **Task 4**:明确 `build_subagent_runtime_middlewares` 不含 TokenBudgetMiddleware、`subagents.token_budget` 无 schema 字段会被 `extra=ignore` 静默丢弃、subagent `checkpointer=False` 致 hard stop 无状态恢复 | `tool_error_handling_middleware.py:216-265`、`config/subagents_config.py:71-91`、`executor.py:381` |
| R5 | **Task 5**:`read_file_dedup` 是纯纸面(无 schema、无代码、committed 模板无),需从零新建并补进 `config.example.yaml` + AppConfig schema;同时订正 `eligibility-screener-fix-changelog.md:432-435` 的不实"已实现"声明;前置收紧 `str_replace` 多处出现拒绝 | `config/app_config.py:121,179`、`sandbox/tools.py:1943-1994`、`docs/eligibility-screener-fix-changelog.md:432-435` |
| R6 | **Task 6(重点重新设计)**:`parse_document` 是**整文档单次 POST**,非每页一次;"逐页 28 次"浪费来自 SOUL 拆图编排,非工具限制。`batch.py` 必要性存疑,降级为可选;首选改 SOUL 用整文档解析 + 复用 artifacts.py 已有的 sha256 缓存 | `textin/client.py:67-69`、`textin/artifacts.py:35-36`、`textin/tools.py:98-101` |
| R7 | **Task 8(重点)**:`workflows/` 目录不存在需新建;**无运行时 feature flag**(`RuntimeFeatures` 是构造期 dataclass,`make_lead_agent` 不走它),回滚机制需从零建;lead 是 `create_agent` 标准 ReAct 无 typed graph 钩子,需外层路由分发且不得污染通用路径;subagent `checkpointer=False` 致 typed workflow 可恢复性需自行解决;task 并发是"turn 内 3 + barrier"非滑动窗口,`TaskQueue` 需新建 | `agents/features.py:17-39`、`lead_agent/agent.py:418-558`、`executor.py:381`、`subagent_limit_middleware.py:52-143` |
| R8 | **实施顺序调整**:新增"阶段 0 独立先行修复"(空 summary 守卫);Task 6 改为"先验证再决定是否建 batch";Task 8 明确为单独立项评审 | — |

## Global Constraints

- 保持 IN/EX 双轨、患者维度和逐文档独立判定,不以 token 优化为由合并临床语义边界。
- 保留标准结构闸、标准语义 QC、`uncertain_recheck`、排除方向检查和报告 `--verify`。
- `patient_mode`、当前阶段、QC 状态和产物路径必须存储为 typed state;不得仅依赖自然语言 summary。
- 同一处理模式只允许询问一次;已有持久化选择时禁止再次 `ask_clarification`。
- 子代理预算必须在子代理内部实时执行;不能只在任务结束后回填给 lead。
- 读取缓存必须按内容版本失效;禁止对已修改文件返回旧内容。
- OCR 外部服务并发保持 2–3;同一页面最多调用一次 `parse_document`,失败页除外。
- 所有优化必须先写失败测试,再做最小实现;后端变更遵循 `backend/AGENTS.md`。
- 每阶段用同一脱敏 fixture 做 A/B replay;质量闸失败时不得用强制放行掩盖回归。
- 不新增第三方依赖;如执行时确需新增,必须先单独评审并固定精确版本。
- > [v1.1] **不污染通用 agent 路径**:typed workflow(Task 8)必须作为 eligibility 单 agent 的 opt-in 路由,不得改变 `make_lead_agent` 通用路径对其他 agent 的行为;其他 agent 继续走 `create_agent`。
- > [v1.1] **subagent 无 checkpointer**(`executor.py:381` `checkpointer=False`):任何依赖子代理状态恢复的设计必须显式接 checkpointer 或改为无状态 typed 终态返回(如 `budget_exceeded`)。
- > [v1.1] **文档与代码一致性**:任何"已实现"声明必须有对应运行时代码;发现失真 changelog 必须在同期订正(见 Task 5 对 `eligibility-screener-fix-changelog.md:432-435` 的订正)。
- > [v1.1] **现状验证先行**:涉及工具行为假设的 Task,实施前必须先用最小脱敏用例验证工具实际行为(如 `parse_document` 对整 PDF 的返回),不得基于文档或 changelog 假设设计。

---

## 1. 已验证基线

来源会话:`4d1f95b4-14ae-4303-99a5-aa2306205741`。(与 v1.0 一致,此处保留。)

| 指标 | 基线 |
|---|---:|
| 三个 run 总 token | 34,407,156 |
| 活跃执行时间 | 3,639.685 秒(60.7 分钟) |
| 主 run token | 32,207,112 |
| 主 run 子代理 token | 27,790,091(86.3%) |
| 主 run lead token | 4,368,081 |
| 主 run middleware token | 48,940 |
| 子代理任务 | 29 |
| 子代理 step | 965 = 379 AI + 586 tool |
| 子代理 `read_file` | 360 |
| 外部化 read_file 文件 | 147;仅 62 个唯一哈希 |
| 完全重复外部化字节 | 2,479,270(63.6%) |
| 五个技能全文 | 42,758 o200k token |
| 五技能 × 379 AI 轮次 | 约 16,205,282 固定重复 token |

三段执行链:

1. `2c6b4668…`:Phase 1,8.65 分钟、1.61M token;重复定位方案章节并询问模式。
2. `837d06fa…`:主流程,50.82 分钟、32.21M token;29 个子代理完成双轨解析、OCR、QC、判定和修订。
3. `84c9f85e…`:因模式状态丢失,用户重复回答"2";额外 588,753 token、71 秒后才合并并生成报告。

## 2. 合理链路与必须删除的冗余

### 保留

- IN/EX 双轨隔离和并行。
- 一次性患者处理模式确认。
- 文档 OCR 及文本层页归集。
- 标准结构闸 + 标准语义 QC。
- 患者聚合后按患者×轨判定。
- `uncertain_recheck`、排除方向检查、最终结构闸。
- 确定性 `judge_pack.py`、`build_reports.py --verify`。

### 删除或重构

- 每个子代理继承五个技能全文,并再次 `read_file SKILL.md`。
- Phase 1 中 16 次读取方案、约 10 次 locator 调用、6 份全量定位副本。
- 通用子代理对 mutable JSON 反复 `read_file -> str_replace -> read_file`。
- > [v1.1] ~~OCR 每页经过 Agent `parse_document -> read_file -> write_file` 循环。~~ **修正为**:SOUL 把 PDF 拆成逐页图片后,对每张图各调一次 `parse_document`,而 `parse_document` 本身是整文档单次 POST。浪费来自"拆图 + 逐图调用"的 SOUL 编排,而非工具的每页行为。详见 Task 6。
- 判定后单独生成理由,再因判定变化反复重生理由。
- 将同轮多个 `task` 称为"滑动窗口";当前是 turn 内并发 3 + barrier(`SubagentLimitMiddleware` + `ThreadPoolExecutor max_workers=3`),等本 turn 全部 task 返回才进下一轮 model。
- 依赖自由文本 summary 保存模式和阶段状态。
- `read_file_dedup` / `search_dedup` 配置占位但没有运行时代码(> [v1.1] 且占位只在 gitignored `config.yaml`,committed `config.example.yaml` 与 AppConfig schema 均无)。

---

### Task 1: 建立可重复的会话基线与逐子代理用量证据

> [v1.1] **采集层已存在,改动比 v1.0 预想更小**。`SubagentTokenCollector`(`subagents/token_collector.py:16`)已是 `BaseCallbackHandler`,通过 `on_llm_end` 收集 usage;`SubagentResult.token_usage_records`(`executor.py:94`)已定义并被各终态路径填充;`task_tool._report_subagent_usage`(`task_tool.py:156-174`)已把明细回填到父 RunJournal(`record_external_llm_usage_records`)。因此 `analyze_run` **可直接从 RunJournal 聚合**,无需改 `subagent.end` 事件 schema。仅当需要按 run-event 时序对齐时才扩展事件。

**Files:**
- Create: `backend/scripts/analyze_eligibility_run.py`
- Create: `backend/tests/test_analyze_eligibility_run.py`
- Modify(可选,仅事件时序对齐路径): `backend/packages/harness/deerflow/subagents/step_events.py`
- Modify(可选,同上): `backend/packages/harness/deerflow/runtime/runs/worker.py`
- Test: `backend/tests/test_subagent_step_events.py`

**Interfaces:**
- Produces: `analyze_run(thread_id: str) -> RunOptimizationReport`,包含 run、task、AI step、tool、重复读取、token 和阶段时序。
- > [v1.1] 推荐 path A:`analyze_run` 直接读 RunEventStore + RunJournal 的 `external_llm_usage_records` 聚合 per-task usage,不改事件 schema。
- > [v1.1] 可选 path B(仅当需要 run-event 级 usage 时):给 `subagent.end.event_metadata.usage = {input_tokens, output_tokens, total_tokens}`(当前 `step_events.py:204-225` 的 content 不含 usage)。

- [ ] **Step 1: 写失败测试**:构造含两个 `subagent.end` 的事件(或两条 RunJournal usage 记录),断言报告按 task_id 输出独立 usage,且不会把 965 个 message step 误报为 LLM 调用。
- [ ] **Step 2: 验证失败**

```bash
cd backend && PYTHONPATH=. uv run pytest tests/test_analyze_eligibility_run.py tests/test_subagent_step_events.py -v
```

Expected: FAIL,缺少 `analyze_run`。

- [ ] **Step 3: 实现最小分析器**:只读 RunStore/RunEventStore + RunJournal;按 `task_id` 聚合 start/end/step;usage 取 RunJournal `external_llm_usage_records` 汇总(优先 path A)。若选 path B,则 `subagent.end.metadata.usage` 直接取 `SubagentResult.token_usage_records` 汇总值。
- [ ] **Step 4: 验证通过并生成基线 JSON**

```bash
cd backend && PYTHONPATH=. uv run pytest tests/test_analyze_eligibility_run.py tests/test_subagent_step_events.py -v
PYTHONPATH=. uv run python scripts/analyze_eligibility_run.py 4d1f95b4-14ae-4303-99a5-aa2306205741 --output ../.deer-flow/criteria-token-baseline.json
```

- [ ] **Step 5: 提交**

```bash
git add backend/scripts/analyze_eligibility_run.py backend/tests/test_analyze_eligibility_run.py backend/tests/test_subagent_step_events.py
# 若走 path B,追加: backend/packages/harness/deerflow/subagents/step_events.py backend/packages/harness/deerflow/runtime/runs/worker.py
git commit -m "feat: report per-subagent eligibility usage"
```

### Task 2: 收窄子代理技能与工具上下文

> [v1.1] **基础设施已具备,无需新增 schema 字段**。`SubagentConfig.skills: list[str] | None`(`subagents/config.py:37`)已存在;`SubagentsAppConfig.get_skills_for()`(`config/subagents_config.py:130-142`)已能从 `config.yaml` 的 `subagents.agents.<name>.skills` 读 override;registry 已应用(`subagents/registry.py:108-111`)。语义:`None`=加载全部已启用 skill;`[]`=不加载任何 skill;`list`=白名单(`executor.py:384-410`)。
>
> [v1.1] **正面副作用(显式说明)**:把 `general-purpose` 设为 `skills=[]` 后,不加载任何带 `allowed-tools` 声明的 skill,`allowed_tool_names_for_skills` 返回 `None`(`tool_policy.py:34-35`),`filter_tools_by_skill_allowed_tools` 不过滤 -> `parse_document` **自动回归**子代理工具集。这顺带修复 MEMORY 记录的 eligibility OCR 根因(4 skill 白名单并集过滤掉 parse_document)。须加验证点确认。
>
> [v1.1] **`_merge_skill_allowlists` 语义提醒**(`task_tool.py:186-194`):当 lead 在 metadata 写了 `available_skills`(非 None)且子代理 `config.skills is None` 时,子代理继承的是 **lead 的 allowlist 副本**而非"全部 skill"。eligibility 场景下 lead 应**不限定** `available_skills`(保持 None),让子代理的 `config.skills` 直接生效。否则 `[]` 会被 lead allowlist 覆盖。

**Files:**
- Modify: `config.yaml`
- Modify: `config.example.yaml`
- Modify(仅配置加载/防重复读取规则,非新字段): `backend/packages/harness/deerflow/config/subagents_config.py`
- Modify: `backend/packages/harness/deerflow/subagents/registry.py`
- Modify: `backend/packages/harness/deerflow/tools/builtins/task_tool.py`
- Test: `backend/tests/test_subagent_registry.py`
- Test: `backend/tests/test_subagent_executor.py`
- Test(> [v1.1] 新增): `backend/tests/test_subagent_tool_policy_skills_empty.py`

**Interfaces:**
- Consumes(已存在): `SubagentConfig.skills: list[str] | None`。
- Produces: 每类子代理明确的技能白名单;`[]` 表示不加载技能,不能退回继承全部技能。

- [ ] **Step 1: 写失败测试**:覆盖 `general-purpose.skills=[]`、`quality-control.skills=["criteria-parser"]`、child `None` 不得在 eligibility 专用配置中隐式扩展为五技能。
- [ ] > [v1.1] **Step 1b: 写 tool_policy 回归测试**:`general-purpose.skills=[]` 时,断言 `filter_tools_by_skill_allowed_tools` 返回 `None`(不过滤),且 `parse_document` 保留在子代理工具集中。
- [ ] **Step 2: 验证失败**

```bash
cd backend && PYTHONPATH=. uv run pytest tests/test_subagent_registry.py tests/test_subagent_executor.py tests/test_subagent_tool_policy_skills_empty.py -k "skills or allowlist or empty" -v
```

- [ ] **Step 3: 配置最小白名单**

```yaml
subagents:
  agents:
    general-purpose:
      skills: []
    quality-control:
      skills: []
    report-writer:
      skills: []
```

任务 prompt 必须只传对应契约路径;不再要求子代理"加载整个技能"。需要规则正文时,只允许一个专用技能,禁止五技能继承。> [v1.1] 同时确认 lead agent 不限定 `available_skills`(保持 None),否则会经 `_merge_skill_allowlists` 覆盖子代理 `[]`。

- [ ] **Step 4: 增加防重复读取规则**:如果技能已经出现在 `_load_skill_messages()`(`executor.py:415-444`),任务 prompt 不得要求再次读取同一 `SKILL.md`;在 task 审计事件记录 `loaded_skill_names`。
- [ ] **Step 5: 验证**

```bash
cd backend && PYTHONPATH=. uv run pytest tests/test_subagent_registry.py tests/test_subagent_executor.py tests/test_subagent_tool_policy_skills_empty.py -v
make lint
```

验收:子代理初始固定技能上下文从 42,758 token 降至每类 0–1 个技能;`/mnt/skills/**/SKILL.md` 每 task 读取不超过 1 次;> [v1.1] `general-purpose` 子代理工具集含 `parse_document`。

- [ ] **Step 6: 提交**

```bash
git add config.example.yaml backend/packages/harness/deerflow/config/subagents_config.py backend/packages/harness/deerflow/subagents/registry.py backend/packages/harness/deerflow/tools/builtins/task_tool.py backend/tests/test_subagent_registry.py backend/tests/test_subagent_executor.py backend/tests/test_subagent_tool_policy_skills_empty.py
git commit -m "perf: scope skills for eligibility subagents"
```

### Task 3: 将模式、阶段和质量闸保存为 typed durable state

> [v1.1] **空 summary 数据丢失 bug 应独立先行**(见"阶段 0")。本 Task 的 Step 2/5 修复 `summarization_middleware.py:271-280` 缺 `not summary.strip()` 守卫的问题:当前若摘要模型返回 `""`,`summary is None` 为 False,不跳过,直接返回 `summary_text: ""` + `RemoveMessage(REMOVE_ALL_MESSAGES)`,双向丢失旧 summary 与消息。本 Task 启用 summarization 前必须先落此修复。
>
> [v1.1] **现状**:`ThreadState`(`thread_state.py:223-235`)无 `eligibility_workflow`/`patient_mode`/`current_phase`/`phase_status`/QC 字段;仅有 `summary_text`、`delegations`、`skill_context`、`goal` 等。`durable_context_middleware` 持久化的是 `delegations` + `skill_context`(`_capture` :145-163),`summary_text` 只读不写。新 typed state 需自带 reducer 才会被 checkpoint 持久化。
>
> [v1.1] **checkpoint 兼容**:新增字段必须 `NotRequired`/带默认值,旧 thread 反序列化不能崩。

**Files:**
- Modify: `backend/packages/harness/deerflow/agents/thread_state.py`
- Modify: `backend/packages/harness/deerflow/agents/middlewares/durable_context_middleware.py`
- Modify: `backend/packages/harness/deerflow/agents/middlewares/summarization_middleware.py`
- Modify: `backend/.deer-flow/agents/eligibility-screener/SOUL.md`(运行态,gitignored)
- Create: `backend/tests/test_eligibility_workflow_state.py`
- Modify: `backend/tests/test_summarization_middleware.py`

**Interfaces:**
- Produces: `eligibility_workflow` state,至少包含 `patient_mode`、`current_phase`、`phase_status`、`criteria_qc`、`judgment_qc`、`artifacts`。
- Produces: `should_request_patient_mode(state) -> bool`,已有模式时恒为 `False`。

- [ ] **Step 1: 写失败测试**:用户首次选择模式2后经过多次 summary,断言 `patient_mode == "single_paged"` 且不会再次调用 clarification。
- [ ] **Step 2: 写空摘要回归测试**:summary model 返回 `""` 或仅 reasoning 时,断言旧 `summary_text` 和原消息不被清除。
- [ ] **Step 3: 运行测试确认失败**

```bash
cd backend && PYTHONPATH=. uv run pytest tests/test_eligibility_workflow_state.py tests/test_summarization_middleware.py -v
```

- [ ] **Step 4: 实现 typed reducer 与 durable 投影**:在 `ThreadState` 新增 `eligibility_workflow` channel + reducer(参照 `delegations`/`skill_context` 模式);不将原始 OCR/判定正文写入 state,仅保存枚举状态、计数和路径。
- [ ] **Step 5: 修复 summary 判定**:`not summary.strip()` 与低信息输出都视为失败;失败时返回 `None` 跳过压缩、保留旧状态。summary 输入去掉完整 task prompt,只保留 delegation description/status/result。
- [ ] **Step 6: 更新 SOUL**:Phase 1.5 入口先检查 typed state;已有 `patient_mode` 禁止再次询问。
- [ ] **Step 7: 验证与提交**

```bash
cd backend && PYTHONPATH=. uv run pytest tests/test_eligibility_workflow_state.py tests/test_summarization_middleware.py -v
make lint
git add backend/packages/harness/deerflow/agents/thread_state.py backend/packages/harness/deerflow/agents/middlewares/durable_context_middleware.py backend/packages/harness/deerflow/agents/middlewares/summarization_middleware.py backend/tests/test_eligibility_workflow_state.py backend/tests/test_summarization_middleware.py
git commit -m "fix: persist eligibility workflow state"
```

### Task 4: 为子代理接入实时、按类型预算

> [v1.1] **现状(完全不存在运行时执行)**:`build_subagent_runtime_middlewares`(`tool_error_handling_middleware.py:216-265`)组装的子代理中间件链**不含** `TokenBudgetMiddleware`;`TokenBudgetMiddleware` 只在 lead 接入(`lead_agent/agent.py:371-376`)。`config.yaml` 的 `subagents.token_budget`(`config.yaml:225-230`)被注释,且 `SubagentsAppConfig`(`config/subagents_config.py:71-91`)无 `token_budget` 字段,pydantic v2 默认 `extra=ignore` 会静默丢弃。需新建 schema 字段 + 在 `build_subagent_runtime_middlewares` 接入(参考 lead 接法)。
>
> [v1.1] **可复用**:`SubagentTokenCollector`(`token_collector.py:16`)已采集 usage,预算直接消费它,不依赖 lead 的 `TokenUsageMiddleware` 或事后回填。
>
> [v1.1] **subagent 无 checkpointer**(`executor.py:381` `checkpointer=False`):hard stop 后无法靠 checkpoint 恢复,`budget_exceeded` 必须作为 typed 终态返回(在 `try_set_terminal` 终态路径写入,参照 cancel/completed/failed 的 `executor.py:623/655/742`)。

**Files:**
- Modify: `backend/packages/harness/deerflow/config/subagents_config.py`(> [v1.1] 新增 `SubagentOverrideConfig.token_budget` + `SubagentsAppConfig.token_budget` 字段)
- Modify: `backend/packages/harness/deerflow/subagents/config.py`
- Modify: `backend/packages/harness/deerflow/subagents/registry.py`
- Modify: `backend/packages/harness/deerflow/subagents/executor.py`
- Modify: `backend/packages/harness/deerflow/agents/middlewares/tool_error_handling_middleware.py`(> [v1.1] 在 `build_subagent_runtime_middlewares` 接入 `TokenBudgetMiddleware`)
- Create: `backend/tests/test_subagent_token_budget.py`

**Interfaces:**
- Produces: `SubagentTokenBudgetConfig(enabled, max_input_tokens, max_output_tokens, max_tokens, warn_threshold, hard_stop_threshold)`。
- Produces: 每个子任务独立 `run_id/task_id` 预算;达到 hard stop 后取消该子代理并返回 typed `budget_exceeded`。

- [ ] **Step 1: 写失败测试**:两个并发子代理各自计数;A 超预算只能停止 A,B 继续;collector usage 必须在下一次模型调用前触发预算。
- [ ] **Step 2: 验证失败**

```bash
cd backend && PYTHONPATH=. uv run pytest tests/test_subagent_token_budget.py -v
```

- [ ] **Step 3: 接入子代理 middleware**:在 `SubagentsAppConfig`/`SubagentOverrideConfig` 新增 `token_budget` 字段;在 `build_subagent_runtime_middlewares` 按 config 装入 `TokenBudgetMiddleware`(参考 `lead_agent/agent.py:371-376`);预算直接消费 `SubagentTokenCollector`。
- [ ] **Step 4: 先以 warn-only 配置上线 replay**:不得直接启用旧注释中的统一 150k;先记录各类型 p50/p95/max。
- [ ] **Step 5: 校准后配置按类型预算**:OCR、parse、judge、QC、repair 分别设阈值;hard stop 至少高于健康样本 p95 的 1.25 倍。
- [ ] **Step 6: 验证与提交**

```bash
cd backend && PYTHONPATH=. uv run pytest tests/test_subagent_token_budget.py tests/test_subagent_executor.py -v
make lint
git add backend/packages/harness/deerflow/config/subagents_config.py backend/packages/harness/deerflow/subagents/config.py backend/packages/harness/deerflow/subagents/registry.py backend/packages/harness/deerflow/subagents/executor.py backend/packages/harness/deerflow/agents/middlewares/tool_error_handling_middleware.py backend/tests/test_subagent_token_budget.py
git commit -m "feat: enforce per-subagent token budgets"
```

### Task 5: 实现版本感知 read_file 去重和批量 Patch

> [v1.1] **read_file_dedup 是纯纸面,需从零新建且补全三处**:① `config/read_dedup_config.py`(不存在);② `read_file_dedup_middleware.py`(不存在);③ `config.example.yaml` 无该 section(目前只在 gitignored `config.yaml:477-485` 靠 `AppConfig.extra="allow"`(`app_config.py:121,179`)偷渡,新部署不会生成)。必须在 `config.example.yaml` 与 AppConfig schema 显式声明。
>
> [v1.1] **订正失真 changelog**:`docs/eligibility-screener-fix-changelog.md:432-435` 声称"已新增模块级 `_read_dedup_cache`"且"`_read_dedup_is_enabled()` 从 config 读取",但代码里完全不存在。本 Task 必须同期订正该声明(删除或改为"计划中"),避免后续误判。
>
> [v1.1] **前置:收紧 `str_replace` 多处出现拒绝**:`sandbox/tools.py:1943-1994` 的 `str_replace_tool` 在 `replace_all=False` 时用 `content.replace(old_str, new_str, 1)`,**只替换第一处且不报错**(与 Claude Code Edit 的"多处出现报错"行为不同)。在引入 `apply_json_patches` 前,先收紧单次 replace 的歧义防护(多处出现时拒绝),否则批量 patch 建立在歧义语义上。
>
> [v1.1] **可参考/复用**:`ReadBeforeWriteMiddleware`(`agents/middlewares/read_before_write_middleware.py` + `config/read_before_write_config.py`)已有写前 hash 比对;`textin/artifacts.py:35-36` 已有 `sha256(bytes)[:12]` 内容寻址缓存。Task 5 的 `content_hash` 应与这两处保持一致策略。
>
> [v1.1] **对象存储语义约束**:`artifacts.py:3-7` 注释明确 gateway 部署无本地 `/mnt/user-data`,所有读写必须走 `Sandbox` API(禁止 `open()`/`Path.write_text`)。`apply_json_patches` 的加锁 + 版本校验 + 原子写必须在此约束下设计(`get_file_operation_lock` 已可用,见 `tools.py:1909`)。

**Files:**
- Create: `backend/packages/harness/deerflow/agents/middlewares/read_file_dedup_middleware.py`
- Create: `backend/packages/harness/deerflow/config/read_dedup_config.py`
- Modify: `backend/packages/harness/deerflow/config/app_config.py`(> [v1.1] 显式声明 schema,不再靠 `extra="allow"`)
- Modify: `backend/packages/harness/deerflow/config/__init__.py`
- Modify: `config.example.yaml`(> [v1.1] 补 `read_file_dedup`/`search_dedup` section)
- Modify: `backend/packages/harness/deerflow/agents/middlewares/tool_error_handling_middleware.py`
- Modify: `backend/packages/harness/deerflow/sandbox/tools.py`(> [v1.1] 先收紧 `str_replace`,再加 `apply_json_patches`)
- Modify(> [v1.1] 订正): `docs/eligibility-screener-fix-changelog.md`
- Create: `backend/tests/test_read_file_dedup_middleware.py`
- Create: `backend/tests/test_batch_json_patch_tool.py`
- Create(> [v1.1] 新增): `backend/tests/test_str_replace_ambiguity.py`

**Interfaces:**
- Produces: cache key `(sandbox_id, thread_id, path, start_line, end_line, content_hash)`。
- Produces: `apply_json_patches(path: str, expected_hash: str, patches: list[JsonPatch]) -> PatchResult`,一次加锁、一次版本校验、一次写入。

- [ ] > [v1.1] **Step 0: 订正 changelog + 补 config schema/example**:删除 `eligibility-screener-fix-changelog.md:432-435` 关于 `_read_dedup_cache` 的不实"已实现"声明(改为"计划中,见 criteria-token-saving-v1.1 Task 5");在 `config.example.yaml` 补 `read_file_dedup`/`search_dedup` section;在 `AppConfig` 显式声明字段。
- [ ] > [v1.1] **Step 0b: 收紧 str_replace**:写失败测试——`replace_all=False` 且 `old_str` 出现多次时拒绝;实现后验证。
- [ ] **Step 1: 写缓存失败测试**:同版本同范围二次读返回短引用;文件修改后必须 cache miss;不同线程/沙箱不得共享 mutable 文件缓存。
- [ ] **Step 2: 写批量 Patch 失败测试**:expected hash 不符拒绝;全部 patch 原子成功或全部不写;成功后 read-before-write 标记失效。
- [ ] **Step 3: 运行测试确认失败**

```bash
cd backend && PYTHONPATH=. uv run pytest tests/test_str_replace_ambiguity.py tests/test_read_file_dedup_middleware.py tests/test_batch_json_patch_tool.py -v
```

- [ ] **Step 4: 实现 middleware 和工具**:SKILL/reference 可按内容 hash 复用;judgment/criteria JSON 只能在版本相同期间复用。`content_hash` 与 `artifacts.py:35-36` 的 sha256 策略一致。
- [ ] **Step 5: 工具参数预检**:`grep` 收到文件路径时返回明确建议,避免 file-as-directory 重试;非法绝对路径在模型下一轮前提供规范路径。
- [ ] **Step 6: 验证与提交**

```bash
cd backend && PYTHONPATH=. uv run pytest tests/test_str_replace_ambiguity.py tests/test_read_file_dedup_middleware.py tests/test_batch_json_patch_tool.py tests/test_sandbox_tools_security.py -v
make lint
git add backend/packages/harness/deerflow/agents/middlewares/read_file_dedup_middleware.py backend/packages/harness/deerflow/config/read_dedup_config.py backend/packages/harness/deerflow/config/app_config.py backend/packages/harness/deerflow/config/__init__.py config.example.yaml backend/packages/harness/deerflow/agents/middlewares/tool_error_handling_middleware.py backend/packages/harness/deerflow/sandbox/tools.py docs/eligibility-screener-fix-changelog.md backend/tests/test_str_replace_ambiguity.py backend/tests/test_read_file_dedup_middleware.py backend/tests/test_batch_json_patch_tool.py
git commit -m "perf: deduplicate versioned file reads"
```

### Task 6: 将 Phase 1 和 OCR 搬运改为确定性批处理(> [v1.1] 重新设计)

> [v1.1] **核心修正:`parse_document` 是整文档单次 POST,非每页一次**。`textin/client.py:67-69` 对整个文件 bytes 一次性 `POST /ai/service/v1/pdf_to_markdown`,TextIn 自己嗅 filetype;模块 docstring(`client.py:1-22`)明确**没有** `image_to_markdown` 机器人(返回 code=40007),所有 per-page-image OCR 都会失败,一切走 `pdf_to_markdown` 整文档解析。`textin/tools.py:56-117` 的 `parse_document_tool` 下载整个文件一次 -> 调一次 `parse_via_textin` -> 命中缓存直接返回 index(`tools.py:98-101`)。
>
> [v1.1] **结论**:v1.0 所述"28 扫描页调用 28 次 provider"的浪费,来自 **SOUL 把 PDF 拆成 28 张图后对每张图各调一次 `parse_document`** 的编排,而非工具的每页行为。`batch.py` / `parse_documents_batch` 的必要性存疑。
>
> [v1.1] **已有缓存可复用**:`textin/artifacts.py:35-36` 已是内容寻址(`cache_key = sha256(bytes)[:12]`),布局 `/mnt/user-data/workspace/parsed/<key>/{document.md, tables/NNN.html, index.json}`(`artifacts.py:31,39-41`)。重复解析同一文件本就命中缓存跳过。
>
> [v1.1] **新设计:先验证再决定**。Step 0 用脱敏 PDF 验证 `parse_document` 对整 PDF 的返回行为(是否返回所有页 markdown + 表格 + 可计算覆盖率)。若整 PDF 可行,首选改 SOUL 直接对每份原始 PDF 调一次 `parse_document`(试验方案/病历/检查 3 份 = 至多 3 次),复用 sha256 缓存,删除"拆图 -> 逐图 parse -> 逐图 read/write"循环。`batch.py` 降级为可选兜底:仅在整 PDF 解析对某些文档(如扫描型纯图 PDF)不适用,或需要显式并发控制多份独立文档时才建,且 3 份 PDF 的并发可直接靠 LangGraph turn 内并发(`SubagentLimitMiddleware` max=3)实现,不强制新接口。

**Files:**
- Modify: `skills/custom/criteria-parser/scripts/locate_criteria_sections.py`(运行态 custom skill)
- Modify(> [v1.1] 可选,仅当 Step 0 验证需要): `backend/packages/community/cellflow_community/textin/tools.py`
- Create(> [v1.1] 可选,仅当需并发多文档兜底): `backend/packages/community/cellflow_community/textin/batch.py`
- Create: `backend/tests/test_textin_whole_document_parse.py`(> [v1.1] 替代 v1.0 的 `test_textin_batch_parse.py`)
- Modify: `backend/.deer-flow/agents/eligibility-screener/SOUL.md`(运行态)

**Interfaces:**
- Produces: locator 一次输出 `eligibility_criteria_raw.md`、`criteria_meta.json` 和机器可读诊断;不生成 protocol 全量副本。定位对象从"逐页"改为"整文档解析后的 markdown"。
- > [v1.1] 可选 Produces(仅当建 batch): `parse_documents_batch(paths, output_dir, concurrency=3) -> BatchParseResult`。

- [ ] > [v1.1] **Step 0(验证先行)**:用一份脱敏 PDF 调 `parse_document`,确认返回包含所有页 markdown + 表格 + 可计算覆盖率。记录:页数、表格数、是否漏页。若整 PDF 解析覆盖完整,跳过 `batch.py`;若发现扫描型纯图 PDF 整文档解析漏页,才进入 batch/兜底路径。
- [ ] **Step 1: 为 locator 写失败测试**:覆盖全角编号、标题无空格、NFKC 和当前方案 fixture;断言一次定位成功且无 `试验方案_locate*` 副本。locator 输入改为整文档 markdown。
- [ ] > [v1.1] **Step 2: 为整文档解析写失败测试**:3 份 PDF 各调 `parse_document` 至多 1 次;命中 sha256 缓存的文件 0 次外部调用;扫描页覆盖率 100%(以解析后页数为准,不以拆图数为准)。仅当 Step 0 判定需 batch 时,改为 `parse_documents_batch` 测试且 concurrency≤3。
- [ ] **Step 3: 实现最小规范化和整文档路径**:服务端完成 parse artifact 读取、目标 Markdown 写入、表格复制和 coverage 返回;只有 Step 0 判定漏页的异常页进入视觉兜底。
- [ ] **Step 4: 更新 SOUL**:删除 LLM 调试 locator、拆图逐页 read/write 和多副本路径;改为"每份原始 PDF 调一次 `parse_document` + locator 一次定位"两个确定性步骤。
- [ ] **Step 5: 验证与提交**

```bash
cd backend && PYTHONPATH=. uv run pytest tests/test_textin_whole_document_parse.py -v
make lint
# 若建了 batch.py:
# git add backend/packages/community/cellflow_community/textin/tools.py backend/packages/community/cellflow_community/textin/batch.py
git add backend/tests/test_textin_whole_document_parse.py
git commit -m "perf: whole-document eligibility parsing"
```

Custom skill 与运行态 SOUL 不纳入普通 git commit;执行时必须另做读回校验并同步到其受控发布源。

### Task 7: 收敛判定、QC、修订与理由链

> [v1.1] 基本保留 v1.0。依赖 Task 5 的 `apply_json_patches`(故需 Task 5 的 str_replace 收紧先行)。`skills/custom/eligibility-judgment` 是运行态 custom skill(gitignored),不纳入普通 git commit,执行时另做读回校验。

**Files:**
- Modify: `skills/custom/eligibility-judgment/SKILL.md`(运行态 custom skill)
- Modify: `skills/custom/eligibility-judgment/references/judge-delegation.md`
- Modify: `skills/custom/eligibility-judgment/references/qc-delegation.md`
- Modify: `skills/custom/eligibility-judgment/scripts/judge_pack.py`
- Create: `backend/tests/test_eligibility_structured_judgment.py`

**Interfaces:**
- Produces: 单次 judgment 结构同时包含 `conclusion`、`evidence`、`reason`、`document`、`page`。
- Produces: QC 返回 `findings` + `patches`;由 Task 5 的 `apply_json_patches` 原子应用。
- 保留: `uncertain_recheck.py`、`exclusion_direction_check.py`、结构闸。

- [ ] **Step 1: 写失败测试**:判定结果缺 reason/evidence/page 时 schema 失败;QC patch 后 conclusion 与 reason 必须方向一致。
- [ ] **Step 2: 写兼容测试**:旧的 `reasons_*.json` 仍可由 `judge_pack.py` 读取,但新流程不再要求独立 report-writer。
- [ ] **Step 3: 运行失败测试**

```bash
cd backend && PYTHONPATH=. uv run pytest tests/test_eligibility_structured_judgment.py -v
```

- [ ] **Step 4: 修改技能契约**:每 patient×track 使用小批结构化调用;不得整轨超长单调用;每批 5–10 条标准,确定性合并。
- [ ] **Step 5: 删除正常路径中的独立 reasons 任务**:仅兼容旧产物;判定修订 patch 同时改 conclusion/reason。
- [ ] **Step 6: 验证质量**:与基线最终 JSON 逐条比对,结论一致率、证据页完整率、方向冲突均达验收线。

### Task 8: 用 typed DAG 和运行时队列替代 lead 的批次轮询(> [v1.1] 重点修订)

> [v1.1] **现状(全部需从零建,隐藏成本最高)**:
> - `workflows/` 目录**不存在**(`backend/packages/harness/deerflow/` 下无此目录),需新建包。
> - **无运行时 feature flag**:`RuntimeFeatures`(`agents/features.py:17-39`)是**构造期** dataclass,只在 `create_deerflow_agent`(`factory.py:61-147`)的 `_assemble_from_features` 里用,且 `make_lead_agent`(`agent.py:418-558`)**根本不走它**。v1.0 所说"通过 feature flag 切回通用 lead graph"**无现成开关可复用**,需从零建运行时 FF 机制(配置项 + 查询点 + 分发分支)。
> - **lead 是 `create_agent` 标准 ReAct**(`agent.py:510,537` 返回 `CompiledStateGraph`,但图结构是标准 model->tools->model 循环),**无钩子**插 typed graph,需在 `make_lead_agent` 之外新建并行 `StateGraph` 工厂并在外层路由分发。
> - **task 并发是"turn 内 3 + barrier",非滑动窗口**:`SubagentLimitMiddleware`(`subagent_limit_middleware.py:52-143`)+ `ThreadPoolExecutor(max_workers=3)`(`executor.py:145`),turn 内 3 个 task 并发,等本 turn 全部返回才进下一轮。要"任一完成立即补位"需改 `task_tool` 轮询模型(`task_tool.py:378-524`)+ 引入 `TaskQueue`/`asyncio.gather`,目前**无此原语**。
> - **subagent `checkpointer=False`**(`executor.py:381`):typed workflow 若要可恢复/可中断,需自行接 checkpointer 或改 executor。
>
> [v1.1] **架构影响判断**:本 Task 本质是给 DeerFlow 引入"特定 agent 可路由到 typed graph"的能力,不只是 eligibility 优化。**应单独立项评审**分发点设计 + FF 机制,确保通用 `make_lead_agent` 路径不被污染(其他 agent 继续走 `create_agent`,影响为零)。P0 replay 达标且质量不退化后才开始。
>
> [v1.1] **回滚前提**:FF 需先实现且默认指向通用 lead graph,typed graph 作为 opt-in。即"FF 切回"的前提是 FF 本身已建好——这是 Task 8 的一部分,不是既有能力。

**Files:**
- Create: `backend/packages/harness/deerflow/workflows/eligibility/state.py`
- Create: `backend/packages/harness/deerflow/workflows/eligibility/graph.py`
- Create: `backend/packages/harness/deerflow/workflows/eligibility/task_queue.py`
- Create(> [v1.1] 运行时 FF 机制): `backend/packages/harness/deerflow/config/feature_flags.py`
- Modify: `backend/packages/harness/deerflow/agents/lead_agent/agent.py`(> [v1.1] 仅加分发分支,不改通用路径行为)
- Create: `backend/tests/test_eligibility_workflow_graph.py`
- Create: `backend/tests/test_eligibility_task_queue.py`
- Create(> [v1.1]): `backend/tests/test_feature_flags.py`
- Modify: `README.md`
- Modify: `backend/AGENTS.md`

**Interfaces:**
- Produces: Phase DAG `P1 -> P1.5 -> P2(IN,EX,OCR) -> P2.5 -> P3 -> P4 -> P4.5 -> P5`。
- Produces: `TaskQueue(max_concurrency=3)`;任一任务完成立即从 ready queue 补位(> [v1.1] 需新建,当前是 barrier)。
- LLM 节点仅限 criteria semantic parse、judgment、semantic QC;管理节点不调用模型。

- [ ] > [v1.1] **Step 0: 单独立项评审分发点 + FF 设计**:评审 `make_lead_agent` 分发分支的设计——eligibility-screener 路由到 typed graph,其他 agent 不变;FF 机制默认指向通用路径。评审通过后再实施。
- [ ] **Step 1: 写 DAG 属性测试**:未选择模式不得进 P2;QC 未通过不得 slim/assemble;两轨未合并不得报告;clarification exactly once。
- [ ] **Step 2: 写真实滑动窗口测试**:三个任务耗时不同,第一个完成后第四个立即启动,不等待最慢任务(> [v1.1] 验证 `TaskQueue` 补位,区别于现有 barrier)。
- [ ] > [v1.1] **Step 2b: 写 FF 测试**:默认指向通用 lead graph;切换后路由到 typed graph;切换不影响其他 agent。
- [ ] **Step 3: 运行测试确认失败**

```bash
cd backend && PYTHONPATH=. uv run pytest tests/test_eligibility_workflow_graph.py tests/test_eligibility_task_queue.py tests/test_feature_flags.py -v
```

- [ ] **Step 4: 实现最小 DAG 与队列**:保留现有通用 lead 路径作为 FF 默认回滚;eligibility-screener 单独路由到 typed graph。> [v1.1] 分发分支不得改变通用路径对其他 agent 的行为。
- [ ] > [v1.1] **Step 4b: 实现运行时 FF**:配置项 + 查询点 + 在 `make_lead_agent` 的分发分支;默认 `eligibility_typed_workflow=false`(opt-in)。
- [ ] **Step 5: 将 copy/present/todo/summary 写入改成节点 side effect**:gate 成功后 exactly once,不再消耗独立 LLM 轮次。
- [ ] **Step 6: 更新文档和验证**

```bash
cd backend && PYTHONPATH=. uv run pytest tests/test_eligibility_workflow_graph.py tests/test_eligibility_task_queue.py tests/test_feature_flags.py -v
make lint
cd .. && pytest tests/skills/test_soul_skill_contract.py -v
```

- [ ] **Step 7: 提交**

```bash
git add backend/packages/harness/deerflow/workflows/eligibility backend/packages/harness/deerflow/config/feature_flags.py backend/packages/harness/deerflow/agents/lead_agent/agent.py backend/tests/test_eligibility_workflow_graph.py backend/tests/test_eligibility_task_queue.py backend/tests/test_feature_flags.py README.md backend/AGENTS.md
git commit -m "feat: add typed eligibility workflow"
```

---

## 3. 分阶段验收

### P0 验收:Tasks 1–5

| 指标 | 基线 | 目标 |
|---|---:|---:|
| 总 token | 34.4M | ≤14M |
| 子代理 token | 27.8M | ≤11M |
| 子代理 AI 轮次 | 379 | ≤180 |
| `read_file` | 360 | ≤150 |
| 重复模式询问 | 1 | 0 |
| 端到端时长 | 约62分钟 | ≤35–40分钟 |

### P1 验收:Tasks 6–8

| 指标 | 目标 |
|---|---:|
| 总 token | ≤8M |
| 子代理 token | ≤5M |
| 子代理 AI 轮次 | ≤80 |
| Lead LLM 调用 | ≤15 |
| 端到端时长 | ≤20–25分钟 |
| > [v1.1] TextIn provider 调用 | 每份 PDF 至多 1 次(命中缓存为 0);无重复解析;扫描页覆盖率 100% |

### 质量验收(每阶段都必须满足)

- 标准总条数、四分类条数和条件 ID 守恒。
- > [v1.1] OCR 覆盖率以整文档解析后的实际页数为准(不以拆图数为分母),文本层页不重复解析,表格数不减少。
- 每条 judgment 有 document、page、evidence、conclusion、reason。
- `suspected_missed == []`。
- 排除方向 `conflicts == []`。
- QC 阻断项清零或按既有 HITL 规则暂停,不自动降级放行。
- `build_reports.py --verify` 通过。
- 与当前高质量基线逐条结论一致率 ≥95%;差异项必须人工复核。

## 4. 完整验证命令

```bash
cd backend
PYTHONPATH=. uv run pytest \
  tests/test_analyze_eligibility_run.py \
  tests/test_subagent_registry.py \
  tests/test_subagent_executor.py \
  tests/test_subagent_tool_policy_skills_empty.py \
  tests/test_eligibility_workflow_state.py \
  tests/test_summarization_middleware.py \
  tests/test_subagent_token_budget.py \
  tests/test_str_replace_ambiguity.py \
  tests/test_read_file_dedup_middleware.py \
  tests/test_batch_json_patch_tool.py \
  tests/test_textin_whole_document_parse.py \
  tests/test_eligibility_structured_judgment.py \
  tests/test_eligibility_workflow_graph.py \
  tests/test_eligibility_task_queue.py \
  tests/test_feature_flags.py -v
make lint
make test

cd ..
pytest tests/skills/test_soul_skill_contract.py -v
```

A/B replay 后重新运行(先把新会话 ID 写入环境变量):

```bash
export NEW_THREAD_ID="实际重跑产生的线程 UUID"
cd backend
PYTHONPATH=. uv run python scripts/analyze_eligibility_run.py "$NEW_THREAD_ID" \
  --baseline ../.deer-flow/criteria-token-baseline.json \
  --output ../.deer-flow/criteria-token-after.json
```

报告必须同时给出 token、wall time、task/AI/tool/read 次数和全部质量闸;只降低 token 而质量失败不得判定优化完成。

## 5. 回滚顺序

> [v1.1] 回滚前提:Task 8 的 FF 切换依赖 FF 机制本身已实现(v1.0 误以为既有能力)。FF 需先建好且默认指向通用路径,typed graph 作为 opt-in,回滚 = 关闭 FF。

1. > [v1.1] 关闭 `eligibility_typed_workflow` FF,切回通用 lead graph(前提:FF 已实现)。
2. 子代理预算先从 hard-stop 改为 warn-only,不删除 usage 观测。
3. 版本缓存整体关闭;不得只关闭失效校验。
4. Structured judgment 保留旧 reasons 兼容读取路径。
5. > [v1.1] OCR 回退:若 Task 6 采用了整文档解析,回退到现有逐图调用,但保持每份文件幂等键(复用 `artifacts.py:35-36` 的 sha256 缓存),禁止双路线同时运行。

## 6. 实施顺序

> [v1.1] 调整为分阶段,新增"阶段 0 独立先行",Task 6 改为"先验证再决定",Task 8 明确单独立项。

### 阶段 0:独立先行修复(不依赖整个方案,可立即合入)

- **空 summary 守卫修复**(从 Task 3 Step 2/5 抽出):`summarization_middleware.py:271-280` 补 `not summary.strip()`,返回 `None` 跳过压缩、保留旧状态。这是当前就存在的真实 bug,只要有人启用 summarization 就触发,不应绑定 Task 3 整体进度。
- **订正 changelog**:`eligibility-screener-fix-changelog.md:432-435` 关于 `_read_dedup_cache` 的不实声明。

### 阶段 1:测量 + 低风险高收益(占基线大头)

- **Task 1**(测量):优先 path A,从 RunJournal 聚合,不改事件 schema。
- **Task 2**(技能收窄):基础设施已具备,主要是配置启用 + 测试。收益 ≈ 16M 固定重复 token(五技能 × 379 轮)。顺带修复 OCR 根因(parse_document 回归)。
- **Task 3**(typed state):解决模式丢失(基线 84c9f85e:588K token + 71s)。若阶段 0 已修空 summary 守卫,本 Task Step 2/5 可省。

### 阶段 2:OCR 路径(先验证再决定)

- **Task 6 Step 0 先行验证**:用脱敏 PDF 确认 `parse_document` 整文档解析的完整性。
  - 若整 PDF 可行:改 SOUL 用整文档解析 + 复用 sha256 缓存,不建 `batch.py`。
  - 若扫描型纯图 PDF 漏页:才建 `batch.py` 作为兜底。

### 阶段 3:预算 + 去重 + 判定收敛

- **Task 4**(子代理预算):warn-only 上线校准后再设 hard stop。
- **Task 5**(read_file 去重 + 批量 patch):含 str_replace 收紧 + changelog 订正 + config schema/example 补全。
- **Task 7**(判定收敛):依赖 Task 5 的 `apply_json_patches`。

### 阶段 4:架构级(单独立项评审,P0 达标后)

- **Task 8**(typed DAG + 运行时队列 + FF):本质是给 DeerFlow 引入"特定 agent 可路由到 typed graph"的能力。先评审分发点 + FF 设计,确保通用路径不被污染;P0 replay 达标且质量不退化后实施。

---

**与 v1.0 的关系**:v1.0 的基线数据、Global Constraints 主体、TDD 纪律、质量闸标准仍然有效。v1.1 不否定 v1.0 的方向,仅修正现状判断偏差(R1/R2/R4/R5/R6)、补全隐藏成本(R5/R7)、调整实施顺序(阶段 0/2/4)。实施时以 v1.1 为准;v1.0 保留作为历史参照。
