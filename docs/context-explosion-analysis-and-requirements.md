# 上下文爆炸问题分析与优化需求文档

> 基于 `aca54c56-dcda-4d6c-8568-7776fc1d8803` 会话实测数据 + 代码链路核实，2026-07-11。
>
> 关联：[eligibility-screener-fix-changelog.md](./eligibility-screener-fix-changelog.md) 第 7 章 budget 分析、[eligibility-screener-fix-plan.md](./plans/eligibility-screener-fix-plan.md)

---

## 1. 问题陈述

eligibility-screener agent 在处理「试验方案 + 筛选期病历 + 筛选期检查」三份 PDF 的入排筛选任务时，**tool calls 与 read_file 反复读取导致主代理/子代理上下文爆炸**，单次 run 消耗 300–470 万 token（设计目标 < 35 万），其中 **97.7% 为 input token**--几乎全部 token 花在重复读取膨胀的上下文，而非产出。

本需求文档定义问题的完整根因链、优化目标与验收标准，供开发计划文档（`docs/context-explosion-optimization-plan.md`）落地。

---

## 2. 实测数据基线

数据源：`backend/.deer-flow/data/deerflow.db` runs 表 + `checkpoints.db`。

### 2.1 会话级 token 消耗

| 维度 | Run 1 (gpt-5-4) | Run 2 (deepseek-v4-pro) |
|------|------|------|
| 状态 | interrupted | success |
| 时长 | 70 min（13:42–14:52） | 10 min（09:11–09:21） |
| 总 token | 4,735,558 | 3,021,765 |
| input token | 4,588,881（96.9%） | 2,953,376（**97.7%**） |
| output token | 146,677（3.1%） | 68,389（2.3%） |
| lead : subagent | 49 : 51 | **16 : 84** |
| 每次调用平均 input | 152,760 | 177,751 |
| LLM 调用次数 | 31 | 17 |
| 步数（checkpoint） | - | 199（卡在 P1） |

### 2.2 上下文堆积证据

- 最大 checkpoint 480KB，其中 `__pregel_tasks` channel 占 268KB（待执行任务队列膨胀）
- 34 条消息中最大单条 5.5KB（read_file 输出），但累积重读导致每轮 input 17.8 万 token
- Run 2 尾部出现 3 次连续 `[TOKEN BUDGET EXCEEDED]`，input 从 392,418 → 434,806 → 477,304 持续爬升

### 2.3 上传文件规模

| 文件 | 大小 |
|------|------|
| 筛选期检查.pdf | 10.6 MB |
| 筛选期病历.pdf | 6.3 MB |
| 试验方案.pdf | 2.3 MB |

PDF 经 `pdf_to_image.py` 转图片后，OCR 产出多个 `.md` 文件；主代理与子代理需多次 `read_file` 读取这些产出。

---

## 3. 根因链

### 3.1 根因链总览

```
G1: read_file 被 exempt，输出全量留上下文（每次最高 50,000 字符 ≈ 12K token）
    │
    ▼
G2: 同一文件被反复 read_file，无去重机制
    （phase 间反复读 criteria_parsed.json / ocr_records.md）
    │
    ▼
G3: summarization 80K 触发点过高，tool 输出已大量堆积后才压缩
    （单轮即可塞入多份 read_file 输出 5万×3=15万字符 >> 80K）
    │
    ▼
G4: subagent 占 84% token，内部 read_file/OCR 循环累积
    （data-extractor/OCR 子代理 253 万 token 全在子代理内）
    │
    ▼
G5: lead agent prompt 无 read_file 约束规则
    （未禁止重复读、未要求用 phase summary 替代重读）
    │
    ▼
结果：每轮 LLM 调用重读全部历史消息（17.8 万 input/轮）
      → token 失控增长 → budget 硬停 → goal 循环未终止 → 300-470 万 token
```

### 3.2 各缺口详解

#### G1：read_file 被 exempt，输出全量留上下文

**位置**：`config.yaml` 的 `tool_output.exempt_tools: [read_file, read_file_tool]`；`backend/packages/harness/deerflow/agents/middlewares/tool_output_budget_middleware.py:431`

**现状**：`ToolOutputBudgetMiddleware._budget_tool_message()` 在 `tool_name in config.exempt_tools` 时直接返回原消息，**不外部化**。`read_file_output_max_chars: 50000` 意味着每次 read_file 最多 5 万字符（≈12K token）全量留在 history。

**影响**：N 次 read_file = 12K×N token 永久占用 context。run 2 中 read_file 出现在最大消息列表前列（5150、4817、3618、3219 字符），反复读取同一批文件导致 context 线性增长。

**为何 exempt**：历史设计（issue #3416）为让 read_file 能读回外部化的 tool 输出。但当前 read_file 自身输出也跟着被 exempt，形成"输出大文件 → 全量留 context → 再读再留"的累积。

#### G2：同一文件被反复 read_file，无去重

**位置**：`backend/packages/harness/deerflow/sandbox/tools.py` `read_file_tool` 无任何缓存/去重

**现状**：read_file_tool 每次调用都重新读磁盘返回全量内容，即使同一 run 内已读过同一文件。SOUL.md 的 Phase 2.5/3/4/5 前置步骤都要求 `read_file workspace/phase{N}_summary.json`，phase 间还会重读 `criteria_parsed.json`、`ocr_records.md`。

**影响**：同一份 `ocr_records.md`（多页 OCR 汇总，可达数万字符）在 P2.5/P3/P4/P5 各读一次，每次 5 万字符重复入 context。无"已读文件"记忆。

#### G3：summarization 80K 触发点过高

**位置**：`config.yaml` `summarization.trigger: tokens 80000`；`backend/packages/harness/deerflow/agents/middlewares/summarization_middleware.py`

**现状**：lead agent 单轮可塞入多份 read_file 输出（5万×3=15万字符 ≈ 37K token），加上 system prompt、tool 调用历史，单轮轻松超 80K。summarization 在 80K 才触发，此时已有大量 tool 输出堆积。

**已确认安全**：`SummarizationMiddleware._find_safe_cutoff_point` 会保持 AI/Tool message 配对（不切断 tool_calls 与对应 ToolMessage），降阈值不会破坏配对。

#### G4：subagent 占 84% token

**位置**：`backend/packages/harness/deerflow/subagents/executor.py:381`（`checkpointer=False`）

**现状**：subagent 跑独立 fresh loop（无持久化 checkpointer），但单次执行内多轮对话消息累积。data-extractor/OCR 子代理内部反复 view_image + read_file，253 万 token 全在子代理内消耗。

**已核实**：subagent 装配了 `ToolOutputBudgetMiddleware`（`build_subagent_runtime_middlewares` → `_build_runtime_middlewares`，与 lead 共用 outer_wrappers）。但 **read_file 同样被 exempt**（同一 config），子代理的 read_file 输出也不压缩。

#### G5：lead agent prompt 无 read_file 约束

**位置**：`backend/.deer-flow/agents/eligibility-screener/SOUL.md`

**现状**：SOUL.md 虽要求 phase 间用 summary 文件（D4 已加），但**未明确禁止重复读**、未规定"同一文件同一 run 最多读一次"、未要求判定阶段证据只来自 extraction.json 而非重读 OCR 原文（C1 已部分约束判定阶段，但 lead agent 主动重读仍可能发生）。

---

## 4. 优化目标与验收标准

### 4.1 量化目标

| 指标 | 当前（Run 2） | 目标 | 验收方式 |
|------|------|------|---------|
| 单 run 总 token | 3,021,765 | < 600,000 | 重跑 aca54c56 会话，查 runs 表 |
| input 占比 | 97.7% | < 85% | runs 表 input/total |
| 每次调用平均 input | 177,751 | < 80,000 | total_input/llm_call_count |
| subagent 占比 | 84% | < 60% | subagent_tokens/total_tokens |
| budget 硬停触发次数 | 3（循环） | 0 或 1（终止） | checkpoint 尾部无连续 EXCEEDED |
| 报告产出 | success 但卡 P1 | 正常产出 screening_report.html | outputs 目录 |

### 4.2 功能性需求

| ID | 需求 | 验收标准 |
|----|------|---------|
| FR-1 | read_file 大输出不再全量留 context | read_file 输出 > 阈值时外部化，context 仅留预览 + 路径引用 |
| FR-2 | 同一文件同一 run 不重复全量读取 | 二次读取同一文件时返回缓存或引用，不重新入 context |
| FR-3 | summarization 更早压缩 tool 输出 | 阈值降至合理水平，且不破坏 AI/Tool 配对 |
| FR-4 | subagent 内部 read_file 输出受控 | 子代理 read_file 输出同样外部化/压缩 |
| FR-5 | lead agent 遵循 read_file 读取纪律 | SOUL.md 明确约束，phase 间用 summary 替代重读 |

### 4.3 非功能性需求

| ID | 需求 | 验收标准 |
|----|------|---------|
| NFR-1 | 不引入回归 | 现有 179 个测试全通过 |
| NFR-2 | 不影响小文件读取 | < 阈值的文件全量返回（OCR 单页 .md 不受影响） |
| NFR-3 | 不破坏 tool_call 配对 | summarization 后 agent 仍能正常 tool 调用 |
| NFR-4 | 配置可调 | 所有阈值通过 config.yaml 配置，非硬编码 |
| NFR-5 | TDD | 每个改动配套单测 |

---

## 5. 优化方案（5 个，按优先级）

### 方案 1：read_file 纳入 tool_output 外部化（治 G1）- P0

**改动**：`config.yaml` 移除 `read_file`/`read_file_tool` 的 exempt，或改为仅对小文件 exempt。

```yaml
tool_output:
  exempt_tools: []  # 原为 [read_file, read_file_tool]
```

**机制**：read_file 输出 > `externalize_min_chars`(8000) 时，外部化到 `.tool-results/`，context 仅留 `preview_head_chars`(2000) + `preview_tail_chars`(1000) 预览 + 路径引用。agent 需细节时用 `read_file(start_line, end_line)` 按段读。

**收益**：单次 read_file 从 12K token 降至 ~1K token（预览），N 次读取从 12K×N 降至 ~1K×N + 按需分段读。

**风险**：外部化后 agent 看不到完整内容，可能影响判定。
**缓解**：
- 小文件（< 8K，如 OCR 单页 .md）全量返回不受影响
- 大文件（criteria_parsed.json、ocr_records.md 汇总）外部化，agent 用 start_line/end_line 按段读
- 预览保留头 2000 + 尾 1000 字符，关键信息通常在头尾

### 方案 2：lead agent 引入 read_file 读取纪律（治 G2/G5）- P0

**改动**：SOUL.md 新增"上下文读取纪律"原则。

```markdown
### 11. 上下文读取纪律
- 每个 Phase 开头只读 phase{N}_summary.json，禁止重读前序 Phase 已读取的文件
- 同一文件同一 run 内最多 read_file 一次；需再次引用时从已有 ToolMessage 回顾，不重新 read
- 判定阶段证据只来自 extraction.json，禁止为补证据重读 OCR 原文
- 需要文件局部内容时用 read_file(start_line, end_line) 按段读，不读全量
```

**收益**：从行为层消除重复读取，配合 D4 phase summary 机制。

### 方案 3：read_file 工具层去重缓存（治 G2）- P1

**改动**：`read_file_tool` 增加 per-run 已读文件缓存，二次读取同一文件同一范围时返回缓存引用而非全量内容。

**机制**：
- 以 `(thread_id, run_id, file_path, start_line, end_line)` 为 key 缓存
- 二次读取返回 `[Already read: {path} ({chars} chars). Content available in prior tool message.]`
- 文件被 write_file/str_replace 修改后失效缓存

**收益**：工具层强制去重，不依赖 agent 遵守 prompt 纪律。

**风险**：缓存失效逻辑复杂（write/str_replace 后需失效）；跨 run 缓存泄漏。
**缓解**：缓存 key 含 run_id，run 结束自动清理；write 工具触发同文件缓存失效。

### 方案 4：降低 summarization 触发阈值（治 G3）- P1

**改动**：`config.yaml` `summarization.trigger` 从 80000 降至 50000。

```yaml
summarization:
  trigger:
  - type: tokens
    value: 50000  # 原 80000
```

**收益**：更早压缩 tool 输出堆积。

**已确认安全**：`_find_safe_cutoff_point` 保持 AI/Tool 配对。

**风险**：触发过频，压缩损失上下文。
**缓解**：`keep: messages 30` 保留近期上下文；配合 DurableContextMiddleware 保持关键信息。

### 方案 5：降低 read_file 输出上限（治 G1 兜底）- P1

**改动**：`config.yaml` `sandbox.read_file_output_max_chars` 从 50000 降至 20000。

```yaml
sandbox:
  read_file_output_max_chars: 20000  # 原 50000
```

**收益**：单次 read_file 上限从 12K token 降至 5K token，兜底防止超大输出。

**风险**：大文件需多次分段读，增加 tool call 次数。
**缓解**：配合方案 1 外部化，大文件本就该按段读。

---

## 6. 方案依赖与冲突分析

### 6.1 依赖关系

```
方案 1（read_file 外部化） ──独立──> 方案 2（SOUL 纪律）
        │                                  │
        ▼                                  ▼
方案 5（read_file 上限） <──互补── 方案 4（summarization 阈值）

方案 3（工具去重缓存）──独立── 可与任何方案组合
```

### 6.2 冲突与权衡

| 组合 | 冲突 | 权衡 |
|------|------|------|
| 方案 1 + 方案 5 | 双重限制 read_file | 互补：1 控大文件外部化，5 控单次上限，无冲突 |
| 方案 1 + 方案 4 | 都压缩 context | 互补：1 压缩 tool 输出，4 压缩历史消息 |
| 方案 2 + 方案 3 | 都去重 | 冗余：2 是 prompt 层软约束，3 是工具层硬约束。**建议二选一或都做（3 兜底 2）** |
| 方案 4 + budget 硬停 | summarization 频繁触发可能与 budget 交互 | 需确认 summarization 后 token 计数是否重置 |

### 6.3 推荐组合

**最小可行（P0）**：方案 1 + 方案 2 -- 改动小、风险可控、直击事故根因（反复读 + 大输出留存）。

**完整方案（P0+P1）**：方案 1 + 2 + 3 + 4 + 5 -- 多层防御，但方案 3 需独立评估缓存失效复杂度。

---

## 7. 涉及文件清单

| 文件 | 涉及方案 | 改动类型 |
|------|---------|---------|
| `config.yaml` | 1, 4, 5 | 配置调整 |
| `backend/.deer-flow/agents/eligibility-screener/SOUL.md` | 2 | 新增原则 11 |
| `backend/packages/harness/deerflow/sandbox/tools.py` | 3 | read_file_tool 增加缓存 |
| `backend/tests/test_read_file_dedup.py`（新增） | 3 | 去重缓存单测 |
| `backend/tests/test_tool_output_read_file_externalize.py`（新增） | 1 | read_file 外部化单测 |

---

## 8. 风险评估

| 风险 | 概率 | 影响 | 缓解 |
|------|------|------|------|
| read_file 外部化后 agent 无法判定（证据不全） | 中 | 高 | 小文件全量返回；预览保留头尾；agent 可按段读 |
| 工具层去重缓存失效逻辑有 bug | 中 | 中 | 缓存 key 含 run_id；write 工具触发失效；充分单测 |
| summarization 降阈值导致上下文丢失 | 中 | 中 | keep=30 messages；DurableContextMiddleware 保留关键信息 |
| subagent 内部压缩影响 OCR 质量 | 低 | 中 | OCR 单页 .md < 8K 不受外部化影响 |
| 降 read_file 上限增加 tool call 次数 | 高 | 低 | 配合外部化按段读，tool call 增加但单次 token 大降 |

---

## 9. 验证计划

### 9.1 单元测试

| 测试文件 | 覆盖方案 |
|----------|---------|
| `test_tool_output_read_file_externalize.py` | 方案 1：read_file > 阈值外部化 |
| `test_read_file_dedup.py` | 方案 3：同文件二次读返回引用 |
| `test_summarization_tool_pair_preservation.py` | 方案 4：降阈值后 tool 配对不破坏 |

### 9.2 集成验证

1. 重跑 aca54c56 会话，上传同样 3 份 PDF
2. 观察 runs 表：总 token < 60 万、input 占比 < 85%
3. 观察 checkpoint：无连续 `[TOKEN BUDGET EXCEEDED]`
4. 观察产出：`screening_report.html` + `criteria_report.html` 正常生成
5. 观察进度：实际推进到 P5（非卡 P1）

### 9.3 回归测试

```bash
cd backend && make test      # 后端全量（现有 179+ 测试通过）
cd frontend && pnpm check    # 前端 lint + type
```

---

## 10. 附录：已核实的技术事实

| 事实 | 核实方式 | 结论 |
|------|---------|------|
| subagent 装配 ToolOutputBudgetMiddleware | `tool_error_handling_middleware.py:148` `_build_runtime_middlewares` 被 lead 和 subagent 共用 | ✅ 已装配 |
| read_file exempt 同时影响 lead 和 subagent | 同一 `app_config` 传入两者 | ✅ 都受影响 |
| summarization 保持 AI/Tool 配对 | `SummarizationMiddleware._find_safe_cutoff_point` 源码 | ✅ 安全 |
| read_file 单次上限 50,000 字符 | `config.yaml` `read_file_output_max_chars: 50000` | ✅ 确认 |
| subagent 无持久化 checkpointer | `executor.py:381` `checkpointer=False` | ✅ fresh loop |
| subagent state 不带父线程历史 | `executor.py:498` `state["messages"]` 仅 System+Human | ✅ 上下文隔离 |
