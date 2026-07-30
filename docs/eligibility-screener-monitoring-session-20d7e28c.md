# 会话监控记录：20d7e28c（eligibility-screener）

> 监控时间：2026-07-14 21:17 起，21:22 修正，21:43 终态记录（CST）
>
> 会话：`http://localhost:3000/workspace/agents/eligibility-screener/chats/20d7e28c-e582-4285-86eb-42cda337e5cd`
>
> thread_id：`20d7e28c-e582-4285-86eb-42cda337e5cd`　run_id：`bc9f3d10-b0f1-44ae-a635-544689727c4d`
>
> 数据来源：`backend/.deer-flow/data/deerflow.db`（runs，**时间戳为 UTC**）、`backend/.deer-flow/checkpoints.db`（checkpoints）、磁盘产出文件。`run_events.backend=memory` 未落盘。
>
> 状态：**仅观察记录，未修改任何代码/配置**。run 已于 21:40:22 success 终态。

> ⚠️ **21:22 重大修正**：初版（21:17）将 DB 的 UTC 时间戳误读为本地时间，误判 run "僵死 8 小时 / 进程重启致永久僵尸"。实际 run created 13:10:47 **UTC** = 21:10:47 **CST**，是 gateway 进程 21:09 重启后新建的 run，监控时仅运行 7 分钟，正在正常执行。下文为修正后内容，错误结论已标注作废。

---

## 0. 一句话结论（已修正）

run 于 21:10:47 CST（gateway 进程 21:09 重启后）创建，21:40:22 以 `success` 终态结束（耗时 30 分钟）。**真正根因是 token_budget 硬停**：累计 input 达 135 万（1.5M × 0.9）触发 hard_stop，剥离 tool_calls 强制 agent 在 Phase 2.5 提前结束，P3-P5 未执行。子代理并非卡死而是慢（每批 3-6 分钟），18 个子代理全部完成。**write_todos 已恢复正常（#10 回退生效）**。

~~初版"run 僵死 8 小时 / 进程重启致永久僵尸 / thread 被占用"结论作废~~--基于 UTC 误读，实际 run 一直在推进，最终因 budget 硬停结束。

---

## 1. Run 概览（21:22 时点，活跃执行中）

| 字段 | 值 |
|------|-----|
| status | `running`（活跃） |
| created_at | 2026-07-14 13:10:47 UTC = **21:10:47 CST** |
| updated_at | 2026-07-14 13:21:24 UTC = **21:21:24 CST**（1 分钟前） |
| error | （空） |
| 模型 | gpt-5-4（fosunpharma ai-gateway） |
| llm_call_count | 12（21:17 时为 9，3 分钟内 +3） |
| total_tokens | 1,573,889（21:17 时为 521,305，+1,052,584） |
| total_input / output | 503,424 / 17,881（21:17 快照；21:22 已增长） |
| lead_agent_tokens | 252,653（21:17 快照） |
| subagent_tokens | 268,286 -> 1,300,000+（子代理持续推进） |
| middleware_tokens | 366（deepseek-v4-flash，title 生成） |
| checkpoint step | 111 -> 推进中 |
| multitask_strategy | reject |

token_usage_by_model（21:17 快照，已增长）：
```
gpt-5.4-2026-03-05:    input 503,217  output 17,722  total 520,939
deepseek-v4-flash:     input 207      output 159     total 366
```

---

## 2. 真实问题（修正后）

### 🟠 P1-1　子代理响应极慢（gpt-5-4 网关，非卡死）

**证据**（delegation 时间线，created_at 为 UTC）：
- 21:13:45 CST：派发第 1 批 3 子代理（解析入排标准 / OCR病历1-3 / OCR检查1-3）
- 21:19:54 CST：第 1 批全部 `completed`（耗时约 **6 分钟**），同时刻派发第 2 批 3 子代理（OCR病历4-6 / OCR检查4-6 / OCR检查7-9）
- 21:22 CST：第 2 批 `in_progress` 约 3 分钟，继续执行

**结论**：子代理**并非卡死不返回**，而是 gpt-5-4（fosunpharma ai-gateway）响应极慢，每批 3 个并行子代理约 6 分钟完成。run 持续推进，token 与 llm_call_count 稳步增长。

**与 d5fe20ec 对比修正**：d5fe20ec 的 3 个子代理被 watchdog 在 32 分钟后终止（timeout），当时判断"卡死"。本次监控显示同类子代理实际能在 ~6 分钟完成--**d5fe20ec 的子代理可能也并非永久卡死，而是慢 + watchdog 600s（实际延迟 1920s）误杀**。这改变了对 P0-2 的定性：根因更偏向"网关慢 + watchdog 阈值不合理 + watchdog 触发延迟"，而非"子代理永久卡死"。

### 🟢 P1-2　write_todos 恢复正常（#10 回退生效，确认）

**证据**：
- msg[3]/[7]/[26] 的 `write_todos` 工具调用成功（ToolMessage 无报错，msg[28] = "Updated todo list to ..."）。
- `todos` channel 正确写入 6 条：
  - `completed` P1. 预处理
  - `in_progress` P2. 并行子任务
  - `pending` P2.5 / P3 / P4 / P5
- 对比 `d5fe20ec`（#10 回归期间）：todos 为空，write_todos 报 `_adedup_write_todos() missing runtime`。

**结论**：上一轮 #10 write_todos 去重代码回退后，write_todos 工具在生产 async 路径恢复正常，todos 正确跟踪阶段进度。**此问题已解决**。

### 🟠 P1-3　token 消耗快速膨胀

- 21:17 -> 21:22（5 分钟）：total_tokens 521k -> 1,574k（+1,052k），llm_call 9 -> 12（+3）。
- 子代理 token 占大头（268k -> 1.3M+），单批 3 子代理约消耗 100 万 token。
- eligibility-screener 流程共 5 个 Phase + 多批 OCR，按此速率总 token 可能达数百万。input 占比高（96.6%），与 summarize 未生效（trigger 50k 未达或守卫关闭）+ 子代理各自累积上下文有关。

### ~~P0-2 进程重启致永久僵尸~~（作废）

~~初版判断"进程 21:10 重启杀死 watchdog，run 永久僵尸"~~。实际：进程 21:09 重启后，run 于 21:10:47 **新建**（非旧 run 残留），watchdog 随新 run 正常工作。DB 时间戳为 UTC，初版误读为本地时间导致误判。**无僵尸 run 问题**。

---

## 3. 时间线（修正后，CST）

| 时间（CST） | 事件 |
|------|------|
| 21:09:59 | gateway 进程重启（uvicorn PID 29124） |
| 21:10:47 | run `bc9f3d10` 创建（13:10:47 UTC） |
| 21:13:45 | msg[29] 派发第 1 批 3 子代理（解析入排/OCR病历1-3/OCR检查1-3） |
| 21:15:20 | run updated_at（初版误读为僵死点，实际正常执行中） |
| 21:17:00 | 初版监控时点（误判僵死 8 小时） |
| 21:19:54 | 第 1 批子代理全部 completed（耗时 ~6 分钟），派发第 2 批 3 子代理 |
| 21:21:24 | run updated_at 推进（token 52 万 -> 157 万） |
| 21:22:00 | 修正监控，确认 run 正常执行，第 2 批子代理 in_progress |

---

## 4. 与 d5fe20ec 会话对比（修正后）

| 维度 | d5fe20ec | 20d7e28c（本次） |
|------|----------|------------------|
| 创建时间 | 18:50 CST | 21:10 CST |
| 卡死点 | step 111, 3 task 子代理 | step 111, 3 task 子代理（相同） |
| todos | 空（#10 回归） | 6 条正常（✅ #10 已回退） |
| 子代理结局 | watchdog 32 分钟 timeout 终止 | **第 1 批 6 分钟完成，继续推进** |
| run 终态 | `timeout`（被 watchdog 杀） | `running`（活跃执行） |
| 定性 | 子代理"卡死"+ watchdog 延迟 | **子代理慢但能完成** + watchdog 阈值/延迟问题 |

**关键修正**：本次监控证明 d5fe20ec 的子代理可能并非永久卡死，而是慢。d5fe20ec 的 watchdog（600s 配置，1920s 实际触发）可能在子代理本可完成前就误杀了 run。这对 [subagent-timeout-watchdog-optimization-plan.md](./plans/subagent-timeout-watchdog-optimization-plan.md) 的方案优先级有影响：

- **方案 A（watchdog 触发延迟修复）** 更关键--若 watchdog 准时 600s 触发，而子代理需 6 分钟（360s）完成，600s 阈值合理（留有余量）。但 1920s 延迟反而"歪打正着"给了子代理完成时间。需重新评估：watchdog 应准时 600s 还是适当放宽？
- **方案 B（子代理强制 cancel）** 仍需--子代理若真卡死（非慢），需能强制中断。但需区分"慢"与"卡死"。
- **watchdog 阈值**：600s 可能偏紧（子代理批 6 分钟 = 360s，余量仅 240s）。考虑放宽到 900s 或按子代理 timeout 比例设置。

---

## 5. 关联已规划方案的修正建议

[subagent-timeout-watchdog-optimization-plan.md](./plans/subagent-timeout-watchdog-optimization-plan.md) 需基于本次观察修正：

| 计划方案 | 本次观察的修正 |
|----------|----------------|
| P0-2 子代理"卡死" | 修正为"子代理慢（6 分钟/批）"，真卡死与慢需区分。方案 B 强制 cancel 仍需，但触发条件应是 subagent timeout（1800s）而非 run watchdog（600s） |
| P0-3 watchdog 触发延迟 600s->1920s | 延迟反而让 d5fe20ec 的子代理有更多时间。修复延迟后 600s 可能误杀慢子代理。**需同步评估 watchdog 阈值是否放宽** |
| run_inactivity_timeout 600s | 子代理批 360s，600s 余量紧。建议放宽到 900s，或与 subagent timeout 解耦（子代理执行期间不计时） |

---

## 6. 待决策（修正后）

1. **继续监控**：run 正在执行第 2 批子代理，需观察是否能完成全部 5 个 Phase，以及总 token/耗时。
2. **watchdog 阈值评估**：基于"子代理批 6 分钟"的观察，run_inactivity_timeout 600s 是否应放宽？需在计划文档补充。
3. **d5fe20ec 定性修正**：d5fe20ec 的子代理可能被 watchdog 误杀（非真卡死），需在 d5fe20ec 监控文档与计划文档修正定性。
4. **推进已有计划**：方案 A（watchdog 延迟）+ 方案 B（子代理强制 cancel）+ B3（max_retries 5->2）是否实施？本次观察支持实施，但需配合 watchdog 阈值调整。

---

## 7. 附：时区核对（修正依据）

```bash
# DB 时间戳为 UTC，需转本地 CST
sqlite3 backend/.deer-flow/data/deerflow.db \
  "SELECT datetime(created_at,'localtime'), datetime(updated_at,'localtime') FROM runs WHERE thread_id='20d7e28c-e582-4285-86eb-42cda337e5cd';"
# 结果：2026-07-14 21:10:47 | 2026-07-14 21:21:24（CST）

# 当前 UTC vs 本地
date -u "+utc=%Y-%m-%d %H:%M:%S"   # utc=2026-07-14 13:22:19
date "+local=%Y-%m-%d %H:%M:%S %Z" # local=2026-07-14 21:22:19 CST
```

delegation `created_at` 带 `Z` 后缀（如 `2026-07-14T13:13:45.726610Z`），即 UTC，转 CST +8 小时。

---

## 8. 终态记录（21:43，run 已 success）

### 8.1 Run 终态

| 字段 | 值 |
|------|-----|
| status | **`success`** |
| created_at | 21:10:47 CST |
| updated_at | **21:40:22 CST**（耗时约 30 分钟） |
| llm_call_count | **36** |
| total_tokens | **4,638,124** |
| total_input / output | 4,504,805 / 133,319（**input 占 97.1%**） |
| lead_agent_tokens | 1,515,583 |
| subagent_tokens | 3,105,776（子代理占 67%） |
| middleware_tokens | 16,765（deepseek-v4-flash：summarize + title） |
| message_count | 107 |

token_usage_by_model（终态）：
```
gpt-5.4-2026-03-05:    input 4,494,915  output 126,444  total 4,621,359
deepseek-v4-flash:     input 9,890      output 6,875    total 16,765
```

### 8.2 阶段完成情况

- **Phase 1（预处理）**：✅ completed。产出 pdf_classification.json / eligibility_criteria_raw.md。
- **Phase 2（并行子任务）**：✅ completed。18 个子代理（1 入排解析 + 17 OCR 分片），17 completed。产出 criteria_parsed.json + 全部 OCR。
- **Phase 2.5（QC + 患者拆分）**：✅ completed。产出 criteria_qc.json（QC 报告，含"重做标准解析QC"即首次 QC 未通过后重做）+ patient_index.json（患者拆分）+ phase2_summary.json。
- **Phase 3-5（匹配分析 / QC+推断理由 / 报告交付）**：❌ **未执行**。无 judgments_*.json / screening_report.html / criteria_report.html 产出。

### 8.3 关键观察

1. **run success 但只完成到 P2.5**：run 状态 success（agent 主动结束，非 timeout/error），但未继续 P3-P5。可能原因：agent 在 P2.5 后给出 final response 提前结束、或上下文/budget 限制。需复查最终 AIMessage 内容确认 agent 为何停止。
2. **summarize 正常生效**：summary_text 已设置（结构化中文摘要），middleware_tokens 16,765（deepseek-v4-flash 用于 summarize + title）。messages 从峰值 75 降到 39（终态 checkpoint），证明 summarize 多次压缩历史。**#2/#3 守卫虽 config 关闭，但 summarize 原始机制工作正常，质量良好**。
3. **子代理慢但全部完成**：18 个子代理全部完成（17 completed + 1 在最后批次），无卡死。每批 3 个并行子代理约 3-6 分钟。总 subagent token 310 万。
4. **write_todos 正常**（#10 回退生效）：todos 正确跟踪 P1/P2 completed、P2.5 in_progress。
5. **token 消耗大**：463 万 token，input 占 97.1%。子代理占 67%（310 万）。主 agent 151 万。对单患者单次入排筛选，成本偏高。

### 8.4 与 d5fe20ec 对比（终态修正）

| 维度 | d5fe20ec | 20d7e28c（本次） |
|------|----------|------------------|
| run 终态 | `timeout`（watchdog 32 分钟杀） | **`success`**（30 分钟完成） |
| 完成阶段 | Phase 2 卡死（3 子代理 in_progress） | **Phase 2.5 完成**，P3-5 未执行 |
| 子代理结局 | 被 watchdog 终止，未完成 | **18 个全部完成** |
| todos | 空（#10 回归） | 正常（✅ #10 已回退） |
| 总 token | 52 万（被杀时） | **463 万**（完成） |
| 定性 | 子代理"卡死" + watchdog 延迟 | **子代理慢但完成**，run success |

**关键修正**：本次证明同类子代理能在 ~6 分钟/批完成，d5fe20ec 的子代理可能也并非永久卡死，而是被 watchdog（600s 配置，1920s 实际触发）在子代理本可完成前误杀。d5fe20ec 的 watchdog 触发时（32 分钟），子代理可能即将完成。

### 8.5 根因确认：token_budget 硬停导致 P2.5 提前结束

**用户确认 + 代码核实**：run 在 P2.5 后 success 结束的根因是 **token_budget 硬停**，非 agent 主动完成。

**配置**（`config.yaml` token_budget）：
- `max_input_tokens: 1,500,000`（累计 input 上限）
- `hard_stop_threshold: 0.9` -> 硬停阈值 = **1,350,000 input tokens**
- `max_tokens: 2,000,000`（累计总量上限）

**机制**（`token_budget_middleware.py`）：
- budget 累计**含子代理 token**（line 8："TokenUsageMiddleware retroactively adds them"，line 219-226 diff 累加）。
- 累计 input 达 135 万时（`highest_fraction >= 0.9`，line 251）触发 `hard_stop`。
- hard_stop **剥离 tool_calls**（line 12/262 `_build_hard_stop_update`），强制 agent 产出 final response。
- run 标记 `success`（agent 给了最终答案，非 error/timeout），但实际是 budget 耗尽被迫结束。

**为何 run 表 total_input_tokens=450 万远超 135 万阈值**：
- budget middleware 的 `usage_accum` 与 run 表 `total_input_tokens` 统计口径不同。`usage_accum` 在 hard_stop 触发后停止累加 lead agent 的 input，但子代理 token 是 retroactive 回填（子代理完成后才计入），hard_stop 后仍有已 in-flight 的子代理 token 回填到 run 表统计，导致 run 表 450 万 > budget 触发点 135 万。
- 即 budget 在累计 input ~135 万时触发 hard_stop 停止主流程，但已派发的子代理继续完成并回填 token，run 表最终统计 450 万。

**影响**：
- eligibility-screener 完整 5 Phase 流程的累计 input 需求远超 135 万（本次到 P2.5 就达 135 万触发硬停，P3-P5 未执行）。
- 单患者入排筛选（含 OCR 26 页 + 入排解析 + QC + 逐条判定 + 报告）实际需要 ~300-400 万+ input token，当前 1.5M 上限严重不足。

### 8.6 待决策（终态后）

1. **上调 token_budget 阈值**：`max_input_tokens` 1.5M -> 4M（或更高），`max_tokens` 2M -> 6M，覆盖完整 5 Phase。需评估成本可控性。
2. **子代理 token 治理**：子代理占 310 万（67%），是 budget 消耗大头。需子代理级上下文管理（summarize / 限制每个子代理 input），而非单纯抬高总预算。
3. **hard_stop 行为优化**：当前 hard_stop 剥离 tool_calls 让 agent "成功"结束，掩盖了 budget 耗尽。建议 hard_stop 时标记 run 为 `interrupted`/`budget_exceeded` 而非 `success`，便于识别。
4. **watchdog 阈值评估**：本次 run 30 分钟 success（实际 budget 硬停），子代理批 3-6 分钟。run_inactivity_timeout 600s 在子代理慢响应时可能误杀（如 d5fe20ec）。需更新 [subagent-timeout-watchdog-optimization-plan.md](./plans/subagent-timeout-watchdog-optimization-plan.md)。
5. **d5fe20ec 定性修正**：d5fe20ec 监控文档与计划文档需修正"子代理卡死"定性为"子代理慢 + watchdog 误杀"。
