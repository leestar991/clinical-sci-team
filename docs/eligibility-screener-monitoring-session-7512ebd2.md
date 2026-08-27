# 会话 7512ebd2 执行分析：空摘要净删除 → 判定阶段 6 次尝试零产物

> 会话：`http://localhost:3000/workspace/agents/eligibility-screener/chats/7512ebd2-eda6-4983-8978-ef18488ff8cf`
>
> thread_id：`7512ebd2-eda6-4983-8978-ef18488ff8cf`
>
> 分析时间：2026-08-13（UTC 05:57 快照，**会话仍在运行**）
>
> 数据来源：Postgres `run_events` / `runs` 表、`backend/scripts/analyze_eligibility_run.py`、磁盘 workspace 产物实查、`checkpoint_writes`（反解 lead 真实 `task` 参数）
>
> 状态：**已改代码 + 配置**，见 §5。
>
> 关联：[`eligibility-screener-subagent-context-and-artifact-gate-changelog.md`](eligibility-screener-subagent-context-and-artifact-gate-changelog.md)（88df83a8 的同类净删除，另一条路径）、[`eligibility-screener-monitoring-session-d393714d.md`](eligibility-screener-monitoring-session-d393714d.md)

---

## 0. 结论

判定阶段 **76 分钟墙钟、9.87M token、6 次子代理尝试、零产物**。全部失败于同一道闸：

```
Error: Subagent reported completion but required outputs missing/empty.
missing=['/mnt/user-data/workspace/patients/MCRC-2150006/judgments_draft_MCRC-2150006_EX.json']
```

**产物闸的报错是对的，`expected_outputs` 声明也是对的**（从 checkpoint 反解出的 lead `task` 调用参数逐字正确，8.2KB 委派模板完整、schema 路径与四道闸命令一条不缺）。真正的故障在闸的上游：**子代理在执行途中丢掉了自己的任务书和刚读过的 schema**，于是自创结构、自创文件名。

三个独立缺陷叠加，缺一不可：

| # | 缺陷 | 位置 | 效果 |
|---|---|---|---|
| **A** | 空摘要仍执行「删消息 + 写摘要」的交换 | `summarization_middleware._maybe_summarize` | **净删除**：69 次子代理压缩里 **33 次摘要为空（48%）**，每次带走 2–22 条消息 |
| **B** | dedup 引用指向已被压缩删掉的消息 | `read_file_dedup_middleware._handle_read` | 子代理再也拿不回 `judgment-schema.md`，原话「the dedup system thinks I read these in this run, but I did not」 |
| **C** | 重试用逐字相同的 prompt | `task_tool` 重试分支 | 重试不知道上次为什么被拒，重复同一个替换、撞同一道闸 |

A 是引擎，B 让错误不可自愈，C 让代价 ×2。

---

## 1. 会话拓扑

三个 run，同一 thread：

| run | 状态 | token | 内容 |
|---|---|---|---|
| `549fdbe0` | success | 3.47M | Phase 1 预处理，停在 `ask_clarification` 问 OCR 模式 |
| `b56a97a3` | success | 10.12M | Phase 2 解析 + QC 三轮 + 修订两轮，停在 `ask_clarification` 问 EX-3-2 裁决 |
| `bce8cce8` | **running** | 5.25M+ | 用户裁决「带病放行」后收尾 + Phase 3 判定（本次故障所在） |

⚠️ `bce8cce8` 非终态，其 token 列在收尾时才回写，快照会低估。以 `subagent.end` 事件派生值为准（这是 [`2d628340`](eligibility-screener-monitoring-session-2d628340.md) 记过的 running-run 陷阱）。同理，首次跑 `analyze_eligibility_run.py` 时它报告判定任务 `compactions=0`，**那是快照太早**——按事件重算实为 4 次、retry1 为 10 次。

### 两个阶段的成本对比

| 阶段 | 任务数 | token | 串行分钟 | 产物 |
|---|---|---|---|---|
| 解析 / QC / 修订 | 11 | 7.09M | 37.9 | 全部就绪 |
| **判定** | 10 | **9.87M** | **50.3** | **0** |

判定阶段墙钟 76.1 分钟（04:40 → 05:57，仍在跑），花掉整场 token 的一半多，交付为零。

---

## 2. 故障链（逐步实证）

### 2.1 派发是正确的

lead 的 `task` 调用参数无法从 `run_events` 直接读到（`llm.ai.response` 事件不落 `tool_calls` 的 args），需从 `checkpoint_writes` 用 `JsonPlusSerializer` 反解。反解结果：

```
desc=MCRC-2150006 入选轨逐条判定
expected_outputs=['/mnt/user-data/workspace/patients/MCRC-2150006/judgments_draft_MCRC-2150006_IN.json']
subagent_type=general-purpose
prompt=8381 字符（judge-delegation.md 模板逐字展开，占位符全部填好）
```

prompt 里该有的全都在：schema 契约路径、`documents` 键的硬规则、四道闸的完整命令、`§原则N` 的指路。**这不是「lead 压缩了模板」那类历史故障**（对比 `9a83ccc9`）。

### 2.2 子代理开局读对了，然后被删掉

以最后一次重派 `call_00_T8xY6dELnOCf83Bk6lj63914` 为例，`message_index=1` 的第一轮就并行读了六个正确的文件：

```
read_file judgment-schema.md
read_file schema_example.json
read_file judgment-principles.md
read_file criteria_judge_IN.json
read_file ocr_records.md ×2
```

随后 4 次压缩、其中 3 次 `summary_chars=0`。到 `seq 1299` 落盘时，它写的是：

```
path: /mnt/user-data/workspace/eligibility_judgment_IN_MCRC-2150006.json   ← 自创路径
content: {"患者": ..., "轨": ..., "判定": {"IN-2-1": {"子条件":..., "结论":"符合入选", "依据":..., "证据":[...]}}}
```

而 schema 要求的是 `patient_id` / `judgment_date` / `documents.{doc}.judgments.{cid}.{conclusion,reason,evidence}`。它把**输入包 `criteria_judge_*.json` 的「四分类」形态当成了输出模板**。

### 2.3 dedup 让它无法自救

`call_00_...5434-retry1` 的读取序列是这条链的铁证：

| idx | 动作 | 结果 |
|---|---|---|
| 1 | `read_file judgment-schema.md` | 正文（首读，入 dedup 缓存） |
| — | 压缩 ×N，首读消息被删 | `summary_chars=0` 五次 |
| 50 | 再读 `judgment-schema.md` | `[read_file dedup] ... Scroll back to that earlier read` |
| 52 | 子代理原话 | *"The dedup notification says I already read judgment-schema.md, but I actually haven't."* |
| 53 | 再读 `schema_example.json` | 同样被挡 |
| 109 | 再读 OCR | 同样被挡 |

引用文案让它「翻回上一次读取」，可那条消息已被压缩删除。`op: get` 也取不回一份 Markdown 规范。两个中间件各自正确，**组合起来给出一条悬空指针**，而 `read_file` 是它取回内容的唯一手段。

### 2.4 任务身份丢失

同一个 retry1 任务（声明产物是 **IN** 初稿）在 idx 34 之后整体跑成了 **EX** 轨：反复读 `criteria_judge_EX.json`（违反 prompt 里「⛔ 禁止读写另一轨的任何产物」），idx 182 写出 `eligibility_judgment_EX_MCRC-2150006.json`，idx 186 汇报「排除标准 EX 轨判定结论」。18.8 分钟、4.02M token，做的是**别人的任务**。

它在 idx 46–50 之间连着说：

- *"Since the task appears to be that I need to..."*
- *"Let me understand what my actual task is."*
- *"The task summary said 「本消息未给出任何 条件ID，无未核验/未定稿的条件ID可列出；需上游明确该子任务要核验的条件ID」"*
- *"I've been given a single-agent context (no `task` subagent dispatch tool available to me)"*

最后那句尤其能说明状态丢到什么程度——它已经不认为自己是个子代理了。

### 2.5 重试重复同一个错误

`task_tool` 重试分支原先是 `executor.execute_async(prompt, task_id=retry_task_id)`，**prompt 逐字不变**。子代理上下文隔离，没有任何别的通道能知道上次为什么被拒。于是四次尝试做了四次同样的替换。

### 2.6 lead 的处置是对的，但代价高

lead 自己诊断出来了（`seq 1162`–`1173`）：

> 子代理写了错误文件名：IN 轨写了 `judgment_IN.md`（markdown 而非 JSON），EX 轨写了 `judgments_EX.json`（文件名错误）
> 根因清晰：两轨子代理都把**输入包 `criteria_judge_*.json` 的「四分类」结构当成了输出模板**

它花了 5 轮对话 + 3 次 `bash` 做这次排查——**而这份信息产物闸自己一次 `list_dir` 就拿得到**。清理时它只清了 `patients/` 目录，`workspace/` 根下的 `eligibility_judgment_EX_MCRC-2150006.json` 留着，下一轮子代理读到它、拿它当模板照抄了一遍（`seq 1310`）。

---

## 3. 空摘要为什么会发生

不是偶发。空摘要与被压缩的消息数无关（**少至 2 条也空**），且**只发生在子代理**（lead 侧 14 次压缩 0 次空）。两侧唯一的量化差别：

| | 摘要模型 | max_tokens | `trim_tokens_to_summarize` | 输入:输出 | 空摘要率 |
|---|---|---|---|---|---|
| lead | deepseek-v4-flash | 8192 | 80000 | 10:1 | 0/14 |
| **子代理** | deepseek-v4-flash | 8192 | **120000** | **15:1** | **33/69** |

`max_tokens: 8192` 是所有输入规模共用的固定预算，reasoning 还要先从里面扣。要求模型在 8192 token 内为 120k token 的输入交出一份「任务交接单」，结果不是「摘要变短」，而是**一个字都没有**。

而旧代码只挡 `None`：

```python
summary = self._summarize_with(...)   # 返回 response.text.strip() → ""
if summary is None:                   # "" 不是 None，守卫放行
    return None
return {"messages": [RemoveMessage(REMOVE_ALL_MESSAGES), *preserved], "summary_text": summary}
```

交换照做：消息删了、`summary_text` 覆写成空、`_maybe_inject_summary` 因通道为空跳过注入。**净删除**——与 88df83a8 修掉的那个（摘要写进没人读的通道）同一类，只是路径不同。

⚠️ 顺带查实：`config.yaml` 里的 `min_summary_chars` / `cooldown_calls` / `min_messages_to_summarize` / `min_summary_body_chars` / `preserve_recent_skill_*` **共 7 个键在本分支代码里没有对应字段**，pydantic 直接忽略。`min_summary_chars: 0` 看起来像「守卫已关」，实际是「守卫不存在」——没有任何代码读它。

---

## 4. 其它可量化的浪费

- **lead 空 AI 步 61/115（53%）**：每两个 AI 步就有一个产空内容仍付 input token。
- **子代理 `bash` 181 次 vs `apply_json_patches` 9 次**：大量 `python3 -c "import json; ..."` 在**探查**输入包结构——这正是子代理在丢失上下文后反复重建认知的表现，`bash` 次数是上下文丢失的间接指标。
- **重复读**：`ocr_records.md`（筛选期检查）单 task 读 11 次、（筛选期病历）9 次；`criteria_judge_EX.json` 3 次。dedup 挡下的那些反而是**应该放行**的。
- **`range_overlap` 2851/40387 行（7%）**：分段读取的重叠不算大头，不是本次瓶颈。

---

## 5. 已落地的修复

| # | 改动 | 文件 | 判据 |
|---|---|---|---|
| A | 空摘要不得换走历史：新增 `_summary_is_usable`，空白摘要 → 放弃本轮压缩（sync + async 两条路径） | `summarization_middleware.py` | `TestEmptySummaryNeverTradesAwayHistory`（含「不覆盖已有摘要」「不记 summarize 事件」） |
| A' | 空摘要可诊断：`_summary_text_of` 记录 `finish_reason` / `output_tokens` / reasoning 字数，区分三种成因 | 同上 | `TestEmptySummaryIsDiagnosable` |
| A'' | 子代理 `trim_tokens_to_summarize` 120000 → 40000，与 8192 输出上限相称（≈5:1，对齐 lead） | `config.yaml` | 下次会话按 `summary_chars=0` 次数单因子归因 |
| B | dedup 引用必须指向拿得到的正文：按 `tool_call_id` 查 transcript，查得到（或已外部化落盘）才给引用；**查得到消息集合但首读不在其中 → 放行正文**；无 `state`/无 `messages` → 保持原行为 | `read_file_dedup_middleware.py` | `TestReferenceRequiresAReachablePayload`（含「不把未知读成已丢」「放行不失效缓存」） |
| C | 重试携带失败原因：`_retry_prompt` 在原 prompt 后追加上次被拒的机械原因，**不改写任务书** | `task_tool.py` | `TestRetryPromptCarriesTheFailureReason` |
| D | 产物闸报错列出目标目录里**实际存在**的文件名，让「改名」与「没做」一眼可分 | `subagents/executor.py` | `TestFailureNamesTheStrayArtifacts`（含 host 路径不外泄、空目录不挂空清单、list_dir 失败不掩盖真错） |

`docs/middleware-execution-flow.md` 同步记录 A / B 两个中间件交互约束。

改动相关测试 274 项全绿；全量套件（排除 e2e/live）6103 passed，20 项既有失败（auth / stream_bridge / live，与本次改动无文件交集，失败源自本机环境）。

---

## 6. 尚未处理

1. **空摘要的根因只做了配置缓解**。40000 是按比例推的，需下次会话验证；若仍有空摘要，下一步是给摘要模型单独抬 `max_tokens`，或换一个非 reasoning 模型做摘要。⛔ 一次只动一个。
2. **lead 空 AI 步 53%** 未处理，独立问题。
3. **错误产物残留会污染下一轮**：lead 清理时只清了 `patients/`，`workspace/` 根下的自创产物被下一个子代理当模板抄走。判定委派模板里应加一条「⛔ 不得把 workspace 下任何已有 `*judgment*.json` 当结构参考，结构只以 `schema_example.json` 为准」。
4. **`analyze_eligibility_run.py` 对 running run 的 `compactions` 仍会报 0**：它按 run 行快照取，未回落到事件重算。本次靠手工重算才发现 69 次压缩。
