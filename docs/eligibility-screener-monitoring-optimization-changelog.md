# eligibility-screener 监控问题优化变更汇总

> 基于 [eligibility-screener-monitoring-optimization-plan.md](./plans/eligibility-screener-monitoring-optimization-plan.md)，实施时间 2026-07-14。
>
> 关联分支：`feature/gpt-team`
>
> 来源：[eligibility-screener-monitoring-issues.md](./eligibility-screener-monitoring-issues.md)（会话 `b729d95e` 实时监控分析，2026-07-13）。
>
> 本轮针对监控发现的 10 项运行质量问题（#1 已修复仅回归验证），按"prompt 优先、可选后端加固按重跑结果决定"分层推进，落地 9 项（#2/#3/#4/#5/#6/#7/#8/#9/#10），可选项（#2-C read_file_dedup、#3-C 强模型、#4-C present_files 工具去重、#6-C guardrail、#7-B/C durable 引用与清理）按计划留待重跑验证后决定。

---

## 1. 变更概览

| 层级 | 文件数 | 新增测试 | 覆盖问题 |
|------|--------|----------|----------|
| 后端 Harness（中间件/配置/子代理） | 6 | 18 | #2/#3/#5/#6/#7/#9/#10 |
| Agent SOUL.md / Skill prompt | 4 | - | #4/#5/#6/#8/#9 |
| 配置（config.yaml / config.example.yaml） | 2 | - | #2/#3/#5 |
| 测试（新增） | 4 | 18 | #2/#3/#7/#10 |

**统计**：12 个源/配置文件，4 个新测试文件，18 个新增测试用例。

---

## 2. 逐文件变更详情

### 2.1 `backend/packages/harness/deerflow/config/summarization_config.py`

**涉及任务**：#2-A, #3-A

`SummarizationConfig` 新增 4 个字段（均带向后兼容默认值）：

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `cooldown_calls` | `3` | 成功压缩后跳过接下来 N 次"应压缩"调用，消除震荡 |
| `min_messages_to_summarize` | `5` | 待压缩消息少于 N 条时跳过本轮（低收益压缩） |
| `min_summary_chars` | `120` | 摘要总长不足 N 字符视为低质量，跳过本轮保留原消息 |
| `min_summary_body_chars` | `40` | 去掉 markdown 标题与字面 "None" 后正文不足 N 字符视为低质量 |

### 2.2 `backend/packages/harness/deerflow/agents/middlewares/summarization_middleware.py`

**涉及任务**：#2-A, #3-A

- `__init__` 新增 4 个守卫参数与 per-(thread_id, run_id) bookkeeping（`_summarize_attempts` / `_has_compacted` / bounded-LRU `_summarize_touch_order`，仿 `TodoMiddleware` 模式，cap 4096）。**`__init__` 默认值均为 0（守卫关闭）以保持向后兼容**（现有单元测试直接构造中间件时不传守卫参数，应保持无守卫的原行为）；生产通过 `SummarizationConfig` 显式开启（config.yaml 设 `cooldown_calls=3` 等，`lead_agent/agent.py` 透传）。
- `_maybe_summarize` / `_amaybe_summarize` 在 `_determine_cutoff_index` 后插入：
  1. **cooldown**：`_cooldown_check_and_tick` -- 首次压缩不节流；成功后接下来 `cooldown_calls` 次应压缩调用跳过。
  2. **最小压缩收益**：`len(messages_to_summarize) < min_messages_to_summarize` 跳过。
  3. **摘要质量校验**：`_is_low_quality_summary(summary)` 为真则跳过本轮、保留原消息、不写 `summary_text`。
- 压缩成功后 `_record_successful_summarization` 重置计数器并标记 `_has_compacted`。
- `_summarize_bookkeeping_key` 容忍 `runtime=None`（部分单元测试直接调 `_maybe_summarize` 不传 runtime），回退到 `("default","default")`。

### 2.3 `backend/packages/harness/deerflow/agents/lead_agent/agent.py`

**涉及任务**：#2-A, #3-A

`_create_summarization_middleware` 构造 `DeerFlowSummarizationMiddleware` 时透传 4 个新 config 字段。

### 2.4 `backend/packages/harness/deerflow/agents/middlewares/todo_middleware.py`

**涉及任务**：#10

- `__init__` 用去重版 `write_todos` 工具替换基类注册的工具。去重工具为**模块级函数** `_dedup_write_todos` / `_adedup_write_todos`（仿 langchain 基类 `_write_todos` 模式），通过 `runtime: ToolRuntime[Any, Any]` 注解接收注入的 runtime。
- 新增 `_build_write_todos_command` / `_todos_equal` / `_current_todos`：新 todos 与 `runtime.state.todos` 完全一致（同内容、同状态、同顺序）时返回幂等 `Command`（仅 `messages`，不更新 `todos`），任一变化正常写入。
- **关键实现细节**：模块顶部 `from __future__ import annotations` 使函数注解存为字符串，而 ToolNode/StructuredTool 的注入检测在某些路径直接读 `__annotations__`（非 `get_type_hints`），字符串化 `ToolRuntime` 不被识别为 injected arg。显式将 `_dedup_write_todos.__annotations__` / `_adedup_write_todos.__annotations__` 覆盖为真实类型对象后注入正常。基类 langchain 工具因模块无 future annotations 不受影响。

### 2.5 `backend/packages/harness/deerflow/sandbox/tools.py`

**涉及任务**：#7-A, #9

- 新增 `_filter_hidden_lines(output, requested_path)`：对 `ls_tool` 的输出按行过滤路径段以 `.` 开头的条目（至少隐藏 `.tool-results`）及整棵子树；`requested_path` basename 以 `.` 开头时（显式列隐藏目录，逃生阀）不过滤。
- `ls_tool` 在 `mask_local_paths_in_output` 之后调用 `_filter_hidden_lines`，全隐藏时返回 `(empty)`。
- `bash_tool` docstring 补"使用 `/mnt/user-data/...` 虚拟路径，禁宿主机绝对路径"。
- `grep_tool` docstring 补"path 应为目录而非单文件，读单文件用 read_file"。

### 2.6 `backend/packages/harness/deerflow/subagents/builtins/quality_control.py` / `report_writer.py`

**涉及任务**：#5-B, #6-B, #6 安全附带

- `quality_control.py` system_prompt 新增 `<qc_discipline>`：明确质控必须 LLM 推理、禁脚本、5 项语义级 QC 范围、分步收敛（按段读/grep 定位、接近轮次上限产出部分结果）、路径纪律（虚拟路径禁宿主机绝对路径）。
- `report_writer.py` system_prompt 新增 `<working_style>`：分步收敛、产出路径规范、result 声明产出文件清单。

### 2.7 `config.yaml`（gitignored，仓库根）

**涉及任务**：#2-B, #2-A, #3-A, #3-B, #5-A

| 配置项 | 旧值 | 新值 | 说明 |
|--------|------|------|------|
| `summarization.keep` | `messages: 30` | `tokens: 25000` | 保留窗口 token-based，与 50k trigger 留稳定缓冲 |
| `summarization.summary_prompt` | `null` | 自定义中文摘要 prompt | 补 task/工具调用消息压缩指导，防首次空摘要 |
| `summarization.cooldown_calls` | _(无)_ | `3` | 新增：cooldown 守卫 |
| `summarization.min_messages_to_summarize` | _(无)_ | `5` | 新增：最小压缩收益门槛 |
| `summarization.min_summary_chars` | _(无)_ | `120` | 新增：摘要质量门槛 |
| `summarization.min_summary_body_chars` | _(无)_ | `40` | 新增：摘要正文门槛 |
| `subagents.agents.quality-control.max_turns` | `100` | `150` | 多患者多文档语义 QC 余量 |
| `subagents.agents.report-writer.max_turns` | `100` | `150` | 报告拼装+多文件读取余量 |

### 2.8 `config.example.yaml`

同步新增 `cooldown_calls` / `min_messages_to_summarize` / `min_summary_chars` / `min_summary_body_chars` 字段及注释；`keep` 段补 token-based 与震荡治理关系说明。

### 2.9 `backend/.deer-flow/agents/eligibility-screener/SOUL.md`（gitignored）

**涉及任务**：#4-A/B, #6-A, #8-A, #9 prompt

新增 4 条强制原则：
- **原则 12 QC 质控纪律**：QC 必须委派 `task(quality-control)` LLM 推理；严禁 QC 阶段用 bash 编写/执行脚本；结构校验仅作前置自动化步骤不计入 QC。
- **原则 13 路径纪律**：一律 `/mnt/user-data/...` 虚拟路径，禁宿主机绝对路径；grep path 为目录；患者路径统一 `workspace/patients/{id}/`。
- **原则 14 输入资料边界**：声明判定阶段唯一判定依据（uploads/*.md、criteria_parsed.json、extraction.json），调度子代理时显式传入输入路径。
- **原则 15 交付文件清单与去重**：必交付 4 项 + 过程文件 2 项（qc_report/reasons）；每文件仅 present 一次；子代理产出移动到 outputs/ 并在 result 声明清单。

同时修正 Phase 4 / Phase 5 的 `present_files`，消除 `criteria_parsed.json` 重复 present，新增 `qc_report_{id}.json` / `reasons_{id}.json` 交付；目录规范补 outputs/ 下过程文件。

### 2.10 `skills/custom/eligibility-judgment/SKILL.md` / `screening-report-generator/SKILL.md`

- `eligibility-judgment`：新增"输入资料"与"交付文件清单"小节；QC 委派模板强化禁脚本纪律。
- `screening-report-generator`：新增"交付文件清单"小节，明确两份 HTML 在 Phase 5 批量 present 不重复。

---

## 3. 新增测试

| 文件 | 用例数 | 覆盖 |
|------|--------|------|
| `backend/tests/test_summarization_cooldown.py` | 9 | #2-A cooldown + 最小压缩收益 + per-run 隔离 + bounded 清理 |
| `backend/tests/test_summarization_quality_gate.py` | 5 | #3-A 全 None/过短/有效/部分 None/不记录成功 |
| `backend/tests/test_ls_hides_tool_results.py` | 5 | #7-A 隐藏 .tool-results/dotfile + 逃生阀 + 普通条目不受影响 |
| `backend/tests/test_todo_dedup.py` | 5 | #10 相同跳过/变化写入/无 state 首写/均空跳过/顺序变化写入 |

运行：`cd backend && make test`；提交前 `cd backend && make lint && make format`。

---

## 4. 未实施项（按计划留待重跑验证）

| 项 | 原因 |
|----|------|
| #2-C `read_file_dedup` / `search_dedup` 启用 | 先上 #2-A+B，重跑观测震荡是否消除再决定 |
| #3-C 首次压缩用更强模型 | 代价高收益边际，A+B 已防空摘要落地 |
| #4-C `present_files` 工具内容去重 | prompt（A/B）能否稳定消除重复，视模型遵循度决定 |
| #6-C QC 阶段 bash guardrail / 收窄 quality-control tool_groups | prompt + 安全路径约束是否足够，QC 仍偶发脚本化再加 |
| #7-B/C durable 引用登记与 .tool-results 清理 | 独立运维优化，按重跑结果评估 |

---

## 5. 风险与回滚

- cooldown 过长致上下文超阈值堆积：`cooldown_calls=3` 配 token_budget 硬停兜底；可按重跑数据下调。
- 摘要质量门槛误判正常短摘要：阈值保守（120/40 chars），仅拦全 None/极短，配单测边界用例。
- ls 过滤 dotfile 误藏合法 dotfile：仅隐藏顶层 dotfile，保留显式列隐藏目录逃生阀。
- `write_todos` 去重误判：仅完全一致（内容+状态+顺序）才跳过，任一变化正常写入；幂等 ToolMessage 提示模型。
- max_turns 上调掩盖低效循环：配合 prompt 分步收敛 + watchdog，观测 turns 分布。

回滚：所有后端守卫均有 config 开关或默认值；cooldown/min_messages 设 0、min_summary_chars 设 0 即关闭守卫；prompt 改动可热更新；ls 过滤与 todo 去重为代码改动，需 git revert。

---

## 6. 回退修订（2026-07-14，会话 d5fe20ec 监控后）

监控会话 `d5fe20ec`（见 [eligibility-screener-monitoring-session-d5fe20ec.md](./eligibility-screener-monitoring-session-d5fe20ec.md)）暴露 #10 的生产回归，并经 token 估算验证澄清 #2/#3 与 summarize 未生效无关，但 #3 存在理论风险。按"优先回退未经充分生产验证的改动"原则执行：

### 6.1 #10 write_todos 去重 - 代码回退

- **根因**：`_adedup_write_todos` 在生产 **async 执行路径**下 `ToolRuntime` 注入失败（`missing 1 required positional argument: 'runtime'`），write_todos 工具完全失效，todos 永不写入。sync `graph.invoke` 单测通过掩盖了 async 路径缺陷。
- **处置**：`git checkout HEAD -- todo_middleware.py` 恢复基类 write_todos + 删除 `test_todo_dedup.py`。TodoMiddleware 恢复原始（仅上下文丢失检测 + 提前退出阻止）。
- **验证**：`test_todo_middleware.py` 全绿。

### 6.2 #2/#3 守卫 - config 关闭（代码保留）

- **关联澄清**：`count_tokens_approximately` 估算该 run messages 总量=15285 < trigger 50000，`_should_summarize` 两路径均不触发，守卫无机会生效。**#2/#3 与本会话 summarize 未生效无关**。
- ** precautionary 回退理由**：#3 质量门槛存在理论风险--若 summarize 触发且摘要模型持续返回低质量摘要，会反复跳过压缩形成"永不压缩 + 重复浪费"死循环（`_has_compacted` 保持 False 使 cooldown 也不生效）。
- **处置**：`config.yaml` / `config.example.yaml` 4 项守卫全设 0（`cooldown_calls`/`min_messages_to_summarize`/`min_summary_chars`/`min_summary_body_chars`）。`SummarizationConfig` 字段 + 中间件 `__init__` 保留（默认即 0），待独立验证（含 async 生产路径 + 真实摘要质量观测）后重新启用。
- **保留**：#2-B keep(tokens 25000)、#3-B summary_prompt（无害改进）。

### 6.3 保留项

#5-A max_turns、#7-A ls 隐藏、#4/#6/#8/#9 prompt 改动均保留--经评估与本会话问题无关且无害。

### 6.4 待办（预存问题，非上一轮改动引入）

- **P0-2 子代理卡死**：gpt-5-4 `max_retries=5`×600s 放大卡死 + subagent timeout 与 run watchdog 耦合（子代理 1800s 永远先被 run 600s 触发）。方案见监控文档 §4.2。
- **P0-3 watchdog 触发延迟**：run 级 inactivity watchdog **已生效**（11:22 将 run 置 timeout，初版"僵死8小时/watchdog失效"判断经 DB 复核修正），但触发延迟 600s->1920s（`activity_event` 被子代理后台零星事件误重置）。方案见监控文档 §4.3。
- **释放僵死 run**：**不需要**。run 已被 watchdog 自动终止，thread 已可用（无 inflight 残留）。
