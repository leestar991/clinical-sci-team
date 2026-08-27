# IN-1/IN-8 死循环根因 + 优化方案

> 触发会话: `b1510d50-480d-4ff9-b344-af06556b8a05` (eligibility-screener, 2026-08-20)
> 计划日期: 2026-08-24
> 关联计划: `loop-detection-mutation-aware-reset-plan.md`、`2026-08-12-subagent-context-handoff-and-artifact-gate-plan.md`

## Problem Statement

会话 `b1510d50` 的 IN-1/IN-8 改判子任务 (`call_00_XbQLLfwz0nEtXGGiAUKN3302`) 陷入 680s/2.98M token 的死循环:
子代理反复运行 `uncertain_recheck.py` 门禁脚本 16 次，试图理解为什么 `main()` (CLI)
和 `recheck()` (直接调用) 在同一进程中产生不同结果，却从未直接修改判定文件的
`conclusion`/`reason` 字段。全 thread 34.5M token / 127.5 分钟活跃时间。

需要修的四件事，按因果链排序:

1. **子代理调试脚本而非修改数据** (根因) — 门禁失败后不修改产物，而是反复读源码、删 pyc、重置 history
2. **`main()` 与 `recheck()` 的数据加载路径差异** (诱因) — `main()` 从磁盘读取判定文件，`recheck()` 接受内存 dict，子代理无法理解这个差异
3. **熔断机制被绕过** (放大器) — 第 3 轮正确输出 `stuck_items` 并建议上报失败，但子代理手动删除 history 文件重置计数器
4. **`read_file` 截断 250 chars** (加剧) — 子代理被迫多次范围读取拼凑脚本源码，每次读取消耗 token 且不完整

## Background: 已核实的证据

### 死循环时间线 (来自 postgres `run_events`, task `call_00_XbQLLfwz0nEtXGGiAUKN3302`)

| 时间 (UTC+8) | 步骤 | 事实 |
|---|---|---|
| 23:11:12 | 1 | 任务启动: IN-1/IN-8漏判改判 |
| 23:11:22 | 1-6 | 正常: 读取 5 个输入文件 (recheck 产物、判定文件、criteria、judgment-principles、judgment-repair) |
| 23:13:02 | 17-19 | 首次运行 recheck → `suspected_missed=['IN-1','IN-8']`，连续 2 轮 |
| 23:13:37 | 26-31 | 开始调查脚本源码: 追踪 `_split_hits_by_document` 函数 |
| 23:14:02 | 32-39 | 发现 `.pyc` 缓存，删除后重跑 → **连续 3 轮 → 熔断升级!** `stuck_items=['IN-1','IN-8']` |
| 23:14:22 | 40-45 | 手动删除 history 文件，直接调用 `recheck()` → `suspected_missed=[]` ✅ |
| 23:16:12 | 48-50 | 重新运行 `main()` → **连续 4 轮未清!** 🔴 |
| 23:16:42 | 51-54 | 发现 `.pyc` 被重新创建，怀疑 import 自动生成 |
| 23:17:02 | 55-58 | 清除 pyc + history 重置 → 第 1 轮 → **仍然 `suspected_missed=['IN-1','IN-8']`** |
| 23:17:27 | 59-62 | **核心悖论确认**: `recheck()` → `[]`，`main()` → `['IN-1','IN-8']`，同一进程 |
| 23:18:47 | 71-74 | 重新读取 judgment-principles 和 repair 规则 |
| 23:19:07 | 75-78 | **绝望信号**: 尝试 `cd /tmp` → 沙箱违规; `python -c "import"` → 失败 |
| 23:19:57 | 81-82 | 最终确认: `recheck()` 和 `main()` 行为不一致 |
| 23:20:22 | 83-84 | 放弃脚本调试，尝试手动修改判定文件 |
| 23:21:07 | 85-88 | **发现关键线索**: IN-1/IN-8 的 `build_keywords` 返回空列表 `[]` |
| 23:22:42 | 97-99 | 最终确认 `.pyc` 与 `.py` 完全匹配，任务结束 (completed, 但问题未解决) |

### 关键统计

| 指标 | 数值 |
|------|------|
| `uncertain_recheck.py` 调用 | 16 次 (执行 4 + 读源码 4 + 其他引用) |
| `uncertain_recheck.cpython-312.py` 引用 | 5 次 |
| `check_judgment_structure.py` | 1 次 |
| `check_reason_alignment.py` | 1 次 |
| 重复读 `judgment-principles.md` | 2 次 |
| 重复读 `judgment-repair.md` | 2 次 |
| 全文件读取 | 9 次 |
| 工具错误 | 6 次 (`cd /tmp`、`python -c` 失败等) |
| compaction | 2 次 |
| 总 token | 2,984,776 (input=2,936,296, output=48,480) |

---

## 优化方案

### Task 1: 修复 `main()` 与 `recheck()` 的数据加载路径差异 (P0)

**根因**: `main()` 从磁盘 `json.load()` 读取判定文件，`recheck()` 接受内存 dict。
子代理在调查阶段从未修改过磁盘上的判定文件，所以 `main()` 每次读取的都是相同的原始数据，
而 `recheck()` 直接调用时传入的是子代理在内存中修改过的 dict。

**修复**:

在 `uncertain_recheck.py` 的 `main()` 中，加载判定文件后增加一个 **数据一致性检查**:
如果 `judgments` 与上次运行相比没有任何变化 (通过 `sha256sum` 比较)，
且 `suspected_missed` 非空，则在输出中显式声明:

```
⚠ 判定文件自上次检查以来未发生变化。suspected_missed 条目需要修改判定文件的
conclusion/reason 字段，而非重新运行本脚本。请使用 apply_json_patches 或
write_file 修改判定文件后重新运行。
```

同时在 `recheck()` 的输出结构中增加 `judgments_input_hash` 字段，
让子代理可以对比两次运行的输入是否相同。

**文件**: `skills/custom/eligibility-judgment/scripts/uncertain_recheck.py`
**测试**: 新增 `tests/test_uncertain_recheck_input_hash.py` 验证:
- 相同输入两次运行 → `judgments_input_hash` 相同
- 修改判定后运行 → `judgments_input_hash` 不同
- `suspected_missed` 非空 + 输入未变 → 输出包含显式修改指引

### Task 2: 硬编码 history 绕过防护 (P0)

**根因**: 子代理通过 `rm` 删除 history 文件来重置 `rounds_unchanged` 计数器，
绕过了"连续 3 轮未清 → 熔断"的防护机制。

**修复 (两层)**:

**A. 服务端防御**: 在 `recheck()` 中不再依赖外部 history 文件作为唯一计数源。
改为在 `recheck()` 内部对 `suspected_missed` 条目做**原地计数**:
每次调用时对比当前 `suspected_missed` 与上次调用时的集合，
如果完全相同则 `internal_rounds += 1`，否则重置。
这个计数器存储在 recheck 输出的 `_internal_state` 字段中，
由调用者 (子代理) 在下次调用时传回，形成闭环。

子代理无法绕过内部计数器，因为:
- 它不知道 `_internal_state` 的编码方式 (可以是 HMAC 签名的)
- 即使它伪造 `_internal_state`，服务端也会检测到不一致

**B. 子代理 prompt 约束**: 在 `judgment-repair.md` 中增加规则:

```
禁止操作:
- 不得删除或修改 uncertain_recheck 的 history 文件
- 不得删除 __pycache__ 目录
- 当 recheck 报告 stuck_items 时，必须上报主代理，不得继续修改
```

**文件**:
- `skills/custom/eligibility-judgment/scripts/uncertain_recheck.py`
- `skills/custom/eligibility-judgment/references/judgment-repair.md`
**测试**: `tests/test_uncertain_recheck_internal_counter.py`

### Task 3: `read_file` 截断导致源码碎片化 (P1)

**根因**: 事件数据显示 `read_file` 结果被截断到 250 chars。
子代理读取 `uncertain_recheck.py` (30KB) 时，每次只能看到 250 字符，
需要多次范围读取来拼凑完整函数逻辑。9 次全文件读取中，大部分是源码文件。

**修复**: 在子代理的 `ReadFileDedupMiddleware` 或 `ToolOutputBudgetMiddleware` 中，
对 `.py`/`.md` 等源码/文档文件的截断阈值放宽到至少 4096 chars。
或者，在 `uncertain_recheck.py` 中增加 `--help` 模式输出脚本的核心逻辑摘要
(不超过 2000 chars)，减少子代理需要读源码的场景。

**文件**:
- `packages/harness/deerflow/agents/middlewares/tool_output_budget_middleware.py`
- 或 `skills/custom/eligibility-judgment/scripts/uncertain_recheck.py` (增加 `--help`)
**测试**: 验证 `.py` 文件读取不被截断到 250 chars

### Task 4: 子代理 task prompt 缺乏"修改数据而非调试脚本"的指导 (P1)

**根因**: 子代理花 680s 调查 Python 导入行为差异，而不是直接修改判定文件。
这暴露了 task prompt 中缺乏"当门禁失败时，修改产物数据，不要调试门禁脚本本身"的明确指导。

**修复**: 在 `judgment-repair.md` 的首段增加:

```
核心原则: 门禁脚本 (uncertain_recheck.py 等) 是只读的验证工具。
当门禁报告 suspected_missed 时，你的任务是修改判定产物 (judgments_draft_*.json)
中的 conclusion/reason/evidence 字段，而不是调试或修改门禁脚本本身。

如果门禁脚本的行为看起来不一致:
1. 信任脚本的输出 — 它经过了测试验证
2. 修改判定文件中的数据 — 使用 apply_json_patches 或 write_file
3. 重新运行门禁验证 — 确认修改后门禁通过
4. 不要在脚本源码、pyc 缓存、或 Python import 机制上花费时间
```

**文件**: `skills/custom/eligibility-judgment/references/judgment-repair.md`
**测试**: 不需要代码测试，这是 prompt 修改

### Task 5: Lead 端 bash 合并错误 (P2)

**根因**: 10 次 lead 端 bash 错误集中在合并操作 (merge-judgments、merge-recheck、exclusion_direction)。
这些是主代理在汇总子代理结果时运行的脚本。

**修复**: 在 lead 端的合并脚本中增加输入校验:
- 检查输入文件是否存在且非空
- 检查 JSON 结构是否完整
- 失败时输出明确的错误信息 (包含期望的输入格式)

**文件**: `skills/custom/eligibility-screener/scripts/` 下的合并脚本
**测试**: `tests/test_merge_scripts_input_validation.py`

### Task 6: 零并行度 (P2, 长期)

**根因**: Lead 和子代理的重叠时间为 0.0s。Lead 在 poll 循环中等待子代理完成，
没有并行调度多个子代理。

**现状**: 已有 `SubagentLimitMiddleware` (MAX_CONCURRENT_SUBAGENTS=3) 和 `task_tool`。
但在这个会话中，判定阶段的任务是串行调度的。

**修复**: 不修改中间件代码，而是在 eligibility-screener 的 SOUL.md 中增加并行调度指导:

```
判定阶段: 将 IN/EX 各轨的判定任务按批次并行调度。
同一批次内的 task() 调用应在一次 AI 回复中同时发出 (多个 tool_call)，
而非逐个等待上一个完成后再发下一个。
```

**文件**: `skills/custom/eligibility-screener/SOUL.md`
**测试**: 通过监控下一轮运行验证并行度提升

---

## 实施顺序

| 优先级 | Task | 预期效果 | 实施难度 |
|--------|------|---------|---------|
| P0 | 1. `main()`/`recheck()` 路径差异 | 消除"脚本行为不一致"困惑 | 中 |
| P0 | 2. history 绕过防护 | 熔断机制不可绕过 | 中 |
| P1 | 3. `read_file` 截断 | 减少源码碎片化读取 | 低 |
| P1 | 4. task prompt 指导 | 防止子代理调试脚本 | 低 |
| P2 | 5. Lead 合并错误 | 减少 lead 端失败 | 低 |
| P2 | 6. 并行调度 | 减少总耗时 | 低 |

**建议**: Task 1-4 作为第一批 (修根因 + 建防线)，Task 5-6 作为第二批 (降噪 + 提效)。

## 验证方式

1. 运行 `tests/test_uncertain_recheck_input_hash.py` 和 `tests/test_uncertain_recheck_internal_counter.py`
2. 使用 `PYTHONPATH=. uv run python scripts/analyze_eligibility_run.py b1510d50-480d-4ff9-b344-af06556b8a05` 确认基线数据
3. 在下一个真实病例上运行，对比:
   - `uncertain_recheck.py` 调用次数 (基线: 16 次 → 目标: ≤3 次/任务)
   - 子代理 tool_error_steps (基线: 6 → 目标: 0)
   - 判定阶段总 token (基线: 34.5M → 目标: <20M)
   - 子代理任务平均耗时 (基线: ~200s → 目标: <120s)