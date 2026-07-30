# Eligibility-Screener 监控问题优化开发计划

> 来源：[eligibility-screener-monitoring-issues.md](../eligibility-screener-monitoring-issues.md)（会话 `b729d95e` 实时监控分析，2026-07-13）
>
> 关联：[eligibility-screener-fix-plan.md](./eligibility-screener-fix-plan.md)、[eligibility-screener-fix-changelog.md](../eligibility-screener-fix-changelog.md)、[context-explosion-optimization-plan.md](../context-explosion-optimization-plan.md)
>
> 状态：待评审 / 待实施

---

## 1. 背景与目标

前序修复（fix-plan / changelog）已根治 `aca54c56` 会话的卡死与上下文爆炸问题（图片格式、覆盖率、token_budget 硬停、run watchdog、read_file 外部化等）。本计划针对 **新一轮监控会话 `b729d95e` 发现的 10 项运行质量问题**，聚焦以下三类：

1. **上下文压缩效率**：summarize 高频低效震荡（#2）、首次摘要为空（#3）
2. **交付与产出可见性**：present_files 重复/遗漏（#4）、`.tool-results` 误暴露（#7）、输入边界不清（#8）
3. **子代理与质控质量**：recursion 耗尽（#5）、QC 退化为脚本模式（#6），以及路径/todo 轻微问题（#9/#10）

### 1.1 优化目标

| 目标 | 衡量指标 |
|------|----------|
| 消除 summarize 震荡 | 单 run 内"低效压缩"（每次 <5 条）次数降为 0 |
| 首次摘要有效 | 首次 summarize 不再产出全 None 空摘要 |
| 产出交付完整 | QC/推断理由等过程文件全部 present，无重复 present |
| QC 走 LLM 推理 | QC 阶段 0 次 bash 脚本执行；语义级问题检出不退化 |
| 上下文纯净 | `.tool-results` 不出现在 agent 的 `ls` 视图 |
| 子代理不耗尽 | eligibility-screener 相关子代理 0 次 `GraphRecursionError` |

### 1.2 现状基线（已核对代码）

| 配置/机制 | 当前值 | 位置 |
|-----------|--------|------|
| `summarization.trigger` | 50000 tokens | `config.yaml:237` |
| `summarization.keep` | 30 messages | `config.yaml:240` |
| `tool_output.externalize_min_chars` | 8000 | `config.yaml:274` |
| `tool_output.exempt_tools` | `[ls, glob, grep]` | `config.yaml` |
| `tool_output.storage_subdir` | `.tool-results` | `config.yaml` |
| `read_file_dedup.enabled` | `false` | `config.yaml:352` |
| `search_dedup.enabled` | `false` | `config.yaml` |
| 子代理 `max_turns`（=recursion_limit） | data-extractor 150 / quality-control 100 / report-writer 100 | `config.yaml:194-223` |
| eligibility-screener `tool_groups` | 含 `bash` | `.deer-flow/agents/eligibility-screener/config.yaml` |
| `allowed_subagents` | 含 `quality-control`、`bash` | 同上 |

**关键代码事实**（避免方案落到不存在的机制上）：
- `recursion_limit` 直接取 `SubagentConfig.max_turns`（`subagents/executor.py:557`）。
- summarize 无 cooldown/最小压缩量约束，`_maybe_summarize` 只要 `_should_summarize` 为真且 `cutoff_index>0` 就压缩（`summarization_middleware.py`）。
- summarize 无摘要质量校验，`summary is None` 才跳过；空 None 文本会照常写入 `summary_text`。
- `present_file_tool` 依赖 `merge_artifacts` reducer 去重，工具本身无"内容未变则跳过"逻辑。
- `ls_tool` → `sandbox.list_dir()` 无 dotfile / `.tool-results` 过滤（`sandbox/tools.py:1511`）。
- `TodoMiddleware` 继承 `TodoListMiddleware`，`write_todos` 无"内容与当前一致则跳过"去重。

---

## 2. 问题→方案总览

| ID | 优先级 | 问题 | 方案层 | 主改动位置 |
|----|--------|------|--------|-----------|
| #1 | P0 已修复 | 跨 provider summarize 触发失败 | — | 已完成，仅回归验证 |
| #2 | P1 | summarize 震荡（高频低效压缩） | 后端中间件 + config | `summarization_middleware.py`、`config.yaml` |
| #3 | P1 | 首次 summarize 空摘要 | 后端中间件 | `summarization_middleware.py` |
| #4 | P2 | present_files 重复 + 子代理产出未 present | Skill prompt（主）+ 工具去重（可选） | `eligibility-judgment` / `screening-report-generator` SKILL、SOUL.md、`present_file_tool.py` |
| #5 | P2 | 子代理 recursion 耗尽 | config + prompt | `config.yaml`、子代理 prompt |
| #6 | P2 | QC 用 bash 脚本执行 | Skill prompt（主）+ guardrail（加固） | SOUL.md、`quality-control` prompt、guardrail |
| #7 | P2 | `.tool-results` 对 agent 可见 | 后端工具 | `sandbox/tools.py` ls / list_dir |
| #8 | P2 | 输入边界不清 | Skill prompt | `eligibility-judgment` SKILL、SOUL.md |
| #9 | P3 | 路径使用错误 | 工具 description / prompt | `sandbox/tools.py` docstring、SOUL.md |
| #10 | P3 | write_todos 重复写入 | 后端中间件 | `todo_middleware.py` |

方案分层原则：**能用 prompt 约束的优先 prompt（零回归风险、可热更新）；需要硬保证或跨会话生效的用代码 + 测试。**

---

## 3. 详细方案

### #2 — 消除 summarize 震荡（P1）

**根因（已核对）**：`keep=30 messages` 保留窗口内若含大 read_file 预览/结果，保留量本身逼近 50k 触发阈值，导致几乎每次 model call 都触发 summarize，但每次仅压缩 3-4 条。当前中间件无 cooldown、无最小压缩收益门槛。

**方案（三管齐下，A+B 为核心）**：

**A) 中间件增加 cooldown + 最小压缩收益门槛（后端，核心）**

在 `DeerFlowSummarizationMiddleware` 增加两个约束，任一不满足则本轮跳过压缩：

1. **Cooldown**：距上次成功压缩不足 N 次 model call（默认 `cooldown_calls=3`）则跳过。用 `runtime.context` 或实例内 per-(thread,run) 计数记录上次压缩的调用序号（参考 `TodoMiddleware` 的 per-(thread_id,run_id) bookkeeping 与 bounded LRU 清理模式）。
2. **最小压缩收益**：`cutoff_index` 对应的 `messages_to_summarize` 条数 < `min_messages_to_summarize`（默认 5）时跳过——避免"只压 3-4 条"的低效压缩。

改动点：`_maybe_summarize` / `_amaybe_summarize` 在 `_determine_cutoff_index` 后、`_partition_messages` 前插入门槛判断；压缩成功返回前记录本次调用序号。

```python
# 伪代码，加入 _maybe_summarize
if self._in_cooldown(runtime):
    return None
cutoff_index = self._determine_cutoff_index(messages)
if cutoff_index <= 0:
    return None
if cutoff_index < self._min_messages_to_summarize:   # 收益门槛
    return None
...
# 压缩成功后
self._record_summarization(runtime)
```

**参数来源**：新增到 `SummarizationConfig`（读 `config.yaml`），默认 `cooldown_calls=3`、`min_messages_to_summarize=5`，未配置时用默认值，保证向后兼容。

**B) keep 改为 token-based（config）**

将保留窗口从"消息条数"改为"token 量"，更精确控制保留量、拉开与 trigger 的距离：

```yaml
summarization:
  trigger:
  - type: tokens
    value: 50000
  keep:
    type: tokens
    value: 25000        # 由 30 messages 改为 25k tokens，保留窗口 ≈ trigger 的一半
```

效果：保留量恒定 ≈25k，与 50k trigger 间有稳定缓冲区，压缩后不会立刻再次逼近阈值。

**C) 启用 read_file 去重缓存（config，减小保留窗口体积）**

保留窗口中的大 read_file 结果是震荡诱因之一。启用已实现但默认关闭的去重：

```yaml
read_file_dedup:
  enabled: true
search_dedup:
  enabled: true
```

> 决策点：C 依赖 changelog 第 8 章"待重跑验证"的结论。若 A+B 已足够消除震荡，C 可保持关闭作为预留。建议先上 A+B，重跑观测后再决定 C。

**测试**：`backend/tests/test_summarization_cooldown.py`（新增）
- `test_skips_within_cooldown`：cooldown 期内不压缩
- `test_summarizes_after_cooldown`：cooldown 结束后正常压缩
- `test_skips_when_below_min_messages`：待压缩条数 < 门槛时跳过
- `test_records_call_index_on_success`：压缩成功记录调用序号
- `test_cooldown_state_isolated_per_run`：跨 run 隔离 + bounded 清理

---

### #3 — 首次 summarize 空摘要防护（P1）

**根因（已核对）**：早期消息主要是 task 子代理调度的结构化 JSON 状态，deepseek-v4-flash 无法提取有效摘要，产出 `## SESSION INTENT\nNone\n...`（78 chars 全 None）。当前中间件仅在 `summary is None`（异常）时跳过；空 None 文本会照常写入 `summary_text` 并删除原始消息 → 丢失早期上下文。

**方案**：

**A) 摘要质量校验（后端，核心）**

在 `_maybe_summarize` / `_amaybe_summarize` 拿到 `summary` 后、返回 `RemoveMessage` 前，加入质量校验 `_is_low_quality_summary(summary)`；低质量则**跳过本轮压缩、保留原始消息**（return None），不写入 summary_text。

判定规则（保守，避免误伤正常短摘要）：
```python
def _is_low_quality_summary(self, summary: str) -> bool:
    text = summary.strip()
    if len(text) < self._min_summary_chars:        # 默认 120
        return True
    # 去掉 markdown 小节标题后，正文是否全为 None/空
    body = re.sub(r"(?im)^#+.*$", "", text)
    body = re.sub(r"(?i)\bnone\b", "", body).strip()
    return len(body) < self._min_summary_body_chars  # 默认 40
```

**B) summary prompt 增加 tool-call/task 消息处理指导（prompt）**

自定义 `summary_prompt`（当前 `config.yaml` 为 `null` 用 LangChain 默认），补充对结构化 JSON / task 调度类消息的摘要指导：从 task result 中提取"已完成的子任务、产出文件路径、关键判定结论"，而非逐字复述 JSON。

**C)（可选）首次压缩用更强模型**

当 `previous_summary` 为空（首次压缩）时临时切换到更强模型。**代价高、收益边际**，且当前 `_summary_model` 在实例构造时固定。列为可选项，默认不实施——A+B 已能防止空摘要落地。

**测试**：`backend/tests/test_summarization_quality_gate.py`（新增）
- `test_all_none_summary_skips_compaction`：全 None 摘要 → 返回 None、原消息保留
- `test_too_short_summary_skips`：过短摘要跳过
- `test_valid_summary_compacts`：正常摘要正常压缩
- `test_partial_none_summary_kept`：部分小节为 None 但正文有效 → 正常压缩

---

### #4 — present_files 重复与子代理产出遗漏（P2）

**根因**：交付规范未在 skill prompt 中显式声明，主 agent 仅 present 最终合并文件；`criteria_parsed.json` 被 present 3 次（重复）；QC 报告、推断理由等过程文件（对用户有价值）未 present。

**方案（prompt 为主，工具去重为辅）**：

**A) Skill prompt 明确交付清单（核心）**

在 `eligibility-judgment` / `screening-report-generator` SKILL.md 与 SOUL.md 增加"交付文件清单"，要求所有产出统一移动到 `/mnt/user-data/outputs/` 并一次性 present：

```markdown
## 交付文件清单（全部完成后一次性 present_files）
必交付：
1. /mnt/user-data/outputs/criteria_parsed.json      入排标准解析
2. /mnt/user-data/outputs/judgments_{patient_id}.json 最终判定
3. /mnt/user-data/outputs/screening_report.html      筛选报告
4. /mnt/user-data/outputs/criteria_report.html       标准解析报告
过程文件（对用户有参考价值，需交付）：
5. /mnt/user-data/outputs/qc_report_{patient_id}.json  QC 报告（含质控问题明细）
6. /mnt/user-data/outputs/reasons_{patient_id}.json    推断理由
规则：
- 每个文件仅 present 一次；内容更新后才可再次 present。
- 分阶段产出时，不逐个 present，待某阶段全部就绪后批量 present。
```

**B) 子代理产出回收（prompt）**

在 `task(...)` 调度 data-extractor / quality-control / report-writer 时，要求子代理**将最终产出移动到 `/mnt/user-data/outputs/` 并在 task result 中显式声明产出文件路径清单**；主 agent 收到 result 后按清单核对并 present。

**C) present_files 内容去重（后端，可选加固）**

`present_file_tool` 依赖 `merge_artifacts` 去重路径，但重复调用仍消耗一次工具调用 + token。可在 `ThreadState` 增加 per-path 内容指纹（mtime 或 size+hash），`present_files` 时对"路径已存在且指纹未变"的项静默跳过并在 ToolMessage 中提示。

> 决策：C 为可选。优先靠 A/B 从行为层消除重复；C 作为兜底，避免主 agent 仍偶发重复调用时的浪费。若实施需同步更新 `test_present_file_tool_core_logic.py`。

**测试**（若实施 C）：`test_present_file_dedup_unchanged`（内容未变跳过）、`test_present_file_represent_on_change`（内容变化重新 present）。A/B 为 prompt 改动，通过集成重跑验证。

---

### #5 — 子代理 recursion limit 耗尽（P2）

**根因（已核对）**：`recursion_limit = SubagentConfig.max_turns`。当前 `quality-control=100`、`report-writer=100`。监控中某 task 子代理跑满 100 步触发 `GraphRecursionError`。

**方案**：

**A) 针对性上调 max_turns（config）**

结合任务复杂度上调（并保留余量）：

| subagent | 当前 | 建议 | 理由 |
|----------|------|------|------|
| quality-control | 100 | 150 | 多患者多文档语义 QC，多轮读取比对 |
| report-writer | 100 | 150 | 报告拼装 + 多文件读取 |
| data-extractor | 150 | 150（不变） | 已足够 |

**B) 子代理 prompt 强调分步收敛（prompt）**

在对应子代理 prompt 增加："优先按段 read_file、用 grep 定位而非全量读；避免深层嵌套委派；接近轮次上限时立即产出当前结果并声明未完成项"。

**C) 降级/收尾策略（prompt + 观测）**

依赖已实现的 goal-loop 终止 + C2 watchdog 兜底。子代理 prompt 补充"轮次紧张时产出部分结果"的收尾指令，避免耗尽即空产出。

**测试**：配置段断言（沿用 changelog #9 的验证方式，确认 5 个子代理 max_turns 已配置且 quality-control/report-writer = 150）；`cd backend && make test` 回归 token_budget/goal_worker/watchdog 无回归。

---

### #6 — QC 禁止 bash 脚本执行（P2，质量关键）

**根因（已核对）**：主 agent 在 QC 阶段用 bash 编写 `run_qc.py` 等脚本做规则型结构校验，绕过 LLM 语义推理；脚本仅检出结构性问题（`missing_transform`），而 `task(quality-control)` 子代理能检出 12 项语义级问题。且脚本硬编码宿主机绝对路径存在安全风险。eligibility-screener 的 `tool_groups` 含 `bash`、`allowed_subagents` 含 `quality-control`。

**方案（prompt 硬约束 + guardrail 加固）**：

**A) SOUL.md / skill prompt 硬性禁止（核心）**

在 SOUL.md 新增原则（或扩展现有质控相关原则）：

```markdown
### QC 质控纪律（强制）
- QC 校验必须委派给 task(quality-control) 子代理，以 LLM 推理完成。
- 严禁在 QC 阶段用 bash 编写/执行 Python 脚本做质控（结构或语义均不可）。
- 结构性校验（JSON 合法性、字段存在性）仅作为前置自动化步骤，
  不计入 QC 质控流程，且不得用宿主机绝对路径。
```

**B) quality-control 子代理 prompt 明确 QC 范围（核心）**

```markdown
QC 校验范围（必须用 LLM 推理完成，禁止编写脚本）：
1. 判定结论正确性 — 数值比较、逻辑关系是否成立
2. 证据充分性 — 是否遗漏证据或过度推断
3. 跨文档一致性 — 同一信息在不同文档的描述是否矛盾
4. 时间窗口正确性 — 日期计算、参考日期选择是否合理
5. 条件拆分合理性 — AND/OR/除外拆分是否改变语义
```

**C) Guardrail 拦截 QC 阶段 bash（后端加固，可选）**

两条可选实现路径：
1. **配置层收窄**：为 QC 阶段/quality-control 子代理移除 bash tool_group（若架构支持按阶段/子代理裁剪工具）。注意 eligibility-screener 主 agent 其他阶段仍需 bash（图片提取脚本等），故不能全局移除——只能约束 quality-control 子代理的 tool_groups。检查 `quality-control` 子代理定义是否已含/可去除 bash。
2. **Guardrail 中间件**：参考现有 `sandbox_audit_middleware` / guardrail 机制，对"QC 阶段的 bash 且脚本内容含 qc/校验关键字"发出拦截或强警告。成本较高，仅在 A/B 不足时启用。

> 安全附带项：无论是否实施 C，SOUL.md 必须强调"脚本禁用宿主机绝对路径，只用 `/mnt/user-data/...` 虚拟路径"，消除硬编码 `/Users/louli/...` 的泄露风险。

**测试**：若实施 C-1（收窄 quality-control tool_groups），加配置断言测试；C-2 guardrail 需新增拦截单测。A/B 通过集成重跑验证 QC 语义问题检出不退化。

---

### #7 — `.tool-results` 对 agent 隐藏（P2）

**根因（已核对）**：`ToolOutputBudgetMiddleware` 将 >8000 chars 输出外部化到 `.tool-results/`；`ls_tool` → `sandbox.list_dir()` 无过滤，agent `ls /mnt/user-data/outputs/` 会看到该目录并可能误将中间产物当成果。

**方案**：

**A) ls 默认隐藏 dotfile 目录（后端，核心）**

在 `ls_tool`（`sandbox/tools.py:1511`）对 `sandbox.list_dir()` 结果做过滤：默认隐藏以 `.` 开头的顶层条目（dotfile 约定），至少隐藏 `.tool-results`。为 tree 格式输出需注意过滤整棵子树而非单行。

实现选择（二选一）：
- 在 `ls_tool` 内对 `children`（tree 行）过滤：剔除路径段含 `.tool-results` 或以 `.` 开头的行及其子行。
- 或在各 `list_dir` 实现（local/aio/e2b）增加 `include_hidden=False` 参数，ls 默认不含隐藏项。**倾向前者**（改动集中在 ls_tool，风险小，不动 sandbox 抽象接口）。

```python
# ls_tool 内，list_dir 之后
children = _filter_hidden_entries(children)  # 剔除 .tool-results 等 dotfile 子树
```

保留逃生阀：若 agent 显式 `ls /mnt/user-data/outputs/.tool-results`（path 直指隐藏目录）仍可列出。

**B) summarize 保留活跃 externalized 引用为 durable context（后端，可选）**

已存在 `DurableContextMiddleware`。可在 summarize 时把仍被引用的 `.tool-results/*` 路径登记为 durable，避免摘要后 agent 丢失文件引用。此项与 #2/#3 同区域，建议独立评估——若 read_file_dedup + preview 引用已够用，可暂缓。

**C) 定期清理已 summarize 的旧 tool-results（后端，可选）**

run 结束或 summarize 后清理不再被引用的 `.tool-results` 文件。列为低优先运维项。

**测试**：`backend/tests/test_ls_hides_tool_results.py`（新增）
- `test_ls_excludes_tool_results_dir`：普通 ls 不含 `.tool-results`
- `test_ls_excludes_dotfiles`：隐藏其它 dotfile
- `test_ls_explicit_hidden_path_lists`：显式列隐藏目录仍可见
- `test_ls_normal_entries_unaffected`：普通条目不受影响

---

### #8 — 入排匹配阶段输入边界定义（P2）

**根因**：workspace 混杂输入文件、中间脚本、中间产物、QC 结果，agent glob/ls 时难辨"输入资料"。

**方案（prompt 为主）**：

**A) skill prompt 声明输入清单（核心）**

在 `eligibility-judgment` SKILL.md / SOUL.md 判定阶段声明唯一判定依据：

```markdown
## 输入资料（仅以下文件作为判定依据）
- /mnt/user-data/uploads/试验方案.md        入排标准来源
- /mnt/user-data/uploads/筛选期病历.md      患者病历
- /mnt/user-data/uploads/筛选期检查.md      患者检查报告
- /mnt/user-data/workspace/criteria_parsed.json  结构化入排标准
其余文件（脚本、中间产物、QC 结果）不得作为判定证据来源。
```

**B) 分层 workspace 目录（prompt/skill 约定）**

约定 `workspace/inputs/`、`workspace/scripts/`、`workspace/intermediate/` 分层，减少同目录混杂。需评估对现有 skill 脚本路径的兼容性。

**C) task 传入显式输入路径（prompt）**

调度子代理时在 prompt 内显式传入输入文件绝对路径，而非让子代理自行 glob 搜索。与 #4-B 协同。

**测试**：prompt 改动，通过集成重跑验证判定证据来源正确。

---

### #9 — 路径使用错误（P3）

**根因**：bash 用了不安全绝对路径 `/patient_id`；grep 对文件而非目录调用。影响极低，agent 可自恢复。

**方案**：
- 在 `sandbox/tools.py` 的 `grep`/`bash` docstring 补充正确用法示例（grep 的 path 应为目录；路径统一 `/mnt/user-data/...`）。
- SOUL.md 强化路径规范：一律使用 `/mnt/user-data/workspace/patients/{id}` 等虚拟路径。

**测试**：docstring/prompt 改动，无需新增单测。

---

### #10 — write_todos 重复写入去重（P3）

**根因（已核对）**：相同 todo 列表被写入两次；`TodoMiddleware` 无内容去重。

**方案**：在 `TodoMiddleware` 的 `write_todos` 处理路径增加去重——新 todos 与当前 state `todos` 完全一致时跳过写入（或返回幂等提示）。需定位 `TodoListMiddleware` 中 write_todos 工具的落点（基类工具），在不破坏"状态确实变化时正常写入"的前提下做等值短路。

**测试**：`backend/tests/test_todo_dedup.py`（新增）
- `test_identical_todos_skipped`：内容完全一致 → 跳过
- `test_changed_todos_written`：任一项状态/内容变化 → 正常写入

---

## 4. 实施优先级与排期

| 阶段 | 任务 | 类型 | 预计 | 依赖 |
|------|------|------|------|------|
| **阶段1（P1 核心，先行）** | #2-A cooldown+收益门槛 | 后端 | 2h | — |
| | #2-B keep→token 25k | config | 5min | — |
| | #3-A 摘要质量校验 | 后端 | 1.5h | — |
| **阶段2（P2 prompt 批次）** | #4-A/B 交付清单+子代理回收 | prompt | 1h | — |
| | #6-A/B QC 禁 bash + 范围 | prompt | 1h | — |
| | #8-A/C 输入边界 | prompt | 45min | — |
| | #5-A/B recursion 上调+prompt | config+prompt | 45min | — |
| **阶段3（P2 后端）** | #7-A ls 隐藏 .tool-results | 后端 | 1.5h | — |
| **阶段4（P3 + 可选加固）** | #10 write_todos 去重 | 后端 | 1h | — |
| | #9 路径 docstring/prompt | prompt | 30min | — |
| | #2-C / #3-C / #4-C / #6-C / #7-B/C | 可选 | 视验证 | 阶段1-3 重跑结果 |

**建议顺序**：阶段1（消除震荡/空摘要，收益最高、纯后端可测）→ 阶段2（prompt 批次，热更新零回归风险）→ 阶段3（ls 隐藏）→ 阶段4（收尾 + 按重跑结果决定可选项）。

---

## 5. 验证计划

### 5.1 单元测试（新增）

| 测试文件 | 覆盖 |
|----------|------|
| `backend/tests/test_summarization_cooldown.py` | #2-A |
| `backend/tests/test_summarization_quality_gate.py` | #3-A |
| `backend/tests/test_ls_hides_tool_results.py` | #7-A |
| `backend/tests/test_todo_dedup.py` | #10 |
| （可选）`test_present_file_dedup.py` | #4-C |

运行：`cd backend && make test`；提交前 `cd backend && make lint && make format`。

### 5.2 回归测试

```bash
cd backend && make test          # 重点回归 summarization / todo / tool_output / token_budget / goal_worker
cd frontend && pnpm check        # 若无前端改动可略
```

### 5.3 集成重跑（同 `b729d95e` 场景）

用相同的"试验方案 + 筛选期病历 + 筛选期检查"输入重跑，观测：
1. **无震荡**：summarize 触发次数下降，无"每次仅压 3-4 条"的低效压缩（对比监控统计的 7 次→有效 3 次）。
2. **首次摘要有效**：首次 summarize 不产出全 None 空摘要（或被质量门槛跳过、保留原消息）。
3. **交付完整**：`qc_report_*.json`、`reasons_*.json` 等过程文件均 present，`criteria_parsed.json` 仅 present 1 次。
4. **QC 走推理**：QC 阶段 0 次 bash 脚本；QC 语义问题检出数不低于此前 task(quality-control) 的 12 项水平。
5. **上下文纯净**：`ls /mnt/user-data/outputs/` 不出现 `.tool-results`。
6. **无 recursion 耗尽**：quality-control / report-writer 子代理正常完成。

---

## 6. 风险评估

| 风险 | 影响 | 缓解 |
|------|------|------|
| cooldown 过长导致上下文超阈值堆积 | 单轮 input 偏大 | cooldown_calls 默认 3，配合 token_budget 硬停兜底；可按重跑数据调 |
| min_messages 门槛导致该压不压 | token 缓慢累积 | 门槛仅 5 条，且 trigger 仍会在真正超阈值时触发；keep→token 拉开缓冲 |
| 摘要质量门槛误判正常短摘要 | 该压不压 | 阈值保守（120/40 chars），仅拦全 None/极短；配单测边界用例 |
| keep→token 25k 保留窗口过小丢近期上下文 | 判定遗漏 | 25k ≈ trigger 一半，且 DurableContext/reminder 保关键信息 |
| ls 过滤 dotfile 影响其它合法 dotfile 场景 | 误藏文件 | 仅隐藏顶层 dotfile，保留显式列隐藏目录的逃生阀 |
| QC 禁 bash 后结构性校验缺位 | 结构问题漏检 | 保留前置自动化结构校验（不计入 QC），语义交 LLM |
| max_turns 上调掩盖真实低效循环 | 子代理跑更久 | 配合 prompt 分步收敛 + watchdog；观测 turns 分布 |
| prompt 改动依赖模型遵循度 | 行为不稳定 | prompt 用强制语气 + 清单化；关键项（QC 禁 bash）可加 guardrail 硬保证 |

---

## 7. 涉及文件清单

### 7.1 代码改动

| 文件 | 任务 | 类型 |
|------|------|------|
| `backend/packages/harness/deerflow/agents/middlewares/summarization_middleware.py` | #2-A, #3-A | cooldown/收益门槛/质量门槛 |
| `backend/packages/harness/deerflow/config/*`（SummarizationConfig） | #2-A, #3-A | 新增参数 + 默认值 |
| `backend/packages/harness/deerflow/sandbox/tools.py` | #7-A, #9 | ls 隐藏 + docstring |
| `backend/packages/harness/deerflow/agents/middlewares/todo_middleware.py` | #10 | write_todos 去重 |
| `backend/packages/harness/deerflow/tools/builtins/present_file_tool.py` | #4-C（可选） | 内容去重 |
| `config.yaml`（仓库根，gitignored） | #2-B/C, #5-A | keep/dedup/max_turns |
| `backend/.deer-flow/agents/eligibility-screener/SOUL.md`（gitignored） | #4-A/B, #6-A, #8-A, #9 | prompt 纪律 |
| 相关 skill（`eligibility-judgment` / `screening-report-generator` / `criteria-parser`） | #4, #8 | 交付/输入清单 |
| quality-control / report-writer 子代理 prompt | #5-B, #6-B | 范围/收敛 |

### 7.2 测试新增

| 文件 | 任务 |
|------|------|
| `backend/tests/test_summarization_cooldown.py` | #2-A |
| `backend/tests/test_summarization_quality_gate.py` | #3-A |
| `backend/tests/test_ls_hides_tool_results.py` | #7-A |
| `backend/tests/test_todo_dedup.py` | #10 |
| （可选）`backend/tests/test_present_file_dedup.py` | #4-C |

### 7.3 文档同步（AGENTS.md 政策要求）

- 用户可见行为变化（交付清单、QC 纪律）→ 更新 `README` 相关段落（如涉及）。
- 中间件/config 变化 → 更新 `backend/AGENTS.md` 对应小节。
- 实施后新增 `docs/eligibility-screener-monitoring-optimization-changelog.md` 记录逐文件变更与测试结果（沿用 fix-changelog 体例）。

---

## 8. 决策点（需评审确认）

1. **#2-C / read_file_dedup、search_dedup 是否本轮启用**：建议先上 #2-A+B 重跑，视震荡是否消除再决定（changelog 第 8 章已将其列为"待验证"预留）。
2. **#4-C present_files 工具去重是否实施**：prompt（A/B）能否稳定消除重复；若模型遵循度不足则实施 C。
3. **#6-C guardrail 是否实施**：prompt（A/B）+ 安全路径约束是否足够；QC 仍偶发脚本化则加 guardrail 或收窄 quality-control 子代理 tool_groups。
4. **#7-B/C durable 引用与清理**：是否本轮纳入，还是留待独立运维优化。

> 以上决策点默认按"prompt 优先、可选后端加固按重跑结果决定"推进，除非评审另有意见。
