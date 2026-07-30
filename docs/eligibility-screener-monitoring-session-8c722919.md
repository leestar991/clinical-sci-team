# 会话监控记录：8c722919（eligibility-screener）

> 监控时间：2026-07-14 22:30~23:08（CST）
>
> 会话：`http://localhost:3000/workspace/agents/eligibility-screener/chats/8c722919-c357-4525-aa32-ae0833653844`
>
> thread_id：`8c722919-c357-4525-aa32-ae0833653844`　run_id：`4cbd9d32-4df2-43d4-959f-4d6ae790d292`
>
> 数据来源：`backend/.deer-flow/data/deerflow.db`（runs，**时间戳为 UTC**）、`backend/.deer-flow/checkpoints.db`（checkpoints）。
>
> 状态：**仅观察记录，未修改任何代码/配置**。run 于 22:30 创建，23:08 仍在 `running`（agent 已完成工作但状态未落盘）。

---

## 0. 一句话结论

run 整体执行**正常流畅**：Phase 1 预处理 + Phase 2（16 个 OCR 子代理分批完成，无卡死）+ Phase 2.5（**QC 仅 1 轮即通过，无循环**）全部顺利完成，约 28 分钟。但 agent 在 P2.5 后给出 final message 主动提前结束（未进入 P3-P5），且 **run 状态 10+ 分钟未 transition 到 terminal**（final AIMessage 已产出但 runs 表 stale=22:58:51），疑似 worker 在 checkpoint 持久化或 run 状态更新时卡住。

---

## 1. Run 概览（23:08 时点）

| 字段 | 值 |
|------|-----|
| status | `running`（agent 已完成但未落盘） |
| created_at | 22:30:09 CST |
| updated_at | 22:58:51 CST（10 分钟未更新） |
| 模型 | gpt-5-4（fosunpharma ai-gateway） |
| llm_call_count | 19 |
| total_tokens | 3,200,616 |
| total_input_tokens | 3,105,331（未达 hard_stop 3,600,000） |
| subagent_tokens | （子代理占大头，约 2M+） |
| checkpoint 数 | 376（持续写入，agent 工作正常） |
| delegations | 17 completed, 0 in_progress |

---

## 2. 问题清单

### 🟢 无子代理卡死

16 个 OCR 子代理（general-purpose）+ 1 个 QC 子代理（quality-control）**全部正常完成**，无 in_progress 残留。每批 3 个子代理约 3-6 分钟。与 `20d7e28c` 完成时间一致。

### 🟢 无 QC 循环

P2.5 仅执行了 **1 轮**"标准语义QC"即通过，**无反复 QC**。这是重大改进——对比 `20d7e28c` 第二轮 run 的 5 次 QC 循环。

可能原因：
- 本次 criteria-parser 产出质量较好（同配置、同技能，不同 run 的 LLM 解析结果不同）
- agent 本次未用 bash 脚本修订 criteria_parsed（减少了引入新结构问题的可能）
- 本次仅 1 轮 QC 即通过，恰好落在一个"干净"的解析结果上

### 🟠 P1-1：agent 在 P2.5 后主动提前结束（P3 未执行）

msg[80] AIMessage 内容：
> "本轮任务已推进到'可正式逐条判定'的状态，但由于本次会话被中断，最终逐条判定文件和HTML报告尚未生成完成。"

agent 在 P2.5 完成后给出了 final message（无 tool_calls），未启动 P3（逐条判定）。原因分析：

- **不是 budget hard_stop**：total_input=3.1M < hard_stop=3.6M（4M×0.9）。
- **不是 token_budget 告警**：warn_threshold=0.7 → 2.8M 应告警但 agent 日志中无 budget 相关关键字。
- **"会话被中断"语义不明**：可能是 goal-loop 终止、`TodoMiddleware` 提前退出阻止（todos 中 P3-P5 仍是 pending）、或 agent 自我判断剩余任务无法在合理资源内完成。
- **与 `20d7e28c` 模式相似**：该 run 第二轮也在 P2.5 后提前结束（虽原因不同——budget 硬停 vs 本次 agent 主动）。两例中，P3-P5（逐条判定+QC+报告）均未执行。

### 🟠 P1-2：run 状态 transition 卡住（10+ 分钟未落盘）

final AIMessage 已产出超过 10 分钟，但 runs 表仍 `running`（updated_at=22:58:51）。checkpoint 376 条仍在写入（agent 工作正常），但 run 状态未 transition 到 terminal。

这是与 `d5fe20ec`（最终被 watchdog timeout 终止）同类的 worker 级僵死问题——run 的内部状态已完成，但外层状态未落盘。可能原因：
- checkpoint 持久化成功但 run status 的 `_persist_status` / `set_status` 调用链阻塞
- 或 stream bridge 在 final message 后未正常关闭导致 worker 挂起

### 🟢 无模型长时间不响应

gpt-5-4 调用最长时间约 3-4 分钟，无 600s timeout 触发。所有 LLM 调用正常返回。

### 🟢 无反复执行操作

OCR 分批按 SOUL.md 节奏正常执行。无任何重复调用同一操作（如反复 read_file 同一文件、反复 grep 等）。

### 🟡 P2-3：agent 使用 bash 生成患者索引（合理用法，无违规）

msg[66] AIMessage 包含 `bash:生成患者索引并聚合OCR`——这是 Phase 2.5 的正常步骤（患者索引生成），非 QC 阶段禁用的脚本修订。符合 SOUL.md 预期。

### 🟡 token 消耗与总结

与 `20d7e28c` 相比，本次 token 效率明显改善：

| 指标 | 20d7e28c 第2轮 | 8c722919（本次） |
|------|---------------|-----------------|
| 耗时 | 19min（budget收口） | ~28min |
| tokens | 2.89M | 3.2M |
| llm_calls | 28 | 19 |
| QC 轮次 | 5 轮 | **1 轮** ✅ |
| OCR 子代理 | 18 个 | 16 个 |
| P3-P5 执行 | 未（budget硬停） | 未（agent主动） |

---

## 3. 阶段完成情况

| Phase | 状态 | 产出 |
|-------|------|------|
| 1 预处理 | ✅ | pdf_classification + eligibility_criteria_raw |
| 2 OCR + 解析 | ✅ | criteria_parsed + 16 OCR 分片 |
| 2.5 QC + 患者索引 | ✅ | criteria_qc（1轮通过）+ patient_index |
| 3 匹配分析 | ❌ 未执行 | - |
| 4 QC+理由 | ❌ 未执行 | - |
| 5 报告交付 | ❌ 未执行 | - |

---

## 4. 与前序会话对比

| 维度 | d5fe20ec | 20d7e28c R2 | 8c722919（本次） |
|------|----------|-------------|-----------------|
| 创建 | 18:50 CST | 21:40 CST | 22:30 CST |
| 子代理卡死 | ⚠️ watchdog kill | ✅ 正常 | ✅ 正常 |
| QC 循环 | 未进入 | 🔴 5轮 | 🟢 1轮 |
| P3-P5 | 未 | 未 | 未 |
| terminal | timeout | success(budget) | running(卡住) |
| write_todos | 🔴 失效 | ✅ | ✅ |
| budget 触发 | 未知 | 🔴 hard_stop 1.5M | 🟢 未触发(3.1M<3.6M) |

---

## 5. 待决策

1. **P1-2 worker 僵死**：需确认 run 是否最终 transition 或被 watchdog 终止。如仍 running，建议手动置为 timeout（与 d5fe20ec 相同处理）。
2. **P1-1 agent 提前结束**：agent 为何认为"会话被中断"？需查 final message 上下文与 TodoMiddleware 是否有触发提前退出。P3-P5 连续多个会话未执行，是否存在系统性问题（如 agent 在 P2.5 后普遍提前停止）。
3. **QC 1轮通过**：本次 QC 效率显著改善，但不确定是巧合（好解析结果）还是可复现。后续会话持续观察确认是否系统性问题已收敛。
