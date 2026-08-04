# Eligibility-Screener Token and Latency Optimization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 eligibility-screener 单次完整筛选从 34.4M token、约 62 分钟，分阶段降低至不高于 8M token、25 分钟，同时保持 OCR、标准解析、逐条判定和 QC 质量闸不退化。

**Architecture:** 保留 LLM 负责标准语义拆分、临床判定和语义 QC；将阶段编排、模式状态、OCR 批处理、文件搬运、结构闸、批量修订、合并和报告构建改为结构化状态或确定性工具。先消除子代理五技能全文继承和状态丢失，再接入子代理本地预算与版本感知读取缓存，最后将通用 Agent 循环收敛为专用结构化调用和运行时任务队列。

**Tech Stack:** Python 3.12、LangGraph/LangChain middleware、Pydantic、FastAPI Gateway、PostgreSQL RunEventStore/checkpointer、pytest、ruff、DeerFlow sandbox tools、TextIn `parse_document`。

## Global Constraints

- 保持 IN/EX 双轨、患者维度和逐文档独立判定，不以 token 优化为由合并临床语义边界。
- 保留标准结构闸、标准语义 QC、`uncertain_recheck`、排除方向检查和报告 `--verify`。
- `patient_mode`、当前阶段、QC 状态和产物路径必须存储为 typed state；不得仅依赖自然语言 summary。
- 同一处理模式只允许询问一次；已有持久化选择时禁止再次 `ask_clarification`。
- 子代理预算必须在子代理内部实时执行；不能只在任务结束后回填给 lead。
- 读取缓存必须按内容版本失效；禁止对已修改文件返回旧内容。
- OCR 外部服务并发保持 2–3；同一页面最多调用一次 `parse_document`，失败页除外。
- 所有优化必须先写失败测试，再做最小实现；后端变更遵循 `backend/AGENTS.md`。
- 每阶段用同一脱敏 fixture 做 A/B replay；质量闸失败时不得用强制放行掩盖回归。
- 不新增第三方依赖；如执行时确需新增，必须先单独评审并固定精确版本。

---

## 1. 已验证基线

来源会话：`4d1f95b4-14ae-4303-99a5-aa2306205741`。

| 指标 | 基线 |
|---|---:|
| 三个 run 总 token | 34,407,156 |
| 活跃执行时间 | 3,639.685 秒（60.7 分钟） |
| 主 run token | 32,207,112 |
| 主 run 子代理 token | 27,790,091（86.3%） |
| 主 run lead token | 4,368,081 |
| 主 run middleware token | 48,940 |
| 子代理任务 | 29 |
| 子代理 step | 965 = 379 AI + 586 tool |
| 子代理 `read_file` | 360 |
| 外部化 read_file 文件 | 147；仅 62 个唯一哈希 |
| 完全重复外部化字节 | 2,479,270（63.6%） |
| 五个技能全文 | 42,758 o200k token |
| 五技能 × 379 AI 轮次 | 约 16,205,282 固定重复 token |

三段执行链：

1. `2c6b4668…`：Phase 1，8.65 分钟、1.61M token；重复定位方案章节并询问模式。
2. `837d06fa…`：主流程，50.82 分钟、32.21M token；29 个子代理完成双轨解析、OCR、QC、判定和修订。
3. `84c9f85e…`：因模式状态丢失，用户重复回答“2”；额外 588,753 token、71 秒后才合并并生成报告。

## 2. 合理链路与必须删除的冗余

### 保留

- IN/EX 双轨隔离和并行。
- 一次性患者处理模式确认。
- 28 个扫描页的 OCR 及 11 个文本层页归集。
- 标准结构闸 + 标准语义 QC。
- 患者聚合后按患者×轨判定。
- `uncertain_recheck`、排除方向检查、最终结构闸。
- 确定性 `judge_pack.py`、`build_reports.py --verify`。

### 删除或重构

- 每个子代理继承五个技能全文，并再次 `read_file SKILL.md`。
- Phase 1 中 16 次读取方案、约 10 次 locator 调用、6 份全量定位副本。
- 通用子代理对 mutable JSON 反复 `read_file → str_replace → read_file`。
- OCR 每页经过 Agent `parse_document → read_file → write_file` 循环。
- 判定后单独生成理由，再因判定变化反复重生理由。
- 将同轮多个 `task` 称为“滑动窗口”；当前 ToolNode 实际等待整批完成。
- 依赖自由文本 summary 保存模式和阶段状态。
- `read_file_dedup` / `search_dedup` 配置占位但没有运行时代码。

---

### Task 1: 建立可重复的会话基线与逐子代理用量证据

**Files:**
- Create: `backend/scripts/analyze_eligibility_run.py`
- Create: `backend/tests/test_analyze_eligibility_run.py`
- Modify: `backend/packages/harness/deerflow/subagents/step_events.py`
- Modify: `backend/packages/harness/deerflow/runtime/runs/worker.py`
- Test: `backend/tests/test_subagent_step_events.py`

**Interfaces:**
- Produces: `analyze_run(thread_id: str) -> RunOptimizationReport`，包含 run、task、AI step、tool、重复读取、token 和阶段时序。
- Produces: `subagent.end.event_metadata.usage = {input_tokens, output_tokens, total_tokens}`。

- [ ] **Step 1: 写失败测试**：构造含两个 `subagent.end` 的事件，断言报告按 task_id 输出独立 usage，且不会把 965 个 message step 误报为 LLM 调用。
- [ ] **Step 2: 验证失败**

```bash
cd backend && PYTHONPATH=. uv run pytest tests/test_analyze_eligibility_run.py tests/test_subagent_step_events.py -v
```

Expected: FAIL，缺少 `analyze_run` 或 `subagent.end.metadata.usage`。

- [ ] **Step 3: 实现最小分析器与 usage 落盘**：只读 RunStore/RunEventStore；按 `task_id` 聚合 start/end/step；usage 直接取 `SubagentResult.token_usage_records` 汇总值。
- [ ] **Step 4: 验证通过并生成基线 JSON**

```bash
cd backend && PYTHONPATH=. uv run pytest tests/test_analyze_eligibility_run.py tests/test_subagent_step_events.py -v
PYTHONPATH=. uv run python scripts/analyze_eligibility_run.py 4d1f95b4-14ae-4303-99a5-aa2306205741 --output ../.deer-flow/criteria-token-baseline.json
```

- [ ] **Step 5: 提交**

```bash
git add backend/scripts/analyze_eligibility_run.py backend/tests/test_analyze_eligibility_run.py backend/tests/test_subagent_step_events.py backend/packages/harness/deerflow/subagents/step_events.py backend/packages/harness/deerflow/runtime/runs/worker.py
git commit -m "feat: report per-subagent eligibility usage"
```

### Task 2: 收窄子代理技能与工具上下文

**Files:**
- Modify: `config.yaml`
- Modify: `config.example.yaml`
- Modify: `backend/packages/harness/deerflow/config/subagents_config.py`
- Modify: `backend/packages/harness/deerflow/subagents/registry.py`
- Modify: `backend/packages/harness/deerflow/tools/builtins/task_tool.py`
- Test: `backend/tests/test_subagent_registry.py`
- Test: `backend/tests/test_subagent_executor.py`

**Interfaces:**
- Consumes: `SubagentOverrideConfig.skills: list[str] | None`。
- Produces: 每类子代理明确的技能白名单；`[]` 表示不加载技能，不能退回继承全部技能。

- [ ] **Step 1: 写失败测试**：覆盖 `general-purpose.skills=[]`、`quality-control.skills=["criteria-parser"]`、child `None` 不得在 eligibility 专用配置中隐式扩展为五技能。
- [ ] **Step 2: 验证失败**

```bash
cd backend && PYTHONPATH=. uv run pytest tests/test_subagent_registry.py tests/test_subagent_executor.py -k "skills or allowlist" -v
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

任务 prompt 必须只传对应契约路径；不再要求子代理“加载整个技能”。需要规则正文时，只允许一个专用技能，禁止五技能继承。

- [ ] **Step 4: 增加防重复读取规则**：如果技能已经出现在 `_load_skill_messages()`，任务 prompt 不得要求再次读取同一 `SKILL.md`；在 task 审计事件记录 `loaded_skill_names`。
- [ ] **Step 5: 验证**

```bash
cd backend && PYTHONPATH=. uv run pytest tests/test_subagent_registry.py tests/test_subagent_executor.py -v
make lint
```

验收：子代理初始固定技能上下文从 42,758 token 降至每类 0–1 个技能；`/mnt/skills/**/SKILL.md` 每 task 读取不超过 1 次。

- [ ] **Step 6: 提交**

```bash
git add config.example.yaml backend/packages/harness/deerflow/config/subagents_config.py backend/packages/harness/deerflow/subagents/registry.py backend/packages/harness/deerflow/tools/builtins/task_tool.py backend/tests/test_subagent_registry.py backend/tests/test_subagent_executor.py
git commit -m "perf: scope skills for eligibility subagents"
```

### Task 3: 将模式、阶段和质量闸保存为 typed durable state

**Files:**
- Modify: `backend/packages/harness/deerflow/agents/thread_state.py`
- Modify: `backend/packages/harness/deerflow/agents/middlewares/durable_context_middleware.py`
- Modify: `backend/packages/harness/deerflow/agents/middlewares/summarization_middleware.py`
- Modify: `backend/.deer-flow/agents/eligibility-screener/SOUL.md`（运行态，gitignored）
- Create: `backend/tests/test_eligibility_workflow_state.py`
- Modify: `backend/tests/test_summarization_middleware.py`

**Interfaces:**
- Produces: `eligibility_workflow` state，至少包含 `patient_mode`、`current_phase`、`phase_status`、`criteria_qc`、`judgment_qc`、`artifacts`。
- Produces: `should_request_patient_mode(state) -> bool`，已有模式时恒为 `False`。

- [ ] **Step 1: 写失败测试**：用户首次选择模式2后经过多次 summary，断言 `patient_mode == "single_paged"` 且不会再次调用 clarification。
- [ ] **Step 2: 写空摘要回归测试**：summary model 返回 `""` 或仅 reasoning 时，断言旧 `summary_text` 和原消息不被清除。
- [ ] **Step 3: 运行测试确认失败**

```bash
cd backend && PYTHONPATH=. uv run pytest tests/test_eligibility_workflow_state.py tests/test_summarization_middleware.py -v
```

- [ ] **Step 4: 实现 typed reducer 与 durable 投影**：不将原始 OCR/判定正文写入 state；仅保存枚举状态、计数和路径。
- [ ] **Step 5: 修复 summary 判定**：`not summary.strip()` 与低信息输出都视为失败；失败时保留旧状态。summary 输入去掉完整 task prompt，只保留 delegation description/status/result。
- [ ] **Step 6: 更新 SOUL**：Phase 1.5 入口先检查 typed state；已有 `patient_mode` 禁止再次询问。
- [ ] **Step 7: 验证与提交**

```bash
cd backend && PYTHONPATH=. uv run pytest tests/test_eligibility_workflow_state.py tests/test_summarization_middleware.py -v
make lint
git add backend/packages/harness/deerflow/agents/thread_state.py backend/packages/harness/deerflow/agents/middlewares/durable_context_middleware.py backend/packages/harness/deerflow/agents/middlewares/summarization_middleware.py backend/tests/test_eligibility_workflow_state.py backend/tests/test_summarization_middleware.py
git commit -m "fix: persist eligibility workflow state"
```

### Task 4: 为子代理接入实时、按类型预算

**Files:**
- Modify: `backend/packages/harness/deerflow/config/subagents_config.py`
- Modify: `backend/packages/harness/deerflow/subagents/config.py`
- Modify: `backend/packages/harness/deerflow/subagents/registry.py`
- Modify: `backend/packages/harness/deerflow/subagents/executor.py`
- Modify: `backend/packages/harness/deerflow/agents/middlewares/tool_error_handling_middleware.py`
- Create: `backend/tests/test_subagent_token_budget.py`

**Interfaces:**
- Produces: `SubagentTokenBudgetConfig(enabled, max_input_tokens, max_output_tokens, max_tokens, warn_threshold, hard_stop_threshold)`。
- Produces: 每个子任务独立 `run_id/task_id` 预算；达到 hard stop 后取消该子代理并返回 typed `budget_exceeded`。

- [ ] **Step 1: 写失败测试**：两个并发子代理各自计数；A 超预算只能停止 A，B 继续；collector usage 必须在下一次模型调用前触发预算。
- [ ] **Step 2: 验证失败**

```bash
cd backend && PYTHONPATH=. uv run pytest tests/test_subagent_token_budget.py -v
```

- [ ] **Step 3: 接入子代理 middleware**：预算直接消费 `SubagentTokenCollector`，不依赖 lead `TokenUsageMiddleware` 或任务结束后的 usage 回填。
- [ ] **Step 4: 先以 warn-only 配置上线 replay**：不得直接启用旧注释中的统一 150k；先记录各类型 p50/p95/max。
- [ ] **Step 5: 校准后配置按类型预算**：OCR、parse、judge、QC、repair 分别设阈值；hard stop 至少高于健康样本 p95 的 1.25 倍。
- [ ] **Step 6: 验证与提交**

```bash
cd backend && PYTHONPATH=. uv run pytest tests/test_subagent_token_budget.py tests/test_subagent_executor.py -v
make lint
git add backend/packages/harness/deerflow/config/subagents_config.py backend/packages/harness/deerflow/subagents/config.py backend/packages/harness/deerflow/subagents/registry.py backend/packages/harness/deerflow/subagents/executor.py backend/packages/harness/deerflow/agents/middlewares/tool_error_handling_middleware.py backend/tests/test_subagent_token_budget.py
git commit -m "feat: enforce per-subagent token budgets"
```

### Task 5: 实现版本感知 read_file 去重和批量 Patch

**Files:**
- Create: `backend/packages/harness/deerflow/agents/middlewares/read_file_dedup_middleware.py`
- Create: `backend/packages/harness/deerflow/config/read_dedup_config.py`
- Modify: `backend/packages/harness/deerflow/config/app_config.py`
- Modify: `backend/packages/harness/deerflow/agents/middlewares/tool_error_handling_middleware.py`
- Modify: `backend/packages/harness/deerflow/sandbox/tools.py`
- Create: `backend/tests/test_read_file_dedup_middleware.py`
- Create: `backend/tests/test_batch_json_patch_tool.py`

**Interfaces:**
- Produces: cache key `(sandbox_id, thread_id, path, start_line, end_line, content_hash)`。
- Produces: `apply_json_patches(path: str, expected_hash: str, patches: list[JsonPatch]) -> PatchResult`，一次加锁、一次版本校验、一次写入。

- [ ] **Step 1: 写缓存失败测试**：同版本同范围二次读返回短引用；文件修改后必须 cache miss；不同线程/沙箱不得共享 mutable 文件缓存。
- [ ] **Step 2: 写批量 Patch 失败测试**：expected hash 不符拒绝；全部 patch 原子成功或全部不写；成功后 read-before-write 标记失效。
- [ ] **Step 3: 运行测试确认失败**

```bash
cd backend && PYTHONPATH=. uv run pytest tests/test_read_file_dedup_middleware.py tests/test_batch_json_patch_tool.py -v
```

- [ ] **Step 4: 实现 middleware 和工具**：SKILL/reference 可按内容 hash 复用；judgment/criteria JSON 只能在版本相同期间复用。
- [ ] **Step 5: 工具参数预检**：`grep` 收到文件路径时返回明确建议，避免 13 次 file-as-directory 重试；非法绝对路径在模型下一轮前提供规范路径。
- [ ] **Step 6: 验证与提交**

```bash
cd backend && PYTHONPATH=. uv run pytest tests/test_read_file_dedup_middleware.py tests/test_batch_json_patch_tool.py tests/test_sandbox_tools_security.py -v
make lint
git add backend/packages/harness/deerflow/agents/middlewares/read_file_dedup_middleware.py backend/packages/harness/deerflow/config/read_dedup_config.py backend/packages/harness/deerflow/config/app_config.py backend/packages/harness/deerflow/agents/middlewares/tool_error_handling_middleware.py backend/packages/harness/deerflow/sandbox/tools.py backend/tests/test_read_file_dedup_middleware.py backend/tests/test_batch_json_patch_tool.py
git commit -m "perf: deduplicate versioned file reads"
```

### Task 6: 将 Phase 1 和 OCR 搬运改为确定性批处理

**Files:**
- Modify: `skills/custom/criteria-parser/scripts/locate_criteria_sections.py`（运行态 custom skill）
- Modify: `backend/packages/community/cellflow_community/textin/tools.py`
- Create: `backend/packages/community/cellflow_community/textin/batch.py`
- Create: `backend/tests/test_textin_batch_parse.py`
- Modify: `backend/.deer-flow/agents/eligibility-screener/SOUL.md`（运行态）

**Interfaces:**
- Produces: locator 一次输出 `eligibility_criteria_raw.md`、`criteria_meta.json` 和机器可读诊断；不生成 protocol 全量副本。
- Produces: `parse_documents_batch(paths, output_dir, concurrency=3) -> BatchParseResult`。

- [ ] **Step 1: 为全角编号、标题无空格、NFKC 和当前方案 fixture 写 locator 失败测试**；断言一次定位成功且无 `试验方案_locate*`。
- [ ] **Step 2: 为 batch OCR 写失败测试**：28 扫描页只调用 28 次 provider；11 文本页不调用；已有页跳过；并发不超过3。
- [ ] **Step 3: 实现最小规范化和批处理**：服务端完成 parse artifact 读取、目标 Markdown 写入、表格复制和 coverage 返回；只有异常页进入视觉兜底。
- [ ] **Step 4: 更新 SOUL**：删除 LLM 调试 locator、逐页 read/write 和多副本路径；改为两个确定性工具调用。
- [ ] **Step 5: 验证与提交**

```bash
cd backend && PYTHONPATH=. uv run pytest tests/test_textin_batch_parse.py -v
make lint
git add backend/packages/community/cellflow_community/textin/tools.py backend/packages/community/cellflow_community/textin/batch.py backend/tests/test_textin_batch_parse.py
git commit -m "perf: batch eligibility document parsing"
```

Custom skill 与运行态 SOUL 不纳入普通 git commit；执行时必须另做读回校验并同步到其受控发布源。

### Task 7: 收敛判定、QC、修订与理由链

**Files:**
- Modify: `skills/custom/eligibility-judgment/SKILL.md`（运行态 custom skill）
- Modify: `skills/custom/eligibility-judgment/references/judge-delegation.md`
- Modify: `skills/custom/eligibility-judgment/references/qc-delegation.md`
- Modify: `skills/custom/eligibility-judgment/scripts/judge_pack.py`
- Create: `backend/tests/test_eligibility_structured_judgment.py`

**Interfaces:**
- Produces: 单次 judgment 结构同时包含 `conclusion`、`evidence`、`reason`、`document`、`page`。
- Produces: QC 返回 `findings` + `patches`；由 Task 5 的 `apply_json_patches` 原子应用。
- 保留: `uncertain_recheck.py`、`exclusion_direction_check.py`、结构闸。

- [ ] **Step 1: 写失败测试**：判定结果缺 reason/evidence/page 时 schema 失败；QC patch 后 conclusion 与 reason 必须方向一致。
- [ ] **Step 2: 写兼容测试**：旧的 `reasons_*.json` 仍可由 `judge_pack.py` 读取，但新流程不再要求独立 report-writer。
- [ ] **Step 3: 运行失败测试**

```bash
cd backend && PYTHONPATH=. uv run pytest tests/test_eligibility_structured_judgment.py -v
```

- [ ] **Step 4: 修改技能契约**：每 patient×track 使用小批结构化调用；不得整轨超长单调用；每批 5–10 条标准，确定性合并。
- [ ] **Step 5: 删除正常路径中的独立 reasons 任务**：仅兼容旧产物；判定修订 patch 同时改 conclusion/reason。
- [ ] **Step 6: 验证质量**：与基线最终 JSON 逐条比对，结论一致率、证据页完整率、方向冲突均达验收线。

### Task 8: 用 typed DAG 和运行时队列替代 lead 的批次轮询

**Files:**
- Create: `backend/packages/harness/deerflow/workflows/eligibility/state.py`
- Create: `backend/packages/harness/deerflow/workflows/eligibility/graph.py`
- Create: `backend/packages/harness/deerflow/workflows/eligibility/task_queue.py`
- Modify: `backend/packages/harness/deerflow/agents/lead_agent/agent.py`
- Create: `backend/tests/test_eligibility_workflow_graph.py`
- Create: `backend/tests/test_eligibility_task_queue.py`
- Modify: `README.md`
- Modify: `backend/AGENTS.md`

**Interfaces:**
- Produces: Phase DAG `P1 → P1.5 → P2(IN,EX,OCR) → P2.5 → P3 → P4 → P4.5 → P5`。
- Produces: `TaskQueue(max_concurrency=3)`；任一任务完成立即从 ready queue 补位。
- LLM 节点仅限 criteria semantic parse、judgment、semantic QC；管理节点不调用模型。

- [ ] **Step 1: 写 DAG 属性测试**：未选择模式不得进 P2；QC 未通过不得 slim/assemble；两轨未合并不得报告；clarification exactly once。
- [ ] **Step 2: 写真实滑动窗口测试**：三个任务耗时不同，第一个完成后第四个立即启动，不等待最慢任务。
- [ ] **Step 3: 运行测试确认失败**

```bash
cd backend && PYTHONPATH=. uv run pytest tests/test_eligibility_workflow_graph.py tests/test_eligibility_task_queue.py -v
```

- [ ] **Step 4: 实现最小 DAG 与队列**：保留现有通用 lead 路径作为 feature flag 回滚；eligibility-screener 单独路由到 typed graph。
- [ ] **Step 5: 将 copy/present/todo/summary 写入改成节点 side effect**：gate 成功后 exactly once，不再消耗独立 LLM 轮次。
- [ ] **Step 6: 更新文档和验证**

```bash
cd backend && PYTHONPATH=. uv run pytest tests/test_eligibility_workflow_graph.py tests/test_eligibility_task_queue.py -v
make lint
cd .. && pytest tests/skills/test_soul_skill_contract.py -v
```

- [ ] **Step 7: 提交**

```bash
git add backend/packages/harness/deerflow/workflows/eligibility backend/packages/harness/deerflow/agents/lead_agent/agent.py backend/tests/test_eligibility_workflow_graph.py backend/tests/test_eligibility_task_queue.py README.md backend/AGENTS.md
git commit -m "feat: add typed eligibility workflow"
```

---

## 3. 分阶段验收

### P0 验收：Tasks 1–5

| 指标 | 基线 | 目标 |
|---|---:|---:|
| 总 token | 34.4M | ≤14M |
| 子代理 token | 27.8M | ≤11M |
| 子代理 AI 轮次 | 379 | ≤180 |
| `read_file` | 360 | ≤150 |
| 重复模式询问 | 1 | 0 |
| 端到端时长 | 约62分钟 | ≤35–40分钟 |

### P1 验收：Tasks 6–8

| 指标 | 目标 |
|---|---:|
| 总 token | ≤8M |
| 子代理 token | ≤5M |
| 子代理 AI 轮次 | ≤80 |
| Lead LLM 调用 | ≤15 |
| 端到端时长 | ≤20–25分钟 |
| OCR provider 调用 | 每扫描页1次；无重复 |

### 质量验收（每阶段都必须满足）

- 标准总条数、四分类条数和条件 ID 守恒。
- OCR 扫描页覆盖率 100%，文本层页不重复 OCR，表格数不减少。
- 每条 judgment 有 document、page、evidence、conclusion、reason。
- `suspected_missed == []`。
- 排除方向 `conflicts == []`。
- QC 阻断项清零或按既有 HITL 规则暂停，不自动降级放行。
- `build_reports.py --verify` 通过。
- 与当前高质量基线逐条结论一致率 ≥95%；差异项必须人工复核。

## 4. 完整验证命令

```bash
cd backend
PYTHONPATH=. uv run pytest \
  tests/test_analyze_eligibility_run.py \
  tests/test_subagent_registry.py \
  tests/test_subagent_executor.py \
  tests/test_eligibility_workflow_state.py \
  tests/test_summarization_middleware.py \
  tests/test_subagent_token_budget.py \
  tests/test_read_file_dedup_middleware.py \
  tests/test_batch_json_patch_tool.py \
  tests/test_textin_batch_parse.py \
  tests/test_eligibility_structured_judgment.py \
  tests/test_eligibility_workflow_graph.py \
  tests/test_eligibility_task_queue.py -v
make lint
make test

cd ..
pytest tests/skills/test_soul_skill_contract.py -v
```

A/B replay 后重新运行（先把新会话 ID 写入环境变量）：

```bash
export NEW_THREAD_ID="实际重跑产生的线程 UUID"
cd backend
PYTHONPATH=. uv run python scripts/analyze_eligibility_run.py "$NEW_THREAD_ID" \
  --baseline ../.deer-flow/criteria-token-baseline.json \
  --output ../.deer-flow/criteria-token-after.json
```

报告必须同时给出 token、wall time、task/AI/tool/read 次数和全部质量闸；只降低 token 而质量失败不得判定优化完成。

## 5. 回滚顺序

1. Typed workflow 通过 feature flag 切回通用 lead graph。
2. 子代理预算先从 hard-stop 改为 warn-only，不删除 usage 观测。
3. 版本缓存整体关闭；不得只关闭失效校验。
4. Structured judgment 保留旧 reasons 兼容读取路径。
5. OCR batch 失败时回退现有逐页调用，但保持每页幂等键，禁止双路线同时运行。

## 6. 实施顺序

严格按 Task 1 → 8 执行。Task 2、3 是最大且风险较低的直接收益；在完成技能收窄和 typed state 前，不得先把统一 150k 子代理预算设为 hard-stop。Task 8 只在 P0 replay 达标且质量不退化后开始。
