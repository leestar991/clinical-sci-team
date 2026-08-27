# 会话分析方法论：b1510d50

> 适用会话：`b1510d50-480d-4ff9-b344-af06556b8a05` (eligibility-screener, 2026-08-20)
> 文档日期：2026-08-24
> 关联：`2026-08-24-in1-in8-dead-loop-fix-plan.md`（本分析驱动的修复计划）、
> `2026-08-24-eligibility-screener-parallelism-optimization-plan.md`（零并行度问题）

本文件是会话分析报告背后的**方法论**：怎么从 postgres `run_events` 原始事件中
还原出「不合理流程步骤 / token 账 / 耗时账」三个维度的结论，以及每一步的判据。
方法论对所有 eligibility-screener 会话通用；本篇以 b1510d50 为实例。

## 一、数据采集层

| 维度 | 工具 | 数据源 |
|------|------|--------|
| Token 账 | `backend/scripts/analyze_eligibility_run.py` | `run_events` 表（`event_type=ai`/`tool`/`subagent.step`） |
| 耗时账 | `backend/scripts/analyze_run_timing.py` | `run_events` 表（按 `created_at` 时间戳） |
| 步骤细节 | 数据库直查（sqlite/postgres） | `run_events` 表 `subagent.step` 事件原文 |

```bash
cd backend && PYTHONPATH=. uv run python scripts/analyze_eligibility_run.py \
  b1510d50-480d-4ff9-b344-af06556b8a05   # token 账
cd backend && PYTHONPATH=. uv run python scripts/analyze_run_timing.py \
  b1510d50-480d-4ff9-b344-af06556b8a05   # 耗时账
```

**初始化顺序陷阱**（已踩过，写死在这里）：

1. 必须先 `init_engine_from_config()`（`deerflow.persistence.engine`）再调用
   `make_run_event_store()`（`deerflow.runtime.events.store.db`），否则拿到的是
   空的 `MemoryRunEventStore`。
2. `list_events()` 返回 dict 而非对象：取字段用 `.get()`。
3. 事件正文在 `content` 字段（含 `kind`/`text`/`tool_calls`），`event_type` 在顶层。
4. CLI/migration 场景传 `user_id=None`。

## 二、分析方法层

### 2.1 死循环定位（IN-1/IN-8 追踪）

**方法**：按 `subagent.step` 事件逐条回放目标任务（`call_00_XbQLLfwz0nEtXGGiAUKN3302`）
的 99 步，从 `content.text` 提取 AI 思考、`content.tool_calls` 提取工具调用，按时间线排列。

**死循环判据**：

| 信号 | 判据 | b1510d50 表现 |
|------|------|---------------|
| 同一命令重复调用 | 相同 `tool_name` + 相同参数连续出现 | `uncertain_recheck.py` 16 次 |
| 熔断被绕过 | `stuck_items` 出现后仍有后续修改 | 第 3 轮熔断 → 删 history → 继续 |
| 产物未变化 | 门禁输入文件 hash 不变（对比 `judgments_input_hash`） | `main()` 每轮从磁盘读同一份判定 |
| 调试脚本而非改数据 | `read_file` 读 `.py` 源码 | 读 `uncertain_recheck.py` 源码 4 次 |
| 绝望信号 | 沙箱违规（`cd /tmp`）、`python -c` 失败 | 6 次工具错误 |

**真死循环 vs 合法复检**的区分（外推自 mutation-aware 重置工作的结论）：
死循环 = 「同一调用反复出现且期间世界没有任何变化」；若重复调用之间夹着写操作
（`apply_json_patches` 等），那是「改动后的复检」，不在此列。

### 2.2 Token 消耗分析

**分层计算**：

```
总 token = Σ(lead AI steps) + Σ(subagent AI steps) + Σ(middleware compaction)
```

- lead：`event_type=ai` 且属于主运行的步骤，取 `metadata.response.model_usage`
- subagent：`event_type=subagent.step` 同取 `model_usage`
- middleware：compaction 事件的 token 开销

**浪费指标检测**：

| 指标 | 检测方法 | b1510d50 数值 |
|------|---------|---------------|
| 空 AI 步骤 | `ai` 步骤 `text` 为空且 `tool_calls` 为空 | 60 步 |
| 全文件读取 | `read_file` 不带 `start_line`/`end_line` | 159 次 |
| 可去重读 | 相同 `file_path` + 内容 hash 相同的重复读取 | 8 次 |
| 工具错误 | `tool` 步骤 `content` 含 `Error`/`failed` | 42 次（子代理 32 + lead 10） |
| 门禁滥用 | bash 命令含门禁脚本名的调用，分「执行」vs「读源码」 | 148 次（执行 118 + 读源码 10 + 其他） |

### 2.3 耗时分析

**方法**：以 `run_events.created_at` 为基础时间轴：

```
lead_busy    = Σ(相邻 ai 步骤时间差)        # lead 在等 LLM 响应
subagent_busy= Σ(相邻 subagent 步骤时间差)   # 子代理在工作
overlap      = 两者时间窗口的交集           # 并行度
未计入        = 总时长 − 以上              # 工具/IO/人工等待
```

**零并行度检测**：`overlap = 0.0s` + `subagent_busy ≈ 活跃时间 − lead_busy`
（b1510d50：4,857s ≈ 7,648s − 2,552s，与「完全串行」吻合）。

**LLM 延迟特征**：按输入 token 分桶（0-50k / 50k-100k / 100k-150k），统计
平均延迟、输出 token、推理占比、缓存命中率。b1510d50：78/99 次调用落在 50k-100k，
延迟主要受 reasoning 驱动而非上下文大小。

### 2.4 分析结果结构（报告骨架）

```
会话分析
├── 总览（token/时间/运行数/步骤数）
├── 一、不合理流程步骤（死循环追踪、门禁滥用、hash 不匹配、工具错误、沙箱违规、compaction）
├── 二、Token 消耗（按运行分布、最重任务 Top 5、浪费指标）
├── 三、耗时分析（时间分布、LLM 延迟特征、子代理耗时 Top 5）
└── 四、核心问题（按严重程度排序，驱动修复计划）
```

## 三、b1510d50 分析结论摘录

| 结论 | 证据 | 驱动 |
|------|------|------|
| 🔴 IN-1/IN-8 死循环无熔断 | 16 次门禁调用、删 history 绕过、680s/2.98M token | dead-loop-fix-plan Task 1/2 |
| 🔴 148 次门禁脚本调用 | gate 失败→微调→重试，无学习机制 | dead-loop-fix-plan Task 4 |
| 🔴 零并行度 | overlap=0.0s，subagent 忙时≈活跃−lead | parallelism-optimization-plan |
| 🟡 81.8% token 在最后被中断的运行 | run 8dde1687 interrupted / 28.2M | 熔断 + run 级产物闸 |
| 🟡 expected_hash 不匹配 ×5 | 索引漂移（thread 3a745b38 同类） | 已修（条件ID 定位） |
| 🟡 7 次子代理 compaction | 压缩后仍轰炸 token | soul-compaction-plan |

## 四、已知计量陷阱（继承自 memories，写死防再次踩坑）

1. running 状态的 run 会被统计成 0
2. 子代理步骤无 latency 埋点（要按 created_at 差值估算）
3. 日志 UTC+8、库 UTC：跨来源对比要先统一时区
4. retry 的任务没有 Started 行
5. 失败步骤不以 `Error` 前缀开头（用 step status 判断，不是 grep）
6. `task` 参数只存在于 `checkpoint_writes`，不在 run_events
7. `.history` jsonl 的时间戳是 UTC（复现须用事发版本代码）