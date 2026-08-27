# SOUL.md 压缩与领域规则下沉计划

**目标**：把 `backend/.deer-flow/agents/eligibility-screener/SOUL.md` 从 **767 行压到 ~400 行**，
只保留「整体流程控制 / 输入输出 / 目录结构 / 并行策略」，并**强化执行流程控制**。
领域执行逻辑（入排解析、患者拆分、患者判定、报告生成）全部由对应 skill 拥有；
bad case 叙事全部移入各 skill 的 `references/failure-archive.md`。

> ## ✅ 实施结果（2026-08-19）
>
> **767 → 591 行（-23%）、34,846 → 26,468 字符（-24%）**，约省 5.1k token/轮。
> 故障叙事行 18 → 2（仅剩两处**指针**）。契约测试 179 项 + 全量 skills 1060 项通过。
>
> **未达 ~400 行的原因**（逐段核过编排特征占比后确认剩下的都是承重内容）：
> - 「严禁跳步 9 条」「目录规范 53 行」「Todolist 模板 22 行」编排特征占比低，
>   但用户明确要求保留目录结构与流程控制，不能删；
> - 「达 3 轮上限的 5 步请示流程」看似领域逻辑，但
>   `criteria-qc-checklist.md:346` 明写「达轮次上限的处置**由编排层决定**」——不能下沉；
> - 压缩过程中三次触发契约测试红灯（`criteria_parsed_IN.json` 等双轨产物被我写成
>   `{IN,EX}` 花括号简写而失去字面声明、`修订（仅当` 锚点被删导致 QC 循环并发规则检测失败、
>   `expected_outputs` 的具体路径示例被删），都已修回——证明这些不是冗余而是被测试守护的契约。
>
> 因此**行数闸定在 610**（实测 589 + 21 行余量）而非计划里的 420。再压就要删规则。

## 关键前提（已核实，决定了做法）

1. **上一轮 split 已完成**，由 `tests/skills/test_soul_skill_contract.py`（835 行）守护三条不变量：
   下沉不回流 / 技能不引用阶段号 / **证据链不丢**。本次是在该契约内继续压缩，不是重做 split。
2. **34 条受保护的硬规则措辞里，30 条已在 skill 中存在**——所以本次主要是
   **删除 SOUL 里的重复表述 + 留指针**，不是搬迁。逐条核查结果见下方「附录 A」。
3. **只有 4 条措辞仅存在于 SOUL**：`超发会被静默丢弃`、`必须单独占一轮`、
   `禁止手写 HTML 报告`、`blocked_round_limit`。它们都是编排层规则 →
   **按用户决定：规则保留在 SOUL，只删叙事**。
4. **5 个 bad case 仅存在于 SOUL**：`ab76d625` / `459951c1` / `03a496cc` / `d1ce04c0` / `ec37dc7d`。
   契约测试 `EVIDENCE_MUST_SURVIVE` 要求它们必须活在语料某处 →
   必须先落到 skill 的 failure-archive，**再**从 SOUL 删除（顺序不可颠倒，否则测试红）。
5. **SOUL 整份进 system prompt**（`prompt.py:739 get_agent_soul` 无摘要、无分段加载），
   agent 目录下除 SOUL.md 外的文件**不会**被自动加载。因此 bad case 只能进 skill 的
   references（skill 有渐进加载），不能新建 agent 级 references 指望它被读到。

## 改动清单

### Task 1 · 三个 skill 补 `references/failure-archive.md`（前置，必须先做）

`pdf-image-extractor` / `patient-separator` / `screening-report-generator` 目前**没有**
failure-archive（只有 `criteria-parser` 与 `eligibility-judgment` 有）。先建立收纳点：

| 新文件 | 收纳哪些 bad case（从 SOUL 迁入） |
|---|---|
| `pdf-image-extractor/references/failure-archive.md` | `ab76d625`（`ask_clarification` 出现 0 次、伪造「用户已确认」写进 `route_reason`）、`459951c1`（纯文本提问不触发中断，同 run 内自行落定路线）、`03a496cc`（与 `write_todos` 同轮 → `goto=END` 被合并掉，用户看到 3 遍选择）、`d1ce04c0`（反复读 0 字节 `M016（ZALO）.md` 卡死）、`workspace/pagepdfs/` 与 `workspace/images_ascii/` 两处目录违规导致同页重复 OCR |
| `patient-separator/references/failure-archive.md` | 现有 SOUL 无该轨独有 bad case；建空骨架 + 收纳 `aggregate-ocr.md` 里已有的叙事引用（保持结构一致，便于后续沉淀） |
| `screening-report-generator/references/failure-archive.md` | `ec37dc7d`（present 了尚不存在的 `outputs/criteria_parsed.json`，工具报成功、用户什么也没拿到；`present_files` 只接受 outputs 路径且**不校验存在性**） |

三个文件都必须被对应 `SKILL.md` 索引（契约测试 `test_no_orphan_reference_files` +
`test_expected_reference_is_indexed_by_skill_md`）。

⚠️ `mode-selection.md` 已拥有模式选择语义，`ask_clarification` 的三条机制性 bad case
放进 `pdf-image-extractor/references/failure-archive.md` 并由 `mode-selection.md` 指过去——
不要塞进 `mode-selection.md` 正文，那会让子代理每次读模式表都重付叙事。

### Task 2 · SOUL 逐段压缩（767 → ~400）

原则：**每段只保留「主代理必须知道、且只有主代理能做」的内容**。判据三问——
① 这条规则的执行者是主代理还是子代理？② 删掉它主代理会做错什么？
③ 同样的话是否已在某个 skill 里？三问中任一指向 skill，就删掉留指针。

| 段 | 现行 | 目标 | 主要动作 |
|---|---:|---:|---|
| 角色定位 | 50 | 26 | 保留：编排者定位、5 行权威表、9 条严禁跳步。**删**每条跳步规则后挂的括号叙事（`5a1c8d95` ×3、`ab76d625`、`459951c1`、`03a496cc` 均已/将在 skill 中） |
| 原则 1 并行 | 28 | 20 | 保留并发预算 3 / 超发静默丢弃 / 打满 / 滑动窗口 / 独占一轮三条。**删** `69612125` 的轮次统计叙事 |
| 原则 2 屏障 | 19 | 17 | 基本保留（这是纯编排），仅去冗词 |
| 原则 3 颗粒度 | 4 | 3 | 合并进原则 1 或保留 |
| 原则 4 容错 | 6 | 5 | 保留 `Stop reason` 识别 + 定向补跑指针（契约测试 `test_soul_forbids_blind_redispatch_on_resource_ceiling` 要求） |
| 原则 5 OCR 编排边界 | 21 | 12 | 保留：路线由用户定/无默认值、落盘去向、在途 ≤2、6-9 页/子任务、目录白名单。**删** `69612125` 叙事 |
| 原则 6 上下文纪律 | 32 | 20 | 保留全部**纪律条目**（这是主代理自己的行为约束），**删**每条后的实测数字与 thread 叙事（`69612125`、`d1ce04c0`、`试劑方案.md 读 7 次`） |
| 原则 7 QC 收敛 | 82 | **28** | 最大压缩点。保留：3 轮上限口径、只计语义 QC、按轨独立、复检带 `--qc`、两层 QC 分工、QC 结论只由子代理写、达上限分岔表、`blocked_round_limit`、修脚本与派修订并发。**删**：三条机械闸的完整判据（→ `eligibility-judgment`）、从严判断整段（→ `judgment-principles.md` 已有）、`S042002` / `5a1c8d95` / `afb85bcd` / `5aa5d6d6` 叙事、`upstream_issues` 判据细节（→ `criteria-qc-checklist.md`） |
| 原则 8 路径纪律 | 9 | 7 | 基本保留 |
| 原则 9 present/交付 | 30 | 20 | 保留 present 三步法（编排层硬纪律）+ 三类交付清单。**删** `ec37dc7d` 叙事（→ Task 1） |
| 原则 10 Todolist | 12 | 10 | 保留 |
| 阶段总览 | 29 | 18 | 保留 8 阶段表 + 并行度速查。**删**与各 Phase 重复的产出枚举（目录规范已有） |
| Phase 1 | 52 | 26 | 改为「轮次骨架」：每轮发什么、依赖谁。**删**流水线 B 的提取手法细节（→ `criteria-extraction.md`）、`locate_criteria_sections.py` 的 splitlines 论证（→ 同上） |
| Phase 1.5 | 42 | 18 | 保留三条硬规则的**规则句**（含 `必须单独占一轮`）+ 中断恢复纪律。**删**三条证据行（→ Task 1）、参数与选项原文指针保留 |
| Phase 2 | 105 | **42** | 保留：路线核对、并发预算分配、三批调度、QC↔修订循环的**轮次骨架**、收尾 slim/assemble 顺序、`phase2_summary.json` schema。**删**：解析委派的 `raw段行号` 论证（→ `parse-delegation.md`）、修订子代理选型理由（→ `criteria-repair.md`）、覆盖率两类缺口处置细节（→ `pdf-image-extractor`）、`afb85bcd` / `5a1c8d95` 叙事 |
| Phase 2.5 | 34 | 20 | 保留三分支表 + 模式1 跳过动作 + 交付。**删**聚合规则复述（→ `aggregate-ocr.md`） |
| Phase 3 | 57 | 26 | 保留：启动闸、患者模式 → summary/OCR 路径表、任务矩阵与派发批次、`expected_outputs` 纪律、不合并两轨、`phase3_summary.json` schema。**删**三条机械闸枚举（→ `judge-delegation.md`） |
| Phase 4 | 39 | 20 | 保留：矩阵、`task(quality-control)` 派发、改判执行者选型、闸门时序（主代理把守）。**删** `81562273` 整段叙事（已在 `judge-delegation.md` + `qc-delegation.md`）、`345f2bf4` 叙事 |
| Phase 4.5 | 24 | 16 | 保留三步机械操作 + 屏障。**删**技能约束 18b 的解释 |
| Phase 5 | 18 | 12 | 保留输入/校验/present。**删**已在 skill 的 `--verify` 细节 |
| 目录规范 | 49 | 45 | **几乎全保留**（用户明确要求保留目录结构）。仅删两处历史违规叙事（→ Task 1） |
| Todolist | 21 | 21 | 全保留（编排契约） |

合计目标 ≈ **402 行**（含空行与分隔线）。

### Task 3 · 强化执行流程控制（用户明确要求，压缩的同时加强）

压缩会让「读起来像清单」，但主代理需要的是**可照着走的状态机**。新增/改写三处：

1. **每个 Phase 小节统一为五要素固定格式**，取代现在长短不一的叙述：
   ```
   ## Phase N: 名称
   ⚙️ todos: [→] ...
   **入口条件**（不满足即停）: ...
   **调度**（第 k 轮发什么，并发几个）: ...
   **⛔ 出口屏障**（未满足禁止进入下一 Phase）: ...
   **产出**: ...  →  ✅ todos: [✓] ...
   ```
   固定格式本身就是流程控制——缺哪一项一眼可见。

2. **新增「阶段推进检查表」**（约 12 行，放在阶段总览后）：把散落在各 Phase 的屏障
   汇成一张**单一真相表**，每行 = `从哪到哪 | 必须成立的条件 | 不成立时的动作`。
   现在这些屏障分散在原则 2、各 Phase 的「⛔ 屏障」与 Phase 3 的「启动闸」三处，
   彼此有重复也有缺口（如 Phase 4→4.5 的屏障只写在 Phase 4 末尾）。

3. **新增「本轮该发什么」决策序**（约 8 行）：把原则 1 的并发规则从「约束列表」
   改写成**每轮开始时的判定顺序**——
   ① 有无状态变更待写（`write_todos` 独占一轮）→ ② 有无待发 `ask_clarification`（独占一轮）
   → ③ 在途 `task` < 3 则补派到 3（跨阶段取无依赖待办）→ ④ 同轮追加不占预算的
   `bash`/`read_file`/`grep` 打满 ≥4 → ⑤ 禁止空等。
   这把「禁止空等」从一句口号变成可执行的步骤。

### Task 4 · 契约测试同步（`tests/skills/test_soul_skill_contract.py`）

1. **行数闸 772 → 420**，并按该测试自己的惯例在 docstring 记录下调理由
   （实测目标 ~402 + 少量余量）。
2. **`EXPECTED_REFERENCES` 新增三个 failure-archive**，使 Task 1 的新文件受
   「必须被 SKILL.md 索引」保护。
3. **`SUNK_FROM_SOUL` 新增本次下沉的关键词**，防止细节回流。候选：
   `取证索引`（已有）、`suspected_missed`、`从严判断`、`raw段行号`、`81562273`、
   `S042002`、`afb85bcd`——即「叙事与领域判据不得回到 SOUL」。
   ⚠️ 只加**确实已下沉**的词；`超发会被静默丢弃` / `必须单独占一轮` /
   `禁止手写 HTML 报告` / `blocked_round_limit` **不得**加入（按用户决定留在 SOUL，
   且 `test_concurrency_budget_is_documented` 还要求 SOUL 保留并发机制）。
4. **新增测试**：五要素格式与阶段推进检查表的存在性断言（Task 3 的产物需要闸门，
   否则下次重构会把格式改回散文）。

## 验证

- `backend/.venv/bin/python -m pytest tests/skills/test_soul_skill_contract.py -q`
  —— 三条不变量 + 新行数闸全绿。
- `backend/.venv/bin/python -m pytest tests/skills/ -q --ignore=tests/skills/test_image_generation.py`
  —— 全量 skills 测试无回归（`test_image_generation.py` 8 项失败是既有问题，与本次无关）。
- **人工核对**：`git diff --stat` 确认 SOUL 只减不增；逐条 grep 五条 bad case 编号，
  确认它们在 skill 语料中各有归属。

## 风险与对策

| 风险 | 对策 |
|---|---|
| 删叙事后主代理失去「为什么」，规则被当成可协商 | 保留每条规则的 ⛔ 标记与一句**后果**（如「切出残缺包」），只删 thread 号与统计数字。后果句是行为约束，叙事是考古 |
| 证据链断裂致契约测试红 | Task 1 先落地、Task 2 再删除。顺序写进任务依赖，不可并行 |
| 压到 400 行后仍有下次「随手加字」 | 闸设 420（余量 18 行），并在 docstring 写明余量用途 |
| 五要素格式重写可能改变语义 | 逐 Phase 改写后与原文做语义 diff 核对：入口/出口/产出三项必须与原「屏障」「产出物」逐字对应 |

## 附录 A · 34 条受保护措辞归属核查（已执行）

30 条已在 skill 中存在（可从 SOUL 删除重复表述）；4 条仅在 SOUL：
`超发会被静默丢弃`（原则 1）、`必须单独占一轮`（原则 1 / Phase 1.5）、
`禁止手写 HTML 报告`（严禁跳步 4）、`blocked_round_limit`（原则 7 / Phase 2）。
按用户决定：**这 4 条规则留在 SOUL**，只删除其后的 bad case 叙事。
