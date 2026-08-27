# 并行度优化计划：零重叠 → 打满 3 并发

> 触发会话：`b1510d50-480d-4ff9-b344-af06556b8a05`（eligibility-screener，2026-08-20）
> 计划日期：2026-08-24
> 关联：`2026-08-24-b1510d50-session-analysis-methodology.md`（耗时账与零并行度判据）

## Problem Statement

会话 b1510d50 的时间分布：

| 指标 | 数值 |
|------|------|
| 活跃时间 | 7,648s（127.5 分钟） |
| Lead LLM 忙 | 2,552s |
| 子代理忙 | 4,857s |
| **Lead/子代理重叠** | **0.0s** |
| 未计入（IO/等待） | 58,064s（含 17.8h 人工思考） |

`subagent_busy ≈ 活跃 − lead_busy`（4,857 ≈ 7,648 − 2,552）与「31 个子任务
**完全串行**执行」数值吻合。判定/QC 阶段的任务矩阵是 `患者 × {IN,EX} × 批次`，
彼此独立，理论上可 3 路并发——但实测一台机器只跑了一个子代理。

## 框架层已具备的并行能力（已核实，无需新造）

逐一核实了执行链，**并行度的缺失不在框架，而在模型调度行为**：

1. **LangGraph `Send` fan-out**：`factory.py` 的 `_make_model_to_tools_edge` 对
   每个 tool_call 发一个 `Send("tools", ...)` —— 一轮 AI 回复里发 3 个 `task` 调用，
   就是 3 条并行执行 lane。
2. **`task_tool` 后台执行**：`execute_async` 把子代理放后台，工具自身在 backend
   以 5s 间隔轮询到子代理终态才返回。阻塞的是**自己那条 lane**，不阻塞其他 lane
   —— 3 个 task 同轮 = 3 个后台子代理同时跑。
3. **`SubagentLimitMiddleware`**：每轮保留前 3 个 `task` 调用（`max_concurrent=3`），
   **超发静默丢弃**（不是排队）。
4. **SOUL.md 指引已在场**：`backend/.deer-flow/agents/eligibility-screener/SOUL.md`
   核心原则 1「最大并行（打满预算，禁止超发）」——每批 3 个、滑动窗口补派、非 task
   工具不占预算。文件 2026-08-20 20:10 更新，会话主要运行（22:20 起）在其后。
   判定侧的批次拆分见 `judge-delegation.md`「批次拆分」（每轨 12 条一批）。

**结论**：三层能力齐备、指引在场，会话仍然串行 → 问题在**模型的派发习惯**。

## 串行根因假设（用 Task 1 的测量证伪/证实）

| 假设 | 内容 | 验证判据 |
|------|------|---------|
| A 模型不批量 | 每轮只发 1 个 task，等返回后再决策下一个 | 每轮 task 调用数分布集中在 1 |
| B 阶段 barrier | 模型把 P3 判定→P4 QC→P4.5 改判的阶段切换当成全局屏障 | 无跨阶段扇出轮 |
| C 结果注意力劫持 | task 返回长结果后模型先处理结果（读报告/改判），漏掉「先补派下一批」 | 上一批第 1 个返回后直到全部返回前无新 task 调用 |

## 方案

### Task 1 基线结果（2026-08-24 实测 b1510d50，已落地）

`analyze_run_timing.py` 新增「调度模式」段后对 b1510d50 重跑：

```
task dispatch（每轮 AI 回复打包的 task 数）
    dispatching replies  19   1-task=7  2-tasks=9  3-tasks=3  4+=0
subagent concurrency（实测在途子代理数）
    max_concurrent=3  avg_while_busy=1.44  peak 22:19:10（判定 b1/b2/b3 三批同发）
phase boundaries（phase summary 首次落盘时刻的在途任务数）
    phase2  22:15:14  in_flight=0  ⚠ 全局屏障
    phase2_5 22:18:12  in_flight=0  ⚠ 全局屏障
```

**对三个假设的裁定**：

| 假设 | 裁定 | 证据 |
|------|------|------|
| A 模型完全不批量 | **部分成立** | 63% 派发轮打包 2-3 个 task；但 7/19 轮仍单发 |
| B 阶段屏障 | **成立** | 两处 phase 边界 in_flight=0——模型等全部任务收尾才写 summary 进下一阶段 |
| C 结果注意力劫持 | 未证实（被 B 掩盖） | 需观察 1-task 轮后的下一轮是否补派（手工核对 7 个单发轮） |

**对原报告的修正**：lead/子代理 overlap=0.0s 是 `task_tool` 阻塞式设计的**结构性必然**
（lead 在工具 lane 里等子代理终态，无法同时发 LLM 调用），它**不度量**子代理与子代理
之间的并行。真正的并行度数字是 `max_concurrent=3` / `avg_while_busy=1.44`。
浪费不是「完全不并行」，而是「峰值打满、均值只有 1.44」——批量纪律不稳定 + 阶段
边界存在全局屏障。

### Task 1（P0）：调度模式测量 —— 把「串行」从印象变成数字

扩展 `backend/scripts/analyze_run_timing.py`，新增「调度模式」输出段：

- **每轮 task 调用数分布**：每个 AI 回复（lead）发出的 `task` tool_calls 数量
  （1/2/3/超发的直方图）。
- **子代理并发水位**：按 `subagent.step` 时间戳构建 31 个任务的 `[start, end]`
  时间窗（Gantt），逐 5s 采样计算在途任务数 → `max_concurrent` 与平均占用率。
- **热点阶段标注**：把水位曲线对齐阶段边界（phase2/3/4 summaries 的落盘时间），
  输出每阶段的实际并发 vs 理论并发。

对 b1510d50 跑一次做基线，顺带完成假设 A/B/C 的证伪。

**文件**：`backend/scripts/analyze_run_timing.py`
**测试**：`backend/tests/test_analyze_run_timing.py` 增加调度模式段的单测（构造
互有重叠/完全不重叠的任务事件，断言 `max_concurrent` 正确）。

### Task 2（P0）：dispatch-first 规则 —— 堵 C 假设的缺口

在 `SOUL.md` 核心原则 1 增加「dispatch-first」条款：

```
⛔ 收到 task 返回后，**先滑动补派下一批**（还有等待中的独立槽位时），
再处理本次返回内容。处理结果（读报告、改判、写 summary）不得挤占补派时机——
「等处理完再派」是零并行度的直接成因（会话 b1510d50：31 任务全串行，127.5min）。
```

同时修两处调度指引缺口：

- `qc-delegation.md`（**当前无并行调度节**）增加：QC 任务矩阵 = 患者 × 轨 × 批次，
  彼此独立（QC 子代理只读不写另一项 QC 的产物），打满 3 并发 + 滑动窗口，与判定阶段
  同一派发纪律。
- SOUL.md 阶段总览表（L294 附近）在 P3/P4 行补「+ 判定/QC 的并行纪律见
  `judge-delegation.md` / `qc-delegation.md`」交叉引用，防止 skill 拆分后指引失联。

**文件**：`backend/.deer-flow/agents/eligibility-screener/SOUL.md`、
`skills/custom/eligibility-judgment/references/qc-delegation.md`
**测试**：prompt 修改，无代码测试；用一个真会话验证（见验证方式）。

### Task 3（P1）：跨阶段边界显式拆 barrier —— 堵 B 假设的缺口

SOUL.md 与 judge/qc-delegation 目前没有一句话允许跨阶段重叠。补一条显式规则：

```
阶段切换不是全局屏障：某个 {患者,轨} 的判定产物落盘 + 机械闸清空后，该组合即可
进入 P4 QC，不等其他组合的判定收尾；P4.5 改判同理。禁止「等整阶段全绿再进下一阶段」。
```

**文件**：同 Task 2 两处
**测试**：真实会话验证（下一轮监控报告读数：max_concurrent ≥ 2）。

### Task 4（P2，可选）：判定阶段自检 —— 零并行运行时主动告警

在 `analyze_run_timing.py` 输出的基础上加一个快速判断（不改中间件、不动运行时）：

```
若判定阶段（P3）在途任务峰值 = 1 且独立任务数 ≥ 6 → 打印
「⛔ 判定阶段零并行：互不依赖的 N 个任务全串行，预计多花 ~2× 墙钟时间」
```

**文件**：`backend/scripts/analyze_run_timing.py`

## 明确不做

- **不改 `task_tool` 的阻塞模型**。backend 轮询收口对 LLM 循环是简化（模型拿到的
  就是终态结果，不需要自己轮询），当前的串行问题不在它；改非阻塞 + 显式 join 会给
  模型引入新的调度复杂度。
- **不改 `SubagentLimitMiddleware`**。3 并发上限与静默丢弃语义已有文档与内存
  （超发静默截断，不是排队），并行度问题与它无关。
- **不做 lead 端提前返回/多 Agent 编排重构**。收益不明确，成本大，先修调度纪律。

## 预期收益（b1510d50 基线外推）

- 判定/QC 阶段子代理忙时 4,857s → 3 路并发理论下限 ~1,620s；考虑 {患者,轨} 的
  QC→改判→QC 串行链与批次粒度，保守预期墙钟 **-35%~-45%**（127.5min → ~75-85min）。
- token 不直接下降（同量工作），但墙钟缩短缩小了中断/compaction 的暴露窗口，
  配合死循环熔断（dead-loop-fix-plan）共同压低「失控烧钱」风险。
- 并行度提升后 `expected_hash` 冲突风险**上升**（并发写不同文件没问题，但同文件
  并发写会冲突）——既有改判串行约束（judgment-repair.md「同组合串行」）必须继续
  严格执行，这也是本计划不试图提高 3 上限的原因。

## 实施顺序

| 优先级 | Task | 预期效果 | 难度 |
|--------|------|---------|------|
| P0 | 1. 调度模式测量 | 「串行」变可测数字，证伪 A/B/C | 中 |
| P0 | 2. dispatch-first + QC 并行节 | 堵最可能的 C 假设缺口 | 低 |
| P1 | 3. 跨阶段拆 barrier | 允许判定→QC→改判流水线重叠 | 低 |
| P2 | 4. 判定阶段自检告警 | 下次零并行运行时立刻可见 | 低 |

**建议**：Task 1 与 Task 2/3 并行推进（测量脚本与 prompt 修改无依赖），
Task 4 等 Task 1 的基线数据形态稳定后再加。

## 验证方式

1. `backend/tests/test_analyze_run_timing.py` 新单测全绿 + 既有用例零变化。
2. 对 b1510d50 重跑基线：调度模式段输出「每轮 task 数分布 + max_concurrent」。
3. 下一个真实病例监控（`analyze_run_timing.py` 新段）：
   - 判定阶段 `max_concurrent ≥ 2`（目标：=3）
   - 每轮 task 调用数分布中 ≥2 的轮次占派发轮的大多数
   - 活跃时间同规模病例 < 90min（基线 127.5min）