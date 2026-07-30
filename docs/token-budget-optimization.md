# Eligibility Screener Token 预算与循环保护优化分析

> 基于会话 `9e958425-2396-4ea0-b260-5b939e4e5c1f` (2026-07-16) 的根因分析

## 会话概要

- **会话 ID**: `9e958425-2396-4ea0-b260-5b939e4e5c1f`
- **Run ID**: `97ea4c9d-b8e4-45db-93fa-32af35fc4dcb`
- **模型**: `gpt-5-4`
- **任务**: 根据试验方案 + 筛选期病历(13页) + 筛选期检查(26页) 分析患者是否匹配入排标准
- **结果**: 两轮系统保护中断，HTML 报告未生成

## 问题描述

用户在 eligibility-screener 会话中提交了患者筛选任务，agent 执行到一半时被系统保护机制中断，输出了以下消息：

> "抱歉，这一轮被系统的循环保护中断了，我不能继续调用工具了。当前我已完成到'标准解析/QC + OCR覆盖确认'这一步，但还没有完成患者逐条入排判定和HTML报告生成..."

用户输入"继续执行"后，agent 继续推进到判定与 QC 阶段，但再次被 token 预算机制中断。

## 会话执行时间线

### 第一轮（原始请求）

| 阶段 | 内容 | 子代理数 | 状态 |
|------|------|----------|------|
| P1 | PDF 分类 + 入排标准提取 | 0 | ✅ 完成 |
| P2 | 标准解析(1) + 13页病历OCR(4) + 26页检查OCR(9) + 2轮语义QC(3) | 17 | ✅ 完成 |
| P2.5 | 患者拆分与OCR聚合 | 0 | ⚠️ 已由agent悄悄完成，但中断时未感知 |
| P3 | 患者S042002逐条判定 | 0 | ❌ 未开始 |
| P4 | 判定QC + 理由合并 | 0 | ❌ 未开始 |
| P5 | HTML报告生成 | 0 | ❌ 未开始 |

**中断触发**: `LoopDetectionMiddleware` hash-based 硬停止

### 第二轮（用户输入"继续执行"后）

| 阶段 | 内容 | 子代理数 | 状态 |
|------|------|----------|------|
| P2.5 | 患者拆分与OCR聚合 | 0 | ✅ 完成 |
| P3 | 患者S042002逐条判定 | 1 | ✅ 完成 |
| P4 | 判定QC + 理由生成 + QC复核 | 3 | ✅ 完成 |
| P5 | HTML报告生成 | 0 | ❌ 未完成 |

**中断触发**: `TokenBudgetMiddleware` 输入 token 硬停止（4,506,835 / 5,000,000）

## 根因分析

### 中断 #1：`LoopDetectionMiddleware` Hash-based 硬停止

**精确触发位置**：Checkpoint CP248–CP255（共 8 次连续相同的工具调用）

**具体重复的工具调用**（bit-for-bit 完全相同）：

```python
# 调用 1: read_file — 始终读取同一个文件同一段行范围
read_file(
    path='/mnt/user-data/workspace/criteria_parsed.json',
    start_line=1180,
    end_line=1268
)

# 调用 2: task — OCR 子代理，描述和 prompt 完全不变
task(
    description='OCR检查13-15页',
    subagent_type='general-purpose',
    prompt='对以下图片执行 view_image + OCR 识别，并把每页输出为同名 .md：...'
)

# 调用 3: task — 另一个 OCR 子代理
task(
    description='OCR检查16-18页',
    subagent_type='general-purpose',
    prompt='对以下图片执行 view_image + OCR 识别，并把每页输出为同名 .md：...'
)
```

**哈希计算详解**：

`LoopDetectionMiddleware._hash_tool_calls` 对工具调用集合计算 MD5 哈希（截取前 12 位）。每个工具调用的 key：

1. `read_file` → `_stable_tool_key` 对 `read_file` 使用 200 行桶：`start_line=1180, end_line=1268` → 桶范围 `(1180//200, 1268//200)` = `(5, 6)` → key = `/mnt/user-data/workspace/criteria_parsed.json:5-6`

2. `task(OCR检查13-15页)` → args 不含 salient fields（`path`, `url`, `query`, `command`, `pattern`, `glob`, `cmd`），回退到 `json.dumps(args, sort_keys=True, default=str)` → **哈希完整的 prompt 文本**（包括图片路径列表）

3. `task(OCR检查16-18页)` → 同上，但图片路径不同 → 不同哈希

三个 key 排序后拼接 → `hashlib.md5(...).hexdigest()[:12]` → **Hash `06515c063677`**（CP248–CP255）

**为什么会连续 8 次**：

CP248–CP255 之所以是 **8 次**而非 5 次（硬停止阈值），是因为存在一个复合循环：

| CP | 次数 | 动作 |
|----|------|------|
| CP248 | 1 | Agent 启动 OCR检查13-18页 |
| CP249 | 2 | 子代理返回结果，但 agent **仍然**启动同样的两个 OCR 任务 |
| CP250 | 3 | ⚠️ WARNING: `[LOOP DETECTED]` 注入到下一轮 |
| CP251 | 4 | — |
| CP252 | 5 | 🛑 HARD STOP: tool_calls 被清除，强制文本回答 |
| CP253 | 6 | 🔁 Goal Continuation 重新进入 → agent 再次产生相同工具调用 → 立即 HARD STOP |
| CP254 | 7 | 🔁 Goal Continuation 再次进入 → HARD STOP again |
| CP255 | 8 | 🔁 Goal Continuation 第 3 次 → HARD STOP，达到 max_continuations=8 |
| CP256 | — | Agent 最终产出 "抱歉，这一轮被系统的循环保护中断了" |

**循环放大器：Goal Continuation System**

`LoopDetectionMiddleware` 的硬停止 + Goal Continuation 形成了一个恶性循环：
1. `LoopDetectionMiddleware` 在第 5 次硬停止 → model 被迫产出文本
2. Goal 评估器判定 "任务未完成" → 触发隐藏 continuation
3. Model 以相同上下文重新进入 → 产出相同的工具调用模式
4. `LoopDetectionMiddleware` 立即再次硬停止（因为同一 hash 在滑动窗口内）
5. 重复直到 `max_continuations=8` 或 `no_progress=2` 触发

**架构不对称（已核对代码，强化此担忧）**：`TokenBudgetMiddleware` 硬停止时会设置
`BUDGET_HARD_STOPPED_KEY`，`worker.py` 读取它后**主动跳出 goal continuation 循环**（incident
`aca54c56` 的修复）。但 `LoopDetectionMiddleware` 硬停止**没有等价的打破标志**——它触发后，
goal continuation 仍会再入模型，只能靠 `no_progress=2` 兜底。因此本节描述的"循环放大器"是一个
真实存在的架构缺口，而非推测。补齐一个对称的 loop-detection 硬停止打破标志，是比抬高阈值更根本的
修法（见优化建议 #8）。

**根本原因**：Lead agent 在连续多轮中对同一个 `criteria_parsed.json` 文件（lines 1180-1268）做 `read_file`，并在看到子代理结果后仍重新启动相同的 OCR 子代理。这表明 agent **没有正确感知子代理已完成**——可能是因为 summarization 压缩了 context 导致子代理完成信息丢失。

**关键配置**：
```yaml
loop_detection:
  enabled: true
  warn_threshold: 3    # 3次重复 → 注入警告 HumanMessage
  hard_limit: 5        # 5次重复 → 清除 tool_calls，强制文本回答
  window_size: 20      # 滑动窗口大小
```

### 中断 #2：`TokenBudgetMiddleware` 输入 Token 硬停止

**直接证据**（浏览器页面中可见）：

```
[TOKEN BUDGET EXCEEDED] The input token usage (4,506,835) has exceeded
the safety limit (5,000,000). Producing final answer with results collected so far.
```

**触发机制**：

`TokenBudgetMiddleware` 在每个模型响应后累加 token 使用量（`usage_accum.input` 单调递增，summarization 删除消息后**不回退**）。当任一维度的使用量超过 `max × hard_stop_threshold` 时，清除 `tool_calls` 并强制文本回答。

- `max_input_tokens: 5,000,000`
- `hard_stop_threshold: 0.9`
- 硬停止线：`5,000,000 × 0.9 = 4,500,000`
- 实际使用：`4,506,835`（仅超出 0.15%）

**Token 消耗来源分析**：

| 来源 | 估计消耗 | 占比 |
|------|---------|------|
| 39页OCR子代理（每页~80K input） | ~3.1M | ~69% |
| 标准解析 + 2轮QC子代理 | ~0.5M | ~11% |
| 患者判定 + QC复核子代理 | ~0.5M | ~11% |
| Lead agent 调度 + 文件读写 | ~0.4M | ~9% |
| **合计** | **~4.5M** | **100%** |

OCR 子代理是主要的 token 消耗来源。每个 OCR 子代理处理 2-3 页图片，需要：
1. 加载 `view_image` base64 编码的图片（每张 ~500KB+ → ~60K+ tokens）
2. 读取 OCR 指令 prompt
3. 写入 OCR 结果文件

### 为什么 Token 计数器不回退

这是有意为之的安全设计（见 `config.yaml` 注释）：

> TokenBudgetMiddleware 的累计是"单调累加"——usage_accum.input 只增不减，summarization 删除旧消息后累计计数器不回退。所以 max_input_tokens 实际约束的是"整个 run 的 input 总和"，而非"单轮 input"。

设计理由：防止 agent 通过 summarization "刷新" token 预算来绕过限制，形成无限循环。

## 保护层全景

该会话的 10 层保护机制中，2 层被实际触发：

| # | 保护层 | 触发? | 阈值/原因 |
|---|--------|-------|-----------|
| 1 | **LoopDetectionMiddleware (hash)** | ✅ 中断#1 | OCR批量轮次工具调用模式重复 ≥5次 |
| 2 | **TokenBudgetMiddleware** | ✅ 中断#2 | 输入token 4.5M ≥ 4.5M (90% of 5M) |
| 3 | LoopDetectionMiddleware (freq) | ❌ | `read_file` 80次告警/100次硬停，未达到 |
| 4 | LangGraph Recursion Limit | ❌ | 配置 1000，未达到 |
| 5 | Run Inactivity Watchdog | ❌ | 代理一直在活动（600s 超时） |
| 6 | Subagent max_turns | ❌ | general-purpose: 150 turns |
| 7 | Subagent Inactivity Timeout | ❌ | 子代理持续工作 |
| 8 | Subagent Wall-Clock Timeout | ❌ | 1800s 默认超时 |
| 9 | Goal Continuation Caps | ❌ | 未触发 goal 连续循环 |
| 10 | SafetyFinishReason | ❌ | 提供商未安全终止 |

## 关键发现

### 1. Token 预算是 eligibility screener 的核心瓶颈

即使已将 `max_input_tokens` 从 1.5M 提升到 5M（基于会话 `20d7e28c` 的经验，见 config.yaml 注释），完整 5 阶段的**单患者筛选**仍需要超过 5M 输入 token。OCR 子代理的 token 消耗占比最高（~69%），因为每页图片需要 base64 编码送入视觉模型。

### 2. 子代理 token 治理是根本解决方向

⚠️ **修正**：子代理**已有**独立的 token 预算机制——`subagents.token_budget`
（`config/subagents_config.py`）默认关闭（`None`），一旦启用会为每次子代理执行挂独立的
`TokenBudgetMiddleware`。当前问题是它**未被启用**，因此每个 OCR 子代理处理 2-3 页时，其 token 仍
全部计入 lead agent 的累计计数器。子代理级 summarization 则确实尚未实现。config.yaml 注释指出：

> 根治方向仍是子代理 token 治理（子代理级 summarize/限制单子代理 input），而非无限抬高总预算。

即根治的第一步是**启用现有的 `subagents.token_budget`**（见优化建议 P0 #2），而非新开发。

### 3. P2.5 状态感知缺陷

Agent 在被 LoopDetectionMiddleware 中断时，**实际已悄悄完成了 P2.5**（`patient_index.json`、`phase2_5_summary.json` 已写入工作区），但中断响应中却说 P2.5 未完成。这是因为：

1. Hard stop 清除了 tool_calls，模型被强制"立即"生成文本回答
2. 模型基于不完整的状态感知（可能尚未读取 P2.5 的输出文件）给出了错误的状态描述
3. 当用户说"继续执行"时，agent 重新读取工作区才发现 P2.5 已完成，直接从 P3 开始

### 4. `task` 工具调用的哈希策略存在盲区

`_stable_tool_key` 对 `task` 工具回退到全参数哈希（包括完整 prompt），这本意是好的（不同 prompt = 不同哈希）。但问题在于 lead agent 的**非 task 工具调用**（如 `read_file` 读取同一文件、`write_todos` 更新相似状态）在连续多轮中产生了相同的哈希，导致整个工具调用集合的哈希碰撞。

## 优化建议

> **优先级修订说明（基于代码核对）**：本节按"是否命中两次中断的真实根因"重排优先级，而非
> 按开发工作量。两次中断的根因分别是——中断 #1 = **hash-based 硬停止**（OCR 子代理被重复派发），
> 中断 #2 = **OCR 图片 base64 编码撑爆单调累加的输入预算**（占 ~69% token）。因此下方 **P0/P1**
> 是直接对症的修法，抬高预算/阈值仅作"止血"归入 P2。

### P0 — 直接命中根因（优先落地）

#### 1. OCR 图片编码优化（命中中断 #2 主因，~69% token）

OCR 子代理的图片 base64 编码是输入预算的最大消耗源，直接压缩它比抬高全局预算更有效：

- **对纯文本 PDF 页面（非扫描件）跳过 OCR/图片编码**，直接提取文本层——收益最大
- 缓存 `view_image` 的图片编码，避免同一张图重复编码送入模型
- 合并小批量：当前每个子代理处理 2-3 页，可合并为 5-6 页，摊薄调度开销

#### 2. 启用子代理级 Token 预算（**已实现，只需配置**）

⚠️ **修正**：此项曾被列为"需要开发的中期项"，但代码中 `subagents.token_budget`
（`config/subagents_config.py`，注释标记"阶段3/建议6"）**已经实现**——它会为每个子代理执行挂一个
独立的 `TokenBudgetMiddleware`（`tool_error_handling_middleware.py:273`），作用域仅限该次委派任务，
与 lead agent 的全局 `token_budget` 相互独立。因此这是**开箱即用的配置项**，不是开发任务：

```yaml
subagents:
  token_budget:               # 全局作用于所有子代理执行
    enabled: true
    max_input_tokens: 80000   # 单次子代理执行的输入上限
    hard_stop_threshold: 0.9
```

**注意**：`SubagentOverrideConfig`（`subagents.agents.<name>`）当前只支持
timeout/model/max_turns/skills，**没有** per-type 的 `token_budget` 字段。所以"只对
`general-purpose` 限制而不影响其他子代理"这种粒度**才是需要开发的新字段**（见 P3 #7）。

#### 3. 补齐 LoopDetection 硬停止的 goal-continuation 打破标志（命中中断 #1 循环放大）

对称补齐 `TokenBudgetMiddleware` 已有的 `BUDGET_HARD_STOPPED_KEY` 机制：让
`LoopDetectionMiddleware` 硬停止时也设置一个标志，`worker.py` 读取后跳出 goal continuation 循环，
避免"硬停止 → goal 再入 → 立即再硬停止"的空转（当前只靠 `no_progress=2` 兜底）。

#### 4. `task` 工具的循环检测独立化（命中中断 #1 触发点）

中断 #1 是 **hash 层**（Layer 1）触发的，`task` 与 `read_file` 等工具被混入同一个工具调用集合哈希，
导致集合级碰撞。修法：

- 将 `task` 从统一哈希中**排除**，或为其创建**独立的、更高阈值的检测窗口**
- `task` 的参数组合天然多样，不应与其他工具共用一个 hash 桶

### P1 — 治本但需要更多开发

#### 5. 子代理级 Summarization

当 OCR 子代理的上下文增长时，自动触发 summarization 精简历史。与 P0 #2 的子代理预算配合使用，
比抬高全局预算更精准。

#### 6. lead agent SOUL.md 修正状态感知（针对"重复派发"根因）

根因是 agent **感知不到子代理已完成**（summarization 压缩丢失完成信息 → 重复派发相同 OCR）。
在 `SOUL.md` 的 OCR 批量阶段：

- 要求 agent 减少对相同文件（如 `criteria_parsed.json` lines 1180-1268）的重复 `read_file`
- 更彻底：将 phase 完成状态**持久化到工作区文件**，并在继续前强制读取该状态文件，而非依赖上下文记忆

### P2 — 止血（治标，不作为主要解法）

#### 7. 提高 Token 预算上限

```yaml
token_budget:
  max_input_tokens: 8000000   # 5M → 8M
  max_tokens: 10000000        # 6M → 10M
```

**风险**: 仅延缓问题，不根治。完整多患者场景（如 3-5 个患者）仍会超出。应在 P0/P1 落地后再评估是否仍需。

#### 8. 调优 LoopDetection 阈值

```yaml
loop_detection:
  hard_limit: 8              # 5 → 8（给予更多轮次空间）
  tool_freq_overrides:
    task:
      warn: 30
      hard_limit: 60
```

⚠️ **靶向提醒**：`tool_freq_overrides.task` 只作用于**频率层（Layer 2）**，**无法阻止**本次由
**hash 层（Layer 1）**触发的中断 #1。要缓解 hash 触发只能抬全局 `hard_limit`（5→8），但那会
**削弱所有循环的保护**，是钝器。对症的做法是 P0 #4（`task` 独立化），此处仅作过渡。

### P3 — 架构改进（长期）

#### 9. Phase 级 Token 预算分段

按 phase 分段计算 token 预算，而非全 run 单调累加。例如：
- P1+P2: 2M 预算
- P2.5+P3: 1.5M 预算
- P4+P5: 1.5M 预算

需要配合 phase 边界检测和防绕过机制。

#### 10. 子代理 per-type token 预算字段（新开发）

在 `SubagentOverrideConfig` 中新增 `token_budget` 字段，支持
`subagents.agents.general-purpose.token_budget`，实现"仅对特定子代理类型限制"的粒度
（当前全局 `subagents.token_budget` 无法区分类型，见 P0 #2 说明）。

## 相关文档

- [eligibility-screener-monitoring-issues.md](eligibility-screener-monitoring-issues.md) — 10项监控问题列表
- [eligibility-screener-monitoring-optimization-changelog.md](eligibility-screener-monitoring-optimization-changelog.md) — 监控修复与回滚记录
- [eligibility-screener-qc-loop-analysis-20d7e28c.md](eligibility-screener-qc-loop-analysis-20d7e28c.md) — QC 循环不收敛分析
- `config.yaml` token_budget 注释 — 历次预算校准记录（`aca54c56`, `ff2192fb`, `20d7e28c`）
