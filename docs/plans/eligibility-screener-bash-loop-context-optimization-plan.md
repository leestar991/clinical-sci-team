# Eligibility Screener 循环保护误报 + 上下文膨胀 + 连接中断优化计划

> 基于会话 `2812eaf8-e337-44a8-8edf-5a221d8d1c7b`，Run `76a820a1-b0ce-49c9-8f79-bec66c862aec`（2026-07-17）的根因分析。
> 与既有 [`docs/token-budget-optimization.md`](../token-budget-optimization.md)（会话 `9e958425`，hash 硬停 + token 预算硬停）是**不同的故障签名**。

## 会话概要

| 项 | 值 |
|----|----|
| 模型 | `gpt-5-4` |
| Run 数 | 1（单次 run） |
| 最终状态 | `error`，错误信息 `Connection error.` |
| 规模 | 55 次 LLM 调用、166 条消息、4,532,811 tokens（input 4,403,858） |
| 子代理 | 15 个 |
| 结果 | 触发 `LoopDetectionMiddleware` 第 2 层（bash 频次告警），未进入逐条入排判定与 HTML 报告阶段 |

## 与既有 `token-budget-optimization.md` 的区别

| 维度 | 既有文档（会话 `9e958425`） | 本次（会话 `2812eaf8`） |
|------|------------------------------|--------------------------|
| 循环保护触发层 | 第 1 层 hash 硬停（相同工具集合 ≥5 次） | 第 2 层 bash 频次**告警**（warn=15） |
| 结束原因 | `TokenBudgetMiddleware` 输入 token 硬停（4.5M ≥ 4.5M） | provider `Connection error.`（非预算硬停） |
| Goal | 有 goal continuation 放大循环 | 无 goal（`metadata_json = {}`） |
| Token 主因 | OCR 图片 base64（~69%） | 结构性重发历史 + 只读大 blob 反复读入 |
| 子代理 | OCR 子代理为主 | 混合子代理，其中一次超时 |

## 本次根因（已从 run 数据复现）

1. **bash 频次告警阈值过低**：`loop_detection.tool_freq_overrides.bash.warn=15`。工作流大量用 `bash python3 heredoc` 做预处理/拷贝/汇总，第 15 次 bash（seq 298）即触发告警 → seq 301 模型停手汇报。read_file（34 次）因 override `warn=80` 未触发；hash 层（Layer 1）最多重复 2 次，未达 `warn=3`。
2. **只读大输入被反复读取**：`试验方案.md` 读 7 次、`criteria_qc.json` 3 次，反复 ls/grep/bash 汇总同一批 OCR 产物。大 blob（方案 JSON 23KB、OCR 转储 ~20KB、grep 143 命中 18KB、SKILL.md 12KB×2）长期驻留上下文。
3. **上下文膨胀是结构性的**：每次 LLM 调用重发全量历史，lead 上下文稳定在 ~76k/次 × 39 次 + 15 个子代理 = 4.5M。归因：lead 2.56M / subagent 1.93M / middleware 43.6k。summarize 已运行 15 次，只能防止无限增长，防不了"每次重付"。
4. **Prompt 缓存反复击穿**：lead 仅约 50% 命中缓存（fresh 非缓存 1.25M / 2.49M）。历史被中途改写（循环告警注入 / summarize 重写 / 工具调用恢复占位）使前缀缓存失效，整段 ~76k 重算（call 8/14/19/33/42/43 出现 `cache_read=0`）。
5. **子代理超时放大循环**：seq 345/350 记录一次子代理"300 秒无流进度"超时，迫使 lead 回退重查、重跑 bash。
6. **连接韧性缺失**：末尾连续 6 次 `Connection error.`（seq 351–356，usage 为空未计 token）直接把 run 打成 error，无有效重连/降级恢复。

## 优化任务分解（按命中根因优先级）

### Task 1：靶向调整 bash 频次阈值（config，命中根因 #1）
- 在 `config.yaml` 的 `loop_detection.tool_freq_overrides.bash` 将 `warn: 15 → 40`、`hard_limit: 30 → 80`。
- 测试：更新 `backend/tests/test_loop_detection_config.py` 断言解析值；回放本 run 的 bash 序列（18 次）验证不再在 15 次误触发。

### Task 5：启用子代理级 token 预算（config，命中根因 #3）
- 在 `config.yaml` 启用 `subagents.token_budget`。
- **偏差说明**：计划初稿的 `max_input_tokens: 80000` 与仓库既有注释（config.yaml:190，"单轮 OCR 3 张图片易超 80K"）冲突，会误杀合法 OCR 子代理；实现采用仓库推荐的 `max_input_tokens: 150000` / `max_tokens: 200000` / `hard_stop_threshold: 0.9`，仍可拦截失控子代理（本 run 子代理均值 ~129K），同时不破坏 OCR。
- 测试：配置测试断言预算解析生效。

### Task 2：只读输入一次性固化引用（SOUL/skill，命中根因 #2）
- 修改 `eligibility-screener/SOUL.md`：只读大输入"读一次 → 写入工作区索引/摘要 → 后续引用摘要而非重复 read_file/整块 dump"；grep 限制返回条数、优先分页读。
- 测试：`tests/skills/` 增加断言校验 SOUL 含"禁止重复读取同一只读文件"指令与摘要引用流程。

### Task 3：子代理超时改为可恢复分支（代码，命中根因 #5）
- 子代理"300s 无流进度"超时后，让 lead 走结构化恢复（记录失败/缩小任务再派发一次/降级直接处理），而非全量重查。对齐 [`docs/plans/subagent-timeout-watchdog-optimization-plan.md`](./subagent-timeout-watchdog-optimization-plan.md)。
- 测试：`backend/tests/test_subagent_executor.py` 增加超时→恢复路径用例。

### Task 6：连接错误韧性（代码，命中根因 #6）
- 为 provider `Connection error.` 增加有限次指数退避重连，或多次失败后保存已完成产物并优雅结束。
- 测试：`backend/tests/test_llm_error_handling_middleware.py` 增加连续连接错误→退避→优雅降级用例。

### Task 4：保护 prompt 缓存前缀 / 减少历史中途改写（代码，命中根因 #4）
- 审查循环告警注入、summarize 重写、工具调用恢复三处历史修改，尽量"追加尾部"替代"改写前缀"。
- 测试：新增单测断言这些中间件不修改历史前缀（仅追加）。

### Task 7：整合验证与文档回填
- `cd backend && make test` 全绿；`make format` 通过。更新相关 README/AGENTS.md 与本文档落地记录。

## 执行顺序

Task 1 → Task 5 → Task 2 → Task 3 → Task 6 → Task 4 → Task 7（config 级止血优先，代码/SOUL 治本随后）。每个 Task 遵循仓库 TDD 要求（先写/更新测试，再改实现）。

## 落地记录

- 2026-07-17：创建本计划文档，开始按序落地。
- 2026-07-17：Task 1-6 全部落地，测试通过（`uv run pytest`）。

### 各任务落地结果

| Task | 状态 | 改动 | 测试 |
|------|------|------|------|
| Task 1 | ✅ | `config.yaml` `loop_detection.tool_freq_overrides.bash` warn 15→40、hard 30→80（含事故根因注释） | `test_loop_detection_config.py` + `test_loop_detection_middleware.py` 新增 3 用例，81 passed |
| Task 5 | ✅ | `config.yaml` 启用 `subagents.token_budget` | `test_subagent_timeout_config.py` 新增 `TestSubagentsTokenBudget`，59 passed |
| Task 2 | ✅ | `SOUL.md` Principle 8 增补只读输入固化引用 / grep 限量 / bash 合并 | 见下方偏差说明 |
| Task 3 | ✅ | `task_tool.py` 超时返回结构化恢复指引 + 抢救部分产物（`build_subagent_timeout_message` / `_extract_partial_result`） | `test_task_tool_core_logic.py` 新增 `TestSubagentTimeoutMessage`（9 用例） |
| Task 6 | ✅ | `llm_error_handling_middleware.py` 连接错误韧性（`_CONNECTION_PATTERNS` + 连接类名 → transient 重试） | `test_llm_error_handling_middleware.py` 新增连接韧性用例，39 passed |
| Task 4 | ✅ | 锁定循环告警注入为“仅追加尾部”（保护 prompt 缓存前缀） | `test_loop_detection_middleware.py` 新增 `TestPromptCachePrefixSafety`（3 用例），75 passed |

汇总：所有涉及文件的测试联合运行 **286 passed**（相关外围 101 passed，无回归）。`ruff format` / `ruff check --fix` 已对改动文件执行通过。

### 偏差与范围说明（重要）

1. **Task 5 预算取值**：计划初稿的 `max_input_tokens: 80000` 与仓库既有注释（`config.yaml`：单轮 OCR 3 张图片易超 80K）冲突，会误杀合法 OCR 子代理。实现采用仓库推荐的 `max_input_tokens: 150000` / `max_tokens: 200000`，仍拦截失控子代理（本 run 子代理均值 ~129K）且不破坏 OCR。
2. **Task 2 无 CI 测试**：`eligibility-screener/SOUL.md` 位于 `.deer-flow/agents/`（`git check-ignore` 确认为运行态、gitignored、无提交源），且 `tests/skills/` 仅测已提交的 public skills。对 gitignored 文件加 CI 断言会在全新 checkout 上失败，故改为读回校验（已确认三处新增指令落地）。
3. **Task 3 helper 归属**：`conftest` 全局 mock 了 `deerflow.subagents.executor`，若把 helper 放在 executor 会在 `test_task_tool_core_logic.py` 的 mock 用例里变成 MagicMock，故 helper 定义在 `task_tool.py`（其本就是该返回串的归属）。保留 `Task timed out. Error:` 前缀，`status_contract`→timed_out 与 `delegation_ledger` 均不受影响。
4. **Task 4 范围**：仅循环告警注入可做“仅追加”并已锁定；summarize（压缩前缀）与悬空工具调用恢复（中途插入 tool 消息）出于 provider 正确性必须改写历史，不在本次“仅追加”约束内。
5. **Task 6 附带修复**：新增 `event loop is closed` transient 模式，顺带修复了 3 个既有失败用例（`test_classify_error_event_loop_closed_*` / `test_async_event_loop_closed_triggers_retry_then_succeeds`）。
6. **既有失败（非本次回归）**：`test_task_tool_core_logic.py` 中 `test_task_tool_returns_partial_result_on_timeout`、`test_task_tool_no_partial_result_when_no_messages`、`test_cleanup_called_on_failed` 在 baseline（改动前）即失败，原因是缺少未合并特性的符号 `SUBAGENT_MAX_RETRIES` / `get_skills_prompt_section`，与本次改动无关（已用 `git stash` 基线复核）。

