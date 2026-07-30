# eligibility-screener 修复变更汇总

> 基于 [eligibility-screener-fix-plan.md](./plans/eligibility-screener-fix-plan.md)，实施时间 2026-07-11。
>
> 关联分支：`feature/gpt-team`
>
> **2026-07-11 修订**：根据代码 review 修复了 C2 watchdog 无法中断阻塞 LLM 调用（问题 1）和 E1 SSE 断连检测对会话内断连无效（问题 2）两个有效性缺陷，并修复了 `_stream_once` 中 `ping` 误写成元组导致心跳调用崩溃的 bug。详见第 6 章。
>
> **2026-07-11 二次修订**：基于 `aca54c56` 会话实测 token 数据分析 budget 异常根因，发现硬停后 goal-continuation 循环未终止导致 token 失控增长，重新校准 budget 阈值并修复循环。详见第 7 章。
>
> **2026-07-13 三次修订**：针对 tool calls/read_file 反复读取导致的上下文爆炸，实施 4 阶段优化（read_file 外部化 + SOUL 读取纪律 + summarization 降阈值 + read_file 去重缓存）。详见第 8 章。

---

## 1. 变更概览

| 层级 | 文件数 | 新增测试 | 优先级覆盖 |
|------|--------|----------|-----------|
| 配置 | 1 | - | D1/D2/D3/C2 |
| 后端 Harness | 1 | 10 | C2 |
| 技能脚本 | 1 | - | A1 |
| Agent SOUL.md | 1 | - | C1/A2/A3/B1/B2/D4/E2 |
| 前端 | 1 | - | E1 |
| 测试（新增） | 2 | 29 | D6/C2 |

**统计**：6 个源文件，2 个新测试文件，29 个新增测试用例，约 370 行新增代码。

---

## 2. 逐文件变更详情

### 2.1 config.yaml（gitignored，仓库根目录）

**涉及任务**：D1, D2, D3, C2

| 配置项 | 旧值 | 新值 | 说明 |
|--------|------|------|------|
| `token_budget.enabled` | `false` | `true` | 启用 token 硬停机制 |
| `token_budget.max_tokens` | `200000` | `600000` | 单 run 总 token 上限 |
| `token_budget.max_input_tokens` | `null` | `400000` | 输入 token 单独限制 |
| `token_budget.hard_stop_threshold` | `1.0` | `0.95` | 95% 触发硬停，剥离 tool_calls |
| `summarization.trigger[0].value` | `120000` | `80000` | 更早触发上下文压缩 |
| `tool_output.externalize_min_chars` | `12000` | `8000` | 更积极外部化大输出 |
| `run_inactivity_timeout_seconds` | _(无)_ | `300` | **新增**：run 无活动超时 5 分钟 |

**效果**：
- 本次卡死场景（271 万 token）：硬停会在约 57 万 token 时触发，agent 被强制产出最终答案
- summarization 在 8 万 token 即触发压缩，避免单轮塞入大量 OCR 文本
- tool_output 在工具输出达 8000 字符时外部化，减少内存上下文

---

### 2.2 backend/packages/harness/deerflow/runtime/runs/worker.py

**涉及任务**：C2（run 级无活动超时）

**新增函数**：

#### `_inactivity_watchdog(abort_event, activity_event, timeout_seconds, record)`
- 异步 watchdog 协程，监控 run 是否有流事件产出
- stream loop 每收到一个 chunk 调用 `activity_event.set()` 作为心跳
- watchdog 等待心跳；超时未收到心跳 → 设置 `abort_event` → run 标记 `timeout`
- 通过 `record.error` 写入超时信息，供最终状态处理区分用户中断与 watchdog 超时

#### `_resolve_run_inactivity_timeout(app_config)`
- 从 `AppConfig.run_inactivity_timeout_seconds` 读取超时配置
- 默认 300 秒（5 分钟），支持零值和格式容错

#### `run_agent()` 改动
- 新增变量：`activity_event`（心跳 Event）、`watchdog_task`（watchdog 任务）、`inactivity_timeout`
- 步骤 1a：`set_status(running)` 后立即启动 watchdog
- `_stream_once()`：新增 `heartbeat_event` 参数，每个 chunk 调用 `ping()` 发送心跳
- 步骤 8 最终状态：区分 `is_timeout`（watchdog 触发）vs `action == "rollback"` vs 普通中断
- finally 块：取消 watchdog 任务，防止泄漏

**配置项**：`config.yaml` → `run_inactivity_timeout_seconds: 300`

**验证**：`backend/tests/test_run_inactivity_watchdog.py`（10 个测试，全部通过）

---

### 2.3 skills/custom/pdf-image-extractor/scripts/pdf_to_image.py（gitignored）

**涉及任务**：A1（输出 manifest 清单）

**改动**：
- `convert_pdf()` 末尾新增：写入 `{stem}_manifest.json`
- manifest 包含 `source`、`stem`、`total_pages`、`pages[]`（每页 `page`/`filename`/`format`）
- 顶层 `import json` 新增

```python
manifest = {
    "source": pdf_path.name,
    "stem": stem,
    "total_pages": total_pages,
    "pages": [
        {"page": i + 1, "filename": p.name, "format": p.suffix.lstrip(".")}
        for i, p in enumerate(outputs)
    ],
}
manifest_path = output_dir / f"{stem}_manifest.json"
manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
```

**效果**：下游统计/覆盖率校验可通过 manifest 可靠获知每页实际格式（.png 或 .jpg），不再依赖扩展名猜测。

---

### 2.4 backend/.deer-flow/agents/eligibility-screener/SOUL.md（gitignored）

**涉及任务**：C1, A2, A3, B1, B2, D4, E2

#### C1 — 原则 7 判定阶段约束（第 82–85 行）
```diff
-### 7. 禁止无效探索
+### 7. 禁止无效探索（含判定阶段约束）
 - Phase 1→2 转换时禁止 glob/find/ls 搜索历史文件
-+**Phase 3-5 判定时，证据仅来自 OCR 文本与 extraction.json；
+  禁止 grep uploads/原始 PDF 补救。证据缺失直接判「无法判断」
+  并在判定理由中记录「缺失页：xxx」，不阻塞流程、不重复搜索。**
 - 每次新会话从零构建
```

#### A2 — Phase 1 第三轮统计脚本（第 186–208 行）
- 原：`ls workspace/images/{source_name}/` 获取图片列表和总页数
- 新：优先读 `_manifest.json`，回退到 `iterdir()` 全格式 glob（`.png`/`.jpg`/`.jpeg`）

#### A3 — 原则 5 表格补注（第 70 行后）
新增格式说明：图片提取可能输出混合 `.png`/`.jpg`，统计必须匹配全格式或读 `_manifest.json`，禁止仅 glob `*.png`。

#### B1/B2 — Phase 2 覆盖率核验（第 246–267 行）
- 覆盖率以实际图片文件数为分母，**不以 `total_pages` 为准**
- 优先读 manifest 获取 `img_count`；`ocr_count` 按实际 `.md` 文件数统计
- 缺失页强制补漏：`missing = set(img_stems) - set(ocr_stems)`，未补全前禁止越过 Phase 2 屏障

#### D4 — 阶段间上下文压缩

| Phase | 新增 summary | 内容 | 后续读取 |
|-------|-------------|------|---------|
| P3 末 | `phase3_summary.json` | 患者列表 + extraction/judgments_draft 路径 + 判定统计 | P4 `read_file` |
| P4 末 | `phase4_summary.json` | judgments 合并路径 + QC 结论 | P5 `read_file` |

Phase 4 和 Phase 5 均增加前置步骤从 summary 文件读取数据。

#### E2 — todos 状态管理（原则 10）
- 原：阶段完成 → `[✓]`（与下一阶段首个 tool_call 同轮）
- 新：**产出物就绪后立即标记 `[✓]`**（不可延迟），写入阶段 summary 后同时标记

---

### 2.5 frontend/src/app/workspace/chats/[thread_id]/page.tsx

**涉及任务**：E1（SSE 断连检测）

**改动**：
- 新增导入 `useThreadRuns`
- 新增 `threadRuns` 查询：获取最新 run 状态
- 新增 SSE 断连检测逻辑：
  ```typescript
  const latestRun = threadRuns.data?.[0];
  const isRunActive = latestRun?.status === "running" || latestRun?.status === "pending";
  const isSSEDisconnected = !isNewThread && !isMock && isRunActive && !thread.isLoading && !thread.error;
  const inputStatus = thread.error
    ? ("error" as const)
    : thread.isLoading || isSSEDisconnected
      ? ("streaming" as const)
      : ("ready" as const);
  ```
- `InputBox` status prop 改用 `inputStatus`（替代原内联三元表达式）

**效果**：当 SSE 断连但 run 仍在运行时，UI 显示 "streaming" 状态（显示停止按钮），而非静默显示 "ready"（闲置）。

---

### 2.6 backend/tests/test_view_image_middleware_context_cleanup.py（新增）

**涉及任务**：D6

**测试覆盖**（19 个用例）：

| 测试类 | 用例数 | 覆盖 |
|--------|--------|------|
| `TestIsImageInjectionMessage` | 5 | 识别有效注入消息 / 拒绝非 HumanMessage / 拒绝字符串 content / 拒绝无 marker / 拒绝无 image block |
| `TestExtractImagePaths` | 3 | 单路径提取 / 多路径提取 / 空消息返回空 |
| `TestCreateLightweightReference` | 2 | 创建路径引用 / 保留 additional_kwargs |
| `TestStripHistoricalImageBase64` | 5 | 剥离历史保留当前 / 仅当前不变 / 无图片不变 / 多个历史全剥离 / 混合消息正确 |
| `TestWrapModelCall` | 4 | wrap_model_call 剥离历史 / 保留当前 / 无图片不变 / 空消息 |

---

### 2.7 backend/tests/test_run_inactivity_watchdog.py（新增）

**涉及任务**：C2

**测试覆盖**（10 个用例）：

| 测试类 | 用例数 | 覆盖 |
|--------|--------|------|
| `TestResolveRunInactivityTimeout` | 5 | 无配置默认值 / 字段缺失默认 / 读取配置值 / 忽略零值负值 / 容错非数值 |
| `TestInactivityWatchdog` | 5 | 超时触发 abort / 活动重置计时 / abort 已设立即退出 / record 写入错误 / 取消优雅处理 |

---

## 3. 测试结果

```text
test_view_image_middleware_context_cleanup.py  19 passed
test_run_inactivity_watchdog.py                10 passed
test_token_budget_middleware.py                 8 passed
─────────────────────────────────────────────────────
Total                                          37 passed
```

前端 typecheck：✅ 通过
前端 lint：✅ 通过（仅预先存在的 3 个 warning）

---

## 4. 修复效果对照

| 问题 ID | 根因 | 修复任务 | 预期效果 |
|---------|------|---------|---------|
| Q1 | `_save_within_limit` PNG→JPEG 降级 | A1 | manifest 记录每页实际格式，下游可靠获知 |
| Q2 | `glob('*.png')` 漏 .jpg | A2 | 优先读 manifest，回退全格式 glob |
| Q3 | total_pages 错误分母 | B1/B2 | 实际文件数为分母 + 缺失页强制补漏 |
| Q4 | ECOG grep 空结果决策循环 | C1 + C2 | 禁 grep uploads → 缺失直接判"无法判断"；300s 无活动超时 |
| Q5 | token 271 万 vs 目标 35K | D1/D2/D3/D4 | 60 万硬停 + 8 万 summarization + 8K tool_output + 阶段间 summary |
| Q6 | SSE 断连静默 idle | E1 | 检测 run 活跃 → 显示 "streaming" 而非 "ready" |
| Q7 | todos 状态滞后 | E2 | 产出物就绪立即标记，不可延迟 |

---

## 5. 风险评估与后续

| 风险 | 缓解措施 | 监控指标 |
|------|---------|---------|
| token_budget 60 万误杀正常长任务 | 基于实跑数据（本次 271 万）的 1/4 余量；后续可上调 | 观察 hard_stop 触发频率 |
| summarization 80K 触发过频 | keep=30 messages 保持近期上下文；DurableContextMiddleware 保持关键信息 | 压缩后输出质量 |
| C2 run 超时误判慢 LLM | 阈值取 5 分钟（LLM 单次响应通常 < 1 分钟） | 观察 timeout 率 |
| 判定禁 grep 导致"无法判断"偏多 | B1/B2 保证 OCR 全覆盖后证据齐全，grep 需求消失 | 报告"无法判断"比例 |
| manifest 旧会话缺失 | A2 设计了 manifest 缺失回退到全格式 glob 的逻辑 | 无 |

**建议**：
- 上线后观察第一周 token_budget hard_stop 触发次数，必要时调整 `max_tokens` 阈值
- 观察 C2 watchdog timeout 触发频率，确认 300s 阈值是否合适
- Phase 3/4 summary.json 是否有效减少了后续阶段的上下文 token 消耗

---

## 6. Review 修订（2026-07-11）

代码 review 发现 C2 和 E1 两个新增机制存在有效性缺陷，本次修订修复。

### 6.1 问题 1：C2 watchdog 无法中断阻塞的 LLM 调用

**缺陷**：原 watchdog 超时只调用 `abort_event.set()`，但 `abort_event` 仅在 `agent.astream()` 两个 chunk 之间被检查。当 LLM 调用挂起（无 chunk 产出，正是 Q4 事故场景），`astream` 协程 suspended 在 `await response` 上，`abort_event.set()` 无法打断--只有 `task.cancel()` 注入 `CancelledError` 才能中断。原实现对声明的核心场景（13+ 分钟无 checkpoint 推进）无效，要等到模型 `request_timeout: 600`（10min）才中断。

**修复**：
- `RunRecord` 新增 `watchdog_timed_out: bool` 标志（替代脆弱的 `"timed out" in record.error` 字符串匹配）
- watchdog 超时时：设置标志 + `record.task.cancel()` 真正中断阻塞协程（`record.task` 可能在 watchdog 启动前未赋值，用 `getattr(record, "task", None)` 防御）
- 步骤 8 最终状态：rollback 优先级最高（用户 cancel 后到 watchdog 触发仍须尊重 rollback 契约），其次 `watchdog_timed_out` -> `timeout`，否则 `interrupted`
- `except asyncio.CancelledError` 分支：watchdog cancel 注入的 CancelledError 标记为 `timeout`（区分用户主动 cancel）

**文件**：`worker.py`（`_inactivity_watchdog`、`run_agent` 步骤 8 与 `CancelledError` 分支）、`manager.py`（`RunRecord` 字段）

**测试**：新增 `test_cancels_record_task_on_timeout`（验证挂起 task 被 cancel）、`test_no_cancel_when_record_task_missing`（防御性），更新 `test_records_error_on_record`（验证标志）。共 12 个测试通过。

### 6.2 问题 2：E1 SSE 断连检测对会话内断连无效

**缺陷**：`useThreadRuns` 的 `useQuery` 无 `refetchInterval` 且 `refetchOnWindowFocus: false`，runs 查询（key `["thread", threadId]`）仅在 mount 和 `onFinish`/stop 时刷新。`onCreated`/`onStart` 不刷新该查询。故会话内新发起的 run 不进入 runs 缓存；SSE 断连后 SDK 静默停止（`isInactiveRunStreamError` 在 `api-client.ts:111` 静默 return，不触发 `onError`），`threadRuns.data` 仍是旧快照，`isRunActive=false`，E1 不触发。

**修复**（三处联动）：
1. `onCreated`：run 启动时 invalidate `["thread", threadId]`，让新 run 立即进入缓存
2. `onError`：SSE 错误时也 invalidate `["thread", threadId]`（覆盖非静默断连）
3. `useThreadRuns`：`refetchInterval` 改为函数式回调，仅当最新 run 为 `running`/`pending` 时以 4s 轮询（覆盖 SDK 静默停止场景）。空闲线程不轮询，成本可控。

**文件**：`frontend/src/core/threads/hooks.ts`（`useThreadStream` 的 `onCreated`/`onError`、`useThreadRuns`）

**验证**：typecheck + lint 通过。

### 6.3 顺带修复：`_stream_once` 心跳调用崩溃

**缺陷**：第一轮 C2 实现中 `ping = (heartbeat_event.set,)` 误将方法包成单元素元组，导致 `ping()` 报 `'tuple' object is not callable`。被 `test_goal_worker.py` 等集成测试捕获（run 因异常落到 `error` 而非预期的 `interrupted`）。

**修复**：`ping = heartbeat_event.set if heartbeat_event is not None else None`

**验证**：`test_run_manager.py` + `test_run_worker_rollback.py` + `test_goal_worker.py` 共 114 测试通过；`test_worker_langfuse_metadata.py` + `test_worker_subagent_persistence.py` + `test_compose_default_workers.py` + `test_runs_api_endpoints.py` 共 29 测试通过。

### 6.4 修订后测试汇总

```text
test_run_inactivity_watchdog.py               12 passed  (+2)
test_view_image_middleware_context_cleanup.py  19 passed
test_token_budget_middleware.py                 8 passed
test_run_manager.py + rollback + goal_worker 114 passed  (回归)
其他 worker 集成测试                          29 passed  (回归)
─────────────────────────────────────────────────────────
Total                                        182 passed
```

前端 typecheck：✅ | lint：✅（仅预先存在的 3 个 warning）

### 6.5 未处理的中低风险项（留待后续）

| 项 | 风险 | 建议 |
|----|------|------|
| A1 manifest `total_pages` vs `pages` 在 `--pages` 过滤时不一致 | 中（eligibility-screener 不用 `--pages`，当前不触发） | manifest 增加 `rendered_count`，覆盖率用 `len(pages)` |
| C2 watchdog 在 goal continuation 评估期间无心跳 | 中（慢 goal 评估可能误报） | 评估期间发心跳或放宽阈值 |
| D6 未覆盖 `awrap_model_call` async 路径 | 低 | 补 async 用例 |
| C2 watchdog 在 agent 构建期间无心跳 | 低（5min 宽裕） | 首次 `_stream_once` 前不计时 |

---

## 7. Budget 异常分析与校准（2026-07-11 二次修订）

基于会话 `aca54c56-dcda-4d6c-8568-7776fc1d8803` 两个 run 的实测 token 数据，定位 budget 失控根因并校准。

### 7.1 实测数据（来自 `data/deerflow.db` runs 表）

| 维度 | Run 1 (gpt-5-4) | Run 2 (deepseek-v4-pro) |
|------|------|------|
| 状态 | interrupted | success |
| 时长 | 70 min（13:42–14:52） | 10 min（09:11–09:21） |
| 总 token | 4,735,558 | 3,021,765 |
| input 占比 | 96.9% | **97.7%** |
| output 占比 | 3.1% | 2.3% |
| lead : subagent | 49:51 | **16:84** |
| 每次调用平均 input | 152,760 | 177,751 |
| 步数 | - | 199（卡在 P1） |
| budget 硬停 | 未启用 | 触发但陷入循环 |

### 7.2 根因：硬停后 goal-continuation 循环未终止

Run 2 的 checkpoint（step 199）尾部出现 3 次连续 `[TOKEN BUDGET EXCEEDED]`：

```
[42] AIMessage tool_calls=[] finish=stop  input=392,418  "Token budget is tight but I'll continue..."
[43] AIMessage tool_calls=[] finish=stop  input=434,806  "[TOKEN BUDGET EXCEEDED]...400,000"
[44] AIMessage tool_calls=[] finish=stop  input=477,304  "[TOKEN BUDGET EXCEEDED]...400,000"
```

**机制**：`TokenBudgetMiddleware` 硬停时剥离 `tool_calls`、设 `finish_reason=stop`，返回 AIMessage。按 `create_react_agent` 的 `should_continue` 路由，`tool_calls=[]` 应进入 END。但 worker.py 的 goal-continuation `while` 循环在 `_stream_once` 结束后调用 `_prepare_goal_continuation_input`，**若 goal 仍 active，注入 continuation_input 再次 `_stream_once`**。硬停未设置 `abort_event`，故 goal loop 反复驱动新轮次，每轮带更大上下文重超 budget、重硬停，input 从 39 万涨到 47 万，直到 10 分钟超时。

### 7.3 修复：硬停标志打破 goal 循环

**`token_budget_middleware.py`**：
- 新增模块常量 `BUDGET_HARD_STOPPED_KEY = "__budget_hard_stopped"`
- 硬停时调用 `_mark_hard_stopped(runtime)`，在 `runtime.context` 写入 `True` 标志
- 新增 2 个测试：`test_hard_stop_marks_runtime_context`、`test_no_hard_stop_does_not_mark_context`

**`worker.py`**：
- 导入 `BUDGET_HARD_STOPPED_KEY`
- goal-continuation `while` 循环开头检查 `runtime_ctx.get(_BUDGET_HARD_STOPPED_KEY)`，若为 True 则 `break`，不再发起 continuation
- 新增测试 `test_run_agent_stops_goal_loop_after_budget_hard_stop`：验证硬停标志存在时 goal loop 不调用 `_prepare_goal_continuation_input`

### 7.4 Budget 阈值校准

**依据**：
- 健康单轮 input ≈ 150–180K（含 OCR 文本 + extraction JSON 的上下文）
- 事故 run input 占 97.7%--几乎全在重复读取膨胀上下文，非产出
- 原 `max_input_tokens: 400000` 在 input 40 万才触发，留了过多膨胀空间，且硬停循环让 input 持续爬升

**新阈值**（`config.yaml`）：

| 参数 | 旧值 | 新值 | 理由 |
|------|------|------|------|
| `max_tokens` | 600,000 | 800,000 | 总量略放宽，主刹车交给 input 维度 |
| `max_input_tokens` | 400,000 | **300,000** | 主刹车：健康单轮 180K 的 ~1.5 倍，膨胀到 270K(0.9)即硬停 |
| `warn_threshold` | 0.8 | **0.7** | 更早警告（210K input），提示 agent 收尾 |
| `hard_stop_threshold` | 0.95 | **0.9** | 270K input 即硬停，配合 goal 循环终止真正结束 run |

**预期效果**：
- 健康流程（每轮 < 200K input）不受影响
- 上下文膨胀 run 在 input 270K 被硬停，且 goal loop 立即终止，不再循环爬升到 47 万
- 配合 D4 阶段间 summary 压缩，从源头减少 input 膨胀

### 7.5 二次修订测试汇总

```text
test_token_budget_middleware.py             10 passed  (+2)
test_goal_worker.py                          9 passed  (+1)
test_run_inactivity_watchdog.py             12 passed
test_view_image_middleware_context_cleanup.py 19 passed
test_run_worker_rollback.py + run_manager  116 passed  (回归)
─────────────────────────────────────────────────────────
Total                                      156 passed
```

### 7.6 残留风险

| 风险 | 说明 | 监控 |
|------|------|------|
| `max_input_tokens: 300K` 对超大方案误杀 | 极长方案（>200K 字符）健康流程可能触 warn | 观察 warn 频率，必要时上调至 350K |
| goal loop 终止后未产出报告 | 硬停即结束，报告可能未生成 | 这是预期行为--硬停产出"当前已有结果"的最终答案，优于无限循环 |
| D4 summary 压缩未实际验证效果 | 本次仅改阈值+循环，未重跑会话 | 建议重跑 aca54c56 验证 token 是否降至 < 60 万 |

---

## 8. 上下文爆炸优化（2026-07-13 三次修订）

针对 tool calls / read_file 反复读取导致的上下文爆炸（`aca54c56` Run 2 实测 302 万 token，97.7% input），实施 4 阶段优化。对应 [context-explosion-optimization-plan.md](./context-explosion-optimization-plan.md)。

### 8.1 阶段 1：read_file 外部化 + 输出上限（P0，治 G1）

**改动**：
- `config.yaml`：`tool_output.exempt_tools` 从 `[read_file, read_file_tool]` 改为 `[]`，read_file 大输出纳入外部化
- `config.yaml`：`sandbox.read_file_output_max_chars` 从 50000 降至 20000

**机制**：read_file 输出 > 8000 字符时外部化到 `.tool-results/`，context 仅留 2000 头 + 1000 尾预览 + 路径引用；agent 需细节用 `read_file(start_line, end_line)` 按段读。小文件（< 8K，如 OCR 单页 .md）全量返回不受影响。

**测试**：`test_tool_output_read_file_externalize.py` 5 用例（大输出外部化/小文件保留/exempt 回退/磁盘写入/alt 工具名）。

### 8.2 阶段 2：SOUL.md 读取纪律（P0，治 G2/G5）

**改动**：`backend/.deer-flow/agents/eligibility-screener/SOUL.md` 新增原则 11「上下文读取纪律」：
- Phase 间只用 summary，禁止重读前序 Phase 已读文件
- 同一文件同一 run 最多 read_file 一次
- 判定阶段证据只来自 extraction.json
- 按段读不全量读，大文件优先 grep 定位

各 Phase（2.5/3/4/5）前置步骤补充"禁止重读"提示。

### 8.3 阶段 3：summarization 降阈值（P1，治 G3）

**改动**：`config.yaml` `summarization.trigger.value` 从 80000 降至 50000。

**安全验证**：`test_summarization_tool_pair_preservation.py` 4 用例验证 `_find_safe_cutoff_point` 在降阈值后仍保持 AI/Tool 配对（不切断 tool_calls 与对应 ToolMessage）。

### 8.4 阶段 4：read_file 工具去重缓存（P1，治 G2，默认禁用）

**改动**：`backend/packages/harness/deerflow/sandbox/tools.py`
- 新增模块级 per-run 去重缓存：`_read_dedup_cache`（bounded 5000，key=(thread_id, run_id, path, start, end)）
- `read_file_tool`：二次读同 (run, path, range) 返回引用消息而非内容
- `write_file_tool` / `str_replace_tool`：写入成功后失效该文件缓存
- `_read_dedup_is_enabled()`：从 config `read_file_dedup.enabled` 读取，支持测试 monkeypatch

**配置**：`config.yaml` 新增 `read_file_dedup.enabled: false`（**默认禁用**）。按计划决策点 2，待重跑验证阶段 1-3 效果后决定是否开启。

**测试**：`test_read_file_dedup.py` 6 用例（首次读内容/二次读引用/不同范围读内容/write 失效/跨 run 隔离/禁用回退）。

### 8.5 测试汇总

```text
test_tool_output_read_file_externalize.py      5 passed  (新增)
test_read_file_dedup.py                        6 passed  (新增)
test_summarization_tool_pair_preservation.py   4 passed  (新增)
test_read_file_tool_binary.py                  2 passed  (回归)
test_read_before_write_middleware.py          18 passed  (回归)
test_tool_output_budget_middleware.py         48 passed  (回归)
test_sandbox_tools_security.py               177 passed  (回归)
─────────────────────────────────────────────────────────
本轮新增+回归                                 260 passed
前序轮次回归                                  176 passed
ruff check + format                            通过
```

### 8.6 配置变更汇总（config.yaml）

| 配置项 | 旧值 | 新值 | 阶段 |
|--------|------|------|------|
| `tool_output.exempt_tools` | `[read_file, read_file_tool]` | `[]` | 1 |
| `sandbox.read_file_output_max_chars` | 50000 | 20000 | 1 |
| `summarization.trigger.value` | 80000 | 50000 | 3 |
| `read_file_dedup.enabled` | _(无)_ | `false`（新增） | 4 |

### 8.7 待集成验证

按计划决策点 2，阶段 4 默认禁用，需重跑 `aca54c56` 会话验证：
1. 阶段 1-3 效果：总 token 是否 < 60 万、input 占比 < 85%
2. 若达标 -> 阶段 4 保持禁用（作为预留硬约束）
3. 若仍超 -> 开启 `read_file_dedup.enabled: true`

```bash
sqlite3 backend/.deer-flow/data/deerflow.db \
  "SELECT run_id, total_tokens, total_input_tokens, subagent_tokens, status
   FROM runs WHERE thread_id='aca54c56-dcda-4d6c-8568-7776fc1d8803'
   ORDER BY created_at DESC LIMIT 1;"
```

---

## 9. Subagent recursion_limit 修复（2026-07-13 四次修订）

### 9.1 问题

会话 `88832c5a` 的 `quality-control` 与 `data-extractor` subagent 触发 `GraphRecursionError: Recursion limit of 50 reached`，导致 QC/证据提取 task 失败。

### 9.2 根因

`SubagentConfig.max_turns` 默认 50（`subagents/config.py:49`），作为 `recursion_limit` 传入 subagent graph（`executor.py:557`）。config.yaml 中仅 `general-purpose` 显式配了 `max_turns: 250`，其余 subagent（data-extractor / quality-control / literature-analyzer / report-writer / report-writing）全用默认 50。

- `data-extractor`：多患者多文档证据提取，需多轮 read_file + view_image，50 轮不够
- `quality-control`：校验 criteria_parsed/judgments 需多轮读取比对，50 轮不够（日志可见单次 read_file externalized 20000 字符，多轮后触顶）

### 9.3 修复（config.yaml）

| subagent | 旧 max_turns | 新 max_turns | 理由 |
|----------|-------------|-------------|------|
| `data-extractor` | 50（默认） | **150** | 多患者多文档证据提取 |
| `quality-control` | 50（默认） | **100** | 多轮读取比对校验 |
| `literature-analyzer` | 50（默认） | **100** | 文献分析 |
| `report-writing` | 50（默认） | **100** | 报告生成 |
| `report-writer` | 50（默认） | **100** | 报告生成（eligibility-screener 用此名） |
| `general-purpose` | 250 | 250（不变） | OCR 分片等 |

### 9.4 加载链路确认

`registry.py:93-96` 从 config.yaml per-agent override 读取 `max_turns` → 写入 `SubagentConfig.max_turns` → `executor.py:557` 用作 `recursion_limit`。配置热加载，重启 gateway 后生效。

### 9.5 验证

- 后端测试 46 passed（token_budget / goal_worker / watchdog 无回归）
- 配置段验证：5 个 subagent 均已配置 max_turns

### 9.6 备注

`88832c5a` run 最终 `success`（384 万 token）但 QC task 因 recursion 失败被跳过，报告可能缺 QC 环节。重启 gateway 后重跑应正常完成 QC。
