# /criteria-parser 子代理 300s 超时优化方案（代码级复核版）

> 来源会话：`31c168d2-f609-48fc-abf2-c5090552e267`（eligibility-screener，2026-07-20）
>
> 参考分析：`~/.claude/plans/ethereal-dazzling-ocean.md`（原始根因初判）
>
> 关联计划：[subagent-timeout-watchdog-optimization-plan.md](./subagent-timeout-watchdog-optimization-plan.md)
> （该计划从"子代理卡死 LLM 网关 + run watchdog 误杀"角度覆盖了强制中断机制；本计划聚焦
> "输出生成过长 + 技能注入膨胀"这一独立触发面，两者互补，实施时需交叉对齐避免冲突。）
>
> 状态：待评审 / 待实施

---

## 1. 背景

lead agent 通过 `/criteria-parser` 斜杠激活后，把"入排标准四分类解析"任务委派给 `general-purpose`
子代理。子代理读取入排原文（14KB）与 schema 示例（7.5KB）后，在生成完整 `criteria_parsed.json`
的那次 LLM 调用中被 **300s inactivity 看门狗**强制中断，抛出 `CancelledError`，子任务失败。

## 2. 根因结论（先行）

**触发点**：`general-purpose` 子代理的 **inactivity 看门狗（`inactivity_timeout_seconds=300`，默认值，
未被覆盖）** 在第二次 LLM 调用（读完两文件后的生成步）执行期间触发，向挂起在 `await model.ainvoke`
的协程注入 `CancelledError`。

**它不是**：总超时 `timeout_seconds`（默认 900）、网络故障、或模型侧报错。

**真正瓶颈**：**单轮超大 JSON 输出的生成时长**越过了 300s 静默窗口。技能全量注入造成的输入膨胀
是**次要加重因素**，而非主因——这是对参考报告（`ethereal-dazzling-ocean.md`）侧重点的关键校正。

## 3. 证据链（LangSmith trace + 代码）

### 3.1 命中的是 300s inactivity 看门狗

| 事实 | 证据位置 |
|------|----------|
| `inactivity_timeout_seconds` 默认 300、`timeout_seconds` 默认 900 | `subagents/config.py` `SubagentConfig` |
| `general-purpose` 未覆盖这两个值（`skills=None`、无 timeout 字段） | `subagents/builtins/general_purpose.py` `GENERAL_PURPOSE_CONFIG` |
| 仅当 config.yaml 显式配置才覆盖默认；示例配置该项为注释 | `subagents/registry.py`（per-agent > 全局 > config 自带）、`config.example.yaml:1142` |
| 看门狗实现：`await asyncio.wait_for(aiter.__anext__(), timeout=inactivity_timeout)`，超时置 `TIMED_OUT` 并 `astream.aclose()` | `subagents/executor.py:_aexecute` |
| `stream_mode="values"`：一个 chunk 仅在一个 super-step（LLM 调用 + 工具）**结束时**产出 | `executor.py` `agent.astream(..., stream_mode="values")` |

### 3.2 卡点在 LLM 响应头接收阶段（等价于"生成耗时 >300s"）

- trace 报错栈末端：`model_.ainvoke → ... → _receive_response_headers → _receive_event → read`，
  即请求已发出、正在等服务端返回。
- langchain_openai 走**非流式** `with_raw_response.create`；非流式补全服务端在**整段生成完成后**
  才返回响应头。因此"卡在收响应头 300s" ≡ "模型生成这段输出用了 >300s"，期间不产出任何 super-step chunk。
- trace 元数据 `langgraph_step: 8`、`langgraph_node: "model"`：正是两次 `read_file`
  （均 `status: success`）之后的那次模型生成。

### 3.3 输出量巨大是生成过长的直接原因

- criteria-parser 技能要求一次性产出完整 `criteria_parsed.json`，schema 示例"子条件总数"约 **51 条**。
- 每个"可从病例获取"条件**强制**填非空 `同义词` + `证据位置`（硬规则），单条体量大。
- 该次生成本质是一个 ~15–25K token 的单轮 `write_file` 工具调用参数，非流式下极易越过 300s。

### 3.4 技能注入膨胀确存在，但不是 300s 主因

- 系统消息内联了全部 **5 个**技能完整 SKILL.md（criteria-parser / eligibility-judgment /
  patient-separator / pdf-image-extractor / screening-report-generator）。
- 对应 `executor.py:_build_initial_state` → `_load_skill_messages(skills)`，且 general-purpose
  `config.skills=None`（继承全部已启用技能）。
- **但**第一次模型调用 `prompt_tokens=22449` 且**正常返回**（1.6s、成功发起 read_file）。
  说明 22–32K 输入预填充仅秒级延迟，**吃掉 300s 的是输出生成，不是输入体积**。

### 3.5 对参考报告的校正

参考报告将根因主要归为"技能全量注入 → 上下文膨胀 → 处理 >300s"。方向正确（冗余确应削减），但**不完整**：
- 触发点是**输出生成时长**越过静默窗口，而非输入体积。
- 即便按其"优化 1"做渐进式加载、输入砍 ~70%，子代理**仍要一次性生成 51 条 JSON**，遇更大方案照样超时。
- 故"渐进式加载"是必要**降本**手段，但**非根治**。根治须从"输出分片 + 看门狗进度感知"两侧入手。

---

## 4. 优化方案（按有效性排序）

### P0-A｜看门狗进度感知（治本，针对触发机制）

**问题**：看门狗以 `stream_mode="values"` 的 super-step chunk 为心跳，一次健康长生成与一次真正挂死
无法区分，导致"慢但在动"的生成被误杀。

**改动**：在 `executor.py:_aexecute` 的 astream 消费中叠加 token 级进度信号，使 inactivity 计时对
真实 token 增量重置。两种落地方式二选一：
1. 同时消费 `stream_mode=["values", "messages"]`，对 `messages` 增量重置计时器（推荐，改动局部）；
2. 用 `astream_events(version="v2")`，对 `on_chat_model_stream` 事件重置计时器。

**验收**：一次持续 >300s 但持续产出 token 的生成不再被 TIMED_OUT；真正静默 >300s 仍按时中断。

**涉及文件**：`backend/packages/harness/deerflow/subagents/executor.py`（`_aexecute` astream 循环）。

**风险**：中。改变流消费模式，需回归 `capture_new_step_messages` 的去重/游标逻辑与 token 采集器。

---

### P0-B｜强制分片输出（治本，针对根因）

**问题**：criteria-parser 目前允许"条目 >30 可分两轮 write_file"（可选），实际单方案 51 条时子代理
仍可能一次性生成全量 JSON。

**改动**（技能规则，无需改代码）：`skills/**/criteria-parser/SKILL.md`
1. 将"可分两轮"从**可选**改为**强制**，并下调阈值：每 **10–15 条**子条件一次 `write_file(append=True)`。
2. 明确产出节奏：首个 `write_file` 建文件（含顶层元数据 + 第一批四分类条目），随后
   `write_file(append=True)` 逐批扩展，最后一批补 `汇总统计` + `描述索引`。
3. 与 general-purpose 系统提示中已有的 `<file_editing_workflow>`（分段写、避免单次超大写入）对齐。

**收益**：每次分片写入结束都是一个 super-step → 产出 chunk → 重置看门狗；同时缩短单轮生成体量。
即使 P0-A 未落地也能显著降低超时概率。

**验收**：重跑同方案时，第二阶段被拆为多次 `write_file`，各 chunk 间隔 <300s。

**涉及文件**：`skills/public/`（或 `skills/custom/`）下 `criteria-parser/SKILL.md`。

**风险**：低。仅提示词调整，不改执行引擎。

---

### P0-C｜上调 general-purpose inactivity 窗口（安全网，立即可做）

**改动**：`config.yaml` 的 `subagents.agents` 下为 general-purpose 增加覆盖：

```yaml
subagents:
  agents:
    general-purpose:
      inactivity_timeout_seconds: 600   # 300 -> 600，兜底长生成
      timeout_seconds: 1800             # 与全局默认对齐，保证总窗口 > 单步窗口
```

同步在 `config.example.yaml`（约 1135–1142 行示例块）补注释说明。

**风险**：低。仅参数，不改逻辑。作为 P0-A/B 落地前的过渡兜底。

**涉及文件**：`config.yaml`、`config.example.yaml`。

---

### P1｜子代理技能渐进式加载（降本 / 加固，对齐参考报告优化 1）

**改动**：
1. `executor.py:_build_initial_state` 不再对全部技能调 `_load_skill_messages`，改为注入技能**元数据摘要**
   （name + description + location），格式对齐 lead agent 的 `<available_skills>`，并附"渐进式加载"指令，
   引导子代理按需 `read_file` 加载所需 SKILL.md。
2. 新增 `_build_skill_metadata_section(skills)` 生成该元数据块。
3. 保留 `_load_skill_messages`，新增 `eager: bool = False` 参数：确需全量技能内容的子代理走 `eager=True`
   保持旧行为（向后兼容）。

**收益**：子代理 SystemMessage token 预期下降 ~70%（约 20K → ~6–8K），缩短预填充、释放时间预算。
**定位**：降本/加固，**非**超时根治（不改变输出生成体量）。

**验收**：单元测试校验元数据块格式与 lead agent 一致；集成测试验证子代理能按需 `read_file` 加载技能并完成任务。

**涉及文件**：`executor.py`（`_build_initial_state`、`_load_skill_messages`）、
`subagents/builtins/general_purpose.py`（system_prompt 补渐进式加载指引）。

**风险**：中。改变子代理获取技能内容的方式，需验证 slash 激活任务下子代理能正确加载。

---

### P1｜slash 激活技能精准透传（评估后不实施，风险 > 收益）

> **实施决定：不实施 blanket 收窄。** 复核后发现该项在本工作流下弊大于利：

1. `SkillActivationMiddleware` 将激活技能以隐藏 HumanMessage 注入消息流，**不**向 runtime
   context 暴露激活技能名；`task_tool` 读到的 `metadata.available_skills` 是 lead agent 的
   **全部**已启用技能集合（经 `_merge_skill_allowlists` 与子代理白名单取交）。
2. 若改为"把 run context 里记录的 slash 技能名作为子代理唯一白名单"，因 context 为
   **run 级**（一个用户回合内多次委派共享），会导致：lead 在本回合激活 `/criteria-parser`
   后，同回合内对 OCR / 患者拆分的**非 slash 委派**被错误收窄为 criteria-parser，
   **切断 eligibility-screener 的多技能流水线**（patient-separator / pdf-image-extractor /
   eligibility-judgment / screening-report-generator 对子代理不可见）。
3. 若改为"逐委派解析 task 文本里的 `/skill`"，观测到的 task 文本 `请按 /criteria-parser 技能规则…`
   斜杠位于句中而非行首，`parse_slash_skill_reference` 只认行首命令 → 对本例为空操作，无收益。

**根本原因**：P1 渐进式加载落地后，技能元数据（name+description+location，每技能仅几行）已足够
轻量，"从 5 个收窄到 1 个"的 token 收益微不足道，却要承担流水线被切断的风险。**故本项取消**。
如后续确有需要，应改为"per-delegation 显式白名单"由调用方在 `task` 的入参上明确指定，而非依赖
run 级隐式状态。

---

## 5. 优先级与实施顺序

| 优先级 | 项 | 作用 | 风险 | 建议 |
|--------|----|------|------|------|
| **P0** | P0-C 上调 inactivity 窗口 | 安全网（不治根因） | 低 | 立即执行 |
| **P0** | P0-B 强制分片输出 | 治本（削单轮输出） | 低 | 尽快，纯提示词 |
| **P0** | P0-A 看门狗进度感知 | 治本（消除误判） | 中 | 需充分测试 |
| **P1** | P1 渐进式技能加载 | 降本/加固 | 中 | P0 后跟进 |
| **P1** | P1 slash 精准透传 | 精准优化 | 低-中 | 依赖渐进式加载 |

建议先落 **P0-C（兜底）+ P0-B（削输出）**，再做 **P0-A（进度感知）**，最后 P1 两项降本。

---

## 6. 验证方案

1. **复现基线**：用同一方案（`XS-03-II201`，51 子条件）重跑 `/criteria-parser`，确认修复前 300s 超时。
2. **P0-B 验证**：观察第二阶段是否被拆成多次 `write_file(append=True)`，各 chunk 间隔 <300s。
3. **P0-A 单元/集成**：构造一次持续 >300s 但持续产出 token 的生成，确认不再被 TIMED_OUT；
   构造真正静默 >300s，确认仍按时中断。
4. **P1 token 对比**：对比修复前后子代理 SystemMessage token 数，预期下降 ~70%。
5. **压力用例**：>60 条子条件的大方案，验证不再超时且输出完整（`汇总统计`/`描述索引` 齐全）。
6. **回归**：运行 `backend/tests/` 现有子代理/超时相关测试套件，确保无破坏性变更。
7. **交叉对齐**：与 `subagent-timeout-watchdog-optimization-plan.md` 的强制中断改动联调，
   确认 inactivity 看门狗、run 级 watchdog、总 timeout 三层不冲突。

---

## 7. 关键代码/配置索引

| 项 | 位置 |
|----|------|
| 子代理超时默认值（900/300） | `backend/packages/harness/deerflow/subagents/config.py` `SubagentConfig` |
| general-purpose 配置（skills=None、无超时覆盖） | `backend/packages/harness/deerflow/subagents/builtins/general_purpose.py` |
| 超时覆盖解析（per-agent > 全局 > 自带） | `backend/packages/harness/deerflow/subagents/registry.py` |
| 技能全量注入 | `executor.py:_build_initial_state` → `_load_skill_messages` |
| inactivity 看门狗 + values 流消费 | `executor.py:_aexecute`（`asyncio.wait_for(aiter.__anext__(), timeout=inactivity_timeout)`） |
| 强制中断（跨线程 cancel） | `executor.py:_force_cancel_subagent_stream`、`run_task` |
| 技能分片输出规则 | `skills/**/criteria-parser/SKILL.md`（执行流程 步骤 1） |
| 全局超时/看门狗示例 | `config.example.yaml`（`run_inactivity_timeout_seconds:41`、`timeout_seconds`/`inactivity_timeout_seconds:1135-1142`） |


---

## 8. 实施状态（2026-07-20）

| 项 | 状态 | 落地内容 |
|----|------|----------|
| **P0-C** 上调 inactivity 窗口 | ✅ 已实施 | `config.yaml` `subagents.agents.general-purpose.inactivity_timeout_seconds: 600`；`config.example.yaml` 早已在注释块示范该值。 |
| **P0-B** 强制分片输出 | ✅ 已实施 | `skills/custom/criteria-parser/SKILL.md` 步骤 1 改为**硬规则**：首批 `write_file` 建文件，后续每 10–15 条 `write_file(append=True)`，末批闭合并追加汇总统计/描述索引。 |
| **P0-A** 看门狗进度感知 | ✅ 已实施 | `executor.py:_aexecute` 改用 `stream_mode=["values","messages"]`，解包 `(mode,payload)`；`"messages"` token 增量作为心跳重置 inactivity 计时（`asyncio.wait_for` 对每个 item 重置），`"values"` 仍做消息捕获。LangGraph 请求 messages 模式会令 `BaseChatModel._should_stream` 为真，模型 token 级流式，从而"慢但在动"的生成不再被误杀。新增 `TestSubagentProgressAwareWatchdog`（3 用例）。 |
| **P1** 渐进式技能加载 | ✅ 已实施 | 新增 `SubagentConfig.eager_skills: bool = False`。`executor.py:_build_initial_state` 默认注入 `_build_skill_metadata_section`（`<available_skills>` name+description+location + 渐进式加载指令），子代理按需 `read_file`；`eager_skills=True` 走旧的全量 `_load_skill_messages`。新增 eager-mode 测试，更新原 consolidation 测试。 |
| **P1** slash 精准透传 | ❌ 取消 | 见 §4：run 级收窄会切断 eligibility-screener 多技能流水线；渐进式加载后收益微不足道。 |

**验证**：`tests/test_subagent_executor.py` 相关子集全绿（watchdog/progress/skill/initial_state/eager/aexecute/forced-cancel）。
