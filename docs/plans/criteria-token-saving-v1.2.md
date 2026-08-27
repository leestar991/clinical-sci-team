# Eligibility-Screener Token 优化执行计划 (v1.2 — 收窄范围 · 低侵入)

> **版本关系**:本文件是 [`criteria-token-saving-v1.1.md`](./criteria-token-saving-v1.1.md) 的**执行版**,
> 不替换 v1.1 的现状分析与基线数据。v1.2 只做两件事:
> ① **收窄范围**——只落地 Task 1 / Task 2 / Task 5,Task 6 重新定义,Task 3 / 4 / 7 / 8 **本轮不做**;
> ② **降低侵入性**——凡会改变现有运行时行为的改动一律 opt-in(默认关闭),新增能力一律**加法**(新文件/新工具),
> 不改既有调用链的默认路径。
>
> v1.1 的 Global Constraints、TDD 纪律、质量闸标准继续有效。

**Goal(本轮):** 消除子代理五技能全文继承(约 16.2M 固定重复 token,占基线 47%),建立可复现的用量度量,
补齐版本感知读取去重与批量图片 OCR 能力;**不引入 typed workflow、不改 lead/子代理编排结构**。

---

## 0. 范围决策与理由

| Task | v1.1 原范围 | v1.2 决策 | 理由 |
|---|---|---|---|
| **Task 1** 用量度量 | path A / path B 二选一 | **做,只走 path A** | path A 纯新增脚本,零生产代码改动;path B 要改 `subagent.end` 事件 schema,侵入性不必要 |
| **Task 2** 技能收窄 | 配置 + registry/task_tool 改动 + 审计事件 | **做,收窄为「纯配置 + 测试」** | `SubagentConfig.skills` 与 `get_skills_for()` 已存在且已被 registry 应用,**无需改任何生产代码**;审计事件 `loaded_skill_names` 属可选观测,本轮不做 |
| **Task 5** 读取去重 + 批量 patch | 5 个子项全做 | **做,拆成必做 / opt-in 两档** | 见下表 |
| **Task 6** OCR 路径 | 整文档解析 + 改 SOUL | **改为新增 `parse_image_batch` 工具** | 纯加法:新工具 + 新测试,不动 `parse_document`,不改 SOUL 既有编排 |
| Task 3 typed state | 新增 ThreadState channel + reducer | **本轮不做** | 需改 `ThreadState`/`durable_context_middleware`,影响所有 agent 的 checkpoint 反序列化 |
| Task 4 子代理预算 | 新 schema + 接入子代理中间件链 | **本轮不做** | 需改 `build_subagent_runtime_middlewares`,影响所有子代理执行 |
| Task 7 判定收敛 | 结构化 judgment + QC patches | **本轮不做**(Step 5 已完成) | 「删除独立 reasons 阶段」已于 2026-08-04 落地(见 `eligibility-screener-silent-failure-gates-and-token-changelog.md`);其余依赖 Task 5 的 `apply_json_patches` |
| Task 8 typed DAG | 新 workflows 包 + 运行时 FF | **本轮不做** | v1.1 自身规定「应单独立项评审」,且 `workflows/` 目录、运行时 FF 机制均需从零建 |

### Task 5 的两档拆分

| 子项 | 档位 | 侵入性 |
|---|---|---|
| 订正 `eligibility-screener-fix-changelog.md` 失真声明 | **必做** | 纯文档 |
| 收紧 `str_replace` 多处出现拒绝 | **必做** | 小,且是**修复文档与行为矛盾**(docstring 已声明 "must appear exactly once",代码却静默替换第一处) |
| `read_file_dedup` config schema + `config.example.yaml` | **必做** | 纯声明,默认 `enabled: false` |
| `read_file_dedup` middleware 运行时 | **做,默认关闭** | 新文件;不开启则行为与现在完全一致 |
| `apply_json_patches` 工具 | **做,需显式注册才生效** | 新工具;不注册进 `tools:` 则任何 agent 都看不到 |
| grep 参数预检(v1.1 Step 5) | **不做** | 与 token 优化无直接因果,留待单独评审 |

---

## 1. 现状核实(2026-08-06 实测,全部成立)

| v1.1 断言 | 核实结果 |
|---|---|
| 空 summary 数据丢失 bug | ✅ `_summarize_with` 返回 `response.text.strip()`;模型返回空串时 `.strip()` 得 `""`,`if summary is None` 拦不住 → 同时清空旧 `summary_text` 与全部消息。**本轮不修**(属 Task 3 范围),但已确认为真,单独跟踪 |
| changelog 失真 | ✅ 更严重:`_read_dedup_cache`、`_read_dedup_is_enabled()`、`test_read_file_dedup.py`(声称 6 用例)全仓零命中 |
| `SubagentConfig.skills` 已存在 | ✅ `subagents/config.py:37`;`get_skills_for()` 在 `config/subagents_config.py:130`;registry 在 `registry.py:108` 应用 |
| `skills=[]` 使 `parse_document` 回归 | ✅ `tool_policy.py:41-43` `allowed is None` 时 `return tools` 不过滤 |
| `SubagentsAppConfig` 无 `token_budget` | ✅ 零命中(Task 4 本轮不做) |
| `str_replace` 不校验出现次数 | ✅ `content.replace(old_str, new_str, 1)`,与自身 docstring 矛盾 |
| `workflows/` 不存在 | ✅ (Task 8 本轮不做) |
| 基线会话可用 | ✅ thread `4d1f95b4-…` 目录在;`database.backend: postgres` |
| TextIn 图片 OCR 走 `pdf_to_markdown` | ✅ `client.py:16-22` 已实测:无 `image_to_markdown` 机器人(code=40007),`pdf_to_markdown` 直接接受图片(PNG → code=200, total_page_number=1) |

**v1.1 笔误订正**:Task 2 引用的 `backend/tests/test_subagent_registry.py` 不存在;实际对应
`backend/tests/test_subagent_skills_config.py`。

---

## 2. Task 1:可复现的逐子代理用量度量(path A,零生产代码改动)

**Files:**
- Create: `backend/scripts/analyze_eligibility_run.py`
- Create: `backend/tests/test_analyze_eligibility_run.py`

**Interfaces:** `analyze_run(...) -> RunOptimizationReport`,按 `task_id` 聚合 run / task / AI step / tool /
重复读取 / token。

> ⚠️ **v1.1 R1 事实订正(实测)**:「usage 取 RunJournal 的 `external_llm_usage_records` 即可按 task 聚合」
> **不成立**。`record_external_llm_usage_records`(`journal.py:455`)把子代理 usage 折叠成 **run 级标量**
> (`_subagent_tokens`)与 per-model 桶,`task_id` 从未落盘;`SubagentTokenCollector`(`token_collector.py:16`)
> 只记 `caller`(=`subagent:<name>`);子代理 `run_config` 只挂自己的 collector(`executor.py:556-559`),
> 父 RunJournal 未附加,所以子代理 LLM 调用**不产生** `llm.ai.response` 事件。
> **结论:纯 path A 拿不到 per-task token 归因。**
>
> 所需数据其实已在流动:`task_tool` 的四个终态 chunk 都已带 `usage`
> (`task_tool.py:436/448/458/464/471`),只是 `subagent_run_event` 的终态分支把它丢掉了。
> 因此实际采用**「path A + 最小 path B」**:只在 `step_events.py` 的终态分支停止丢弃该字段
> (写入 `metadata["usage"]`,缺失时**不伪造 0** —— 否则失败任务会显示为零成本),
> 不新增 schema、不改采集链路。`test_subagent_step_events.py` 无回归。

- [ ] Step 1:写失败测试——两条子代理 usage 记录须按 task_id 独立输出;965 个 message step 不得被误报为 LLM 调用。
- [ ] Step 2:验证失败(缺 `analyze_run`)。
- [ ] Step 3:实现最小分析器,只读 RunStore / RunEventStore / RunJournal,**不改事件 schema**。
- [ ] Step 4:验证通过,生成基线 JSON。
- [ ] Step 5:提交。

## 3. Task 2:子代理技能收窄(纯配置 + 测试)

> **无生产代码改动**。基础设施已具备,本轮只做配置启用与回归测试。

**⚠️ 影响面(v1.1 未点明,须知情)**:`subagents.agents.<子代理名>.skills` 按**子代理名全局生效**
(`registry.py:108` 传入的是子代理名),没有 per-父agent 覆盖机制。把 `general-purpose.skills: []`
写进 `config.yaml`,**所有**父 agent(clinical-medicine / biostats / ppt-generator …)的 `general-purpose`
子代理都不再加载技能。本轮接受该全局语义(不新增覆盖机制以免扩大侵入面);若后续需要按父 agent 区分,
另立项。

**Files:**
- Modify: `config.yaml`(gitignored 运行态)
- Modify: `config.example.yaml`(补文档化示例)
- Test: `backend/tests/test_subagent_skills_config.py`

- [ ] Step 1:写失败测试——`skills=[]` 不得回退为「继承全部技能」;`skills=["criteria-parser"]` 只加载该技能。
- [ ] Step 1b:写 tool_policy 回归测试——`skills=[]` 时 `allowed_tool_names_for_skills` 返回 `None`、
      `filter_tools_by_skill_allowed_tools` 不过滤,`parse_document` 保留在子代理工具集中。
- [ ] Step 2:验证失败。
- [ ] Step 3:配置 `general-purpose` / `quality-control` 的 `skills: []`;
      > ⚠️ **范围偏离(有据,已实施)**:v1.1 原文含 `report-writer`,本轮**故意不设**。
      > 委派关系实测(按 agent SOUL 统计 `task(...)`):`general-purpose` 仅 eligibility-screener 用(1 处);
      > `quality-control` 由 eligibility(10)+ops-quality(1)用,后者是「监查策略 / TMF 结构」纯领域推理、
      > 且该 agent 全文未引用技能;而 `report-writer` 由 clinical-medicine(**11 处**)、regulatory、
      > ops-quality 使用,**eligibility 已不再委派它**(独立理由阶段于 2026-08-04 移除)。
      > 因此设置它对本次 token 优化零收益,只会给其他 agent 带来无收益的行为变更风险。
      > 另:`ppt-generator` 虽写明「Skills are mandatory」,但它有 **0 处** `task(...)` 委派
      > (该要求针对 lead 自身),不受本键影响。
      确认 lead 不限定 `available_skills`(保持 `None`),否则 `_merge_skill_allowlists`(`task_tool.py:186-194`)
      会用 lead allowlist 覆盖子代理的 `[]`。
- [ ] Step 4:验证通过 + `make lint`。
- [ ] Step 5:提交(仅 `config.example.yaml` + 测试;`config.yaml` gitignored,另做读回校验)。

**验收**:子代理初始固定技能上下文从 42,758 token 降至每类 0–1 个技能;`parse_document` 在
`general-purpose` 工具集中。

## 4. Task 5:版本感知读取去重 + 批量 Patch(分必做 / opt-in)

**Files:**
- Modify: `docs/eligibility-screener-fix-changelog.md`(订正失真声明)
- Modify: `backend/packages/harness/deerflow/sandbox/tools.py`(收紧 `str_replace`;新增 `apply_json_patches`)
- Create: `backend/packages/harness/deerflow/config/read_dedup_config.py`
- Modify: `backend/packages/harness/deerflow/config/app_config.py`(显式声明 schema,不再靠 `extra="allow"` 偷渡)
- Modify: `config.example.yaml`
- Create: `backend/packages/harness/deerflow/agents/middlewares/read_file_dedup_middleware.py`
- Create: `backend/tests/test_str_replace_ambiguity.py`
- Create: `backend/tests/test_read_file_dedup_middleware.py`
- Create: `backend/tests/test_batch_json_patch_tool.py`

**约束**:所有文件 IO 走 `Sandbox` API(禁 `open()` / `Path.write_text`,见 `artifacts.py:3-7`);
`content_hash` 与 `artifacts.py:35` 的 `sha256(bytes)[:12]` 策略一致;写操作用
`get_file_operation_lock`(`tools.py:1909`)。

- [ ] Step 0:订正 changelog 8.4 节的不实「已实现」声明。
- [ ] Step 0b:收紧 `str_replace`——`replace_all=False` 且 `old_str` 出现多次时**拒绝并报错**(先写失败测试)。
- [ ] Step 1:`read_file_dedup` config schema + `config.example.yaml` section,默认 `enabled: false`。
- [ ] Step 2:写去重缓存失败测试——同版本同范围二次读返回短引用;文件变更后必须 miss;跨 thread/sandbox 不共享。
- [ ] Step 3:写批量 patch 失败测试——`expected_hash` 不符拒绝;全成功或全不写。
- [ ] Step 4:实现 middleware(默认关闭)与 `apply_json_patches` 工具。
- [ ] Step 5:验证 + `make lint` + 提交。

## 5. Task 6(重新定义):`parse_image_batch` 批量图片 OCR 工具

> **v1.2 改动**:放弃 v1.1 的「整文档解析 + 改 SOUL」路线(需改运行态 SOUL 编排,侵入性高)。
> 改为**纯加法**:新增一个批量工具,把「拆图 → 逐图 `parse_document` → 逐图 read/write」这段
> Agent 循环压成**一次工具调用**。不动 `parse_document`,不改 SOUL 既有流程(SOUL 可后续择机切换)。

**动机**:基线里 28 张页图 = 28 次 `parse_document` + 28 次 `read_file` + 28 次 `write_file`,
每次都要一轮 AI step。批量工具把 N 页压成 1 次调用、0 次 read/write。

**Interfaces:**

```python
parse_image_batch(input_dir: str, output_dir: str) -> str
```

- 从 `input_dir` 批量取图片(`.jpg/.jpeg/.png/.bmp/.tiff/.tif`),逐张经 TextIn `pdf_to_markdown` OCR;
- 每张写出**同名** `.md` 到 `output_dir`(`M018_page_001.jpg` → `M018_page_001.md`);
- 返回**紧凑索引**(页数/成功数/失败数/输出目录/失败清单),**绝不返回 OCR 正文**——
  与 `parse_document` 同一哲学(正文进对话即等于放弃优化)。

**约束:**
- 并发 ≤ 3(v1.1 Global Constraints:OCR 外部服务并发保持 2–3)。
- **幂等**:`output_dir` 下已存在且非空的同名 `.md` 默认跳过(不重复调用外部服务);`overwrite=True` 才重跑。
- 单张失败不中断整批;失败页记入返回索引,可单独重试。
- 所有 IO 走 `Sandbox` API。

**Files:**
- Modify: `backend/packages/community/cellflow_community/textin/tools.py`(新增工具,不改 `parse_document_tool`)
- Create: `backend/tests/test_textin_parse_image_batch.py`
- Modify: `config.yaml`(注册工具;gitignored)
- Modify: `config.example.yaml`(文档化)

- [ ] Step 1:写失败测试——N 张图 → N 个同名 `.md`;并发上限 ≤3;已存在输出时跳过且 0 次外部调用;
      单张失败不影响其余;返回索引不含 OCR 正文。
- [ ] Step 2:验证失败。
- [ ] Step 3:实现工具(复用 `parse_via_textin` + `_tool_settings` + Sandbox API)。
- [ ] Step 4:验证通过 + `make lint` + 注册进 config。
- [ ] Step 5:提交。

---

## 6. 本轮验收

> **实施状态(2026-08-06 完成)**:Task 1 / 2 / 5 / 6 已全部落地并验证。逐项证据见下。

### 可由本轮交付物验证(代码 + 测试层)

- [x] 新增/修改的测试全绿:9 个相关测试文件 **255 passed**。
- [x] `make lint` 通过:`ruff check packages/ scripts/ tests/ app/` → All checks passed;
      `ruff format --check` → 769 files already formatted。
- [x] 后端全量 `pytest`:**5834 passed / 30 failed**。30 项失败**全部预先存在且与本轮无关**
      (`test_stream_bridge` 11 / `test_client_live` 5 / `test_auth*` 5 / live-agent 3 / deferred 4 / 其他 2)。
      验证方法:把 5 个被改动的生产文件换回 HEAD 版本、以同一子集口径重跑 →
      **34 failed / 180 passed**;换回本轮版本 → **30 failed / 184 passed**,失败集与全量运行完全一致。
      即本轮改动未引入任何新失败,并使 4 项恢复通过。
- [x] `tests/skills/` **431 passed / 8 failed**;8 项全在 `test_image_generation.py`
      (HEAD 已存在的 `provider` NameError,`generate.py:243`),与本轮无关。
- [x] `skills=[]` 时 `parse_document` / `parse_image_batch` 留在工具集中
      (`test_subagent_tool_policy_skills_empty.py`,11 项)。
- [x] `str_replace` 多处出现时拒绝且不写入(`test_str_replace_ambiguity.py`,7 项)。
- [x] `parse_image_batch` 对 N 张图产出 N 个同名 `.md`、并发 ≤3、幂等跳过
      (`test_textin_parse_image_batch.py`,14 项);并用真实 thread 目录
      (`6debc330…/workspace/images/M018（LCXI）/`,7 张 jpg + 1 个 manifest.json)离线演练通过:
      7 图 → 7 个同名 `.md`,manifest 被忽略,中文括号路径正常,表格已附加。
- [x] `read_file_dedup` 默认关闭时行为不变:`enabled=False` 时中间件**不被构造**
      (lead 与 subagent 链均不含);开启后进链且顺序在 `ReadBeforeWriteMiddleware` 之后
      (实测 rbw=8, dedup=9)。14 项测试全绿。
- [x] `apply_json_patches` 原子性 / 版本校验 / 歧义拒绝(`test_batch_json_patch_tool.py`,13 项)。
- [x] 基线 JSON 由 `analyze_eligibility_run.py` 从既有 thread 离线生成成功,
      且**独立复现了本计划第 1 节的全部基线数字**:
      total 34,407,156 / subagent 27,790,091 / ai=379 / tool=586 / read_file=360 /
      unique_paths=207 / recoverable_duplicates=153 / SKILL.md=23 / active=3,639.685s / 29 tasks。

### 需要真实重跑才能验证(不在本轮交付范围)

token / wall-time 的实际下降必须由一次真实会话 replay 得出。本轮**不声称**达到 v1.1 的 P0 数值目标
(≤14M token / ≤35–40 分钟)。replay 后用同一脚本对比:

```bash
export NEW_THREAD_ID="重跑产生的线程 UUID"
cd backend && PYTHONPATH=. uv run python scripts/analyze_eligibility_run.py "$NEW_THREAD_ID" \
  --baseline ../.deer-flow/criteria-token-baseline.json \
  --output ../.deer-flow/criteria-token-after.json
```

⚠️ 历史 run(含基线 `4d1f95b4`)记录于 per-task usage 落盘之前,报告会把它们标为
`tasks_missing_usage` 而非 0 token。重跑后的新 run 才有逐 task 归因。

### 质量闸(不得因 token 优化而退化)

标准条数与条件 ID 守恒;每条 judgment 有 document/page/evidence/conclusion/reason;
`suspected_missed == []`;排除方向 `conflicts == []`;
`reason_alignment_*.json.conflicts == []`(原则十,2026-08-04 新增);
`build_reports.py --verify` 通过。

## 7. 回滚

本轮所有改动均可独立回滚,互不依赖:

1. **Task 6**:从 `config.yaml` 的 `tools:` 移除 `parse_image_batch` 注册 → 工具对所有 agent 消失。
2. **Task 5 read_file_dedup**:`enabled: false`(默认值)→ 无运行时影响。
3. **Task 5 apply_json_patches**:不注册 → 不可见。
4. **Task 5 str_replace 收紧**:如误伤,改回 `content.replace(old_str, new_str, 1)`;
   但**须同期修正 docstring**,不得再留「声称 exactly once、实际替换第一处」的矛盾。
5. **Task 2**:删除 `config.yaml` 里的 `skills: []` → 回到继承全部技能。
6. **Task 1**:纯新增脚本,删除即回滚。

## 8. 未落地项跟踪

以下已确认为真但本轮不修,需单独跟踪:

| 项 | 位置 | 影响 |
|---|---|---|
| 空 summary 数据丢失 | `summarization_middleware.py` `_summarize_with` / `_asummarize_with` | 启用 summarization 且摘要模型返回空串时,旧 summary 与全部消息同时丢失 |
| 模式状态丢失 | 无 typed state(Task 3) | 基线 `84c9f85e`:重复询问处理模式,额外 588,753 token |
| 子代理无实时预算 | `build_subagent_runtime_middlewares` 不含 `TokenBudgetMiddleware`(Task 4) | 单个失控子代理可推高 lead 累计 input |
| 子代理 `checkpointer=False` | `executor.py:381` | 子代理 hard stop 无法靠 checkpoint 恢复 |
