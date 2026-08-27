# f9231297 判定阶段静默丢失 + 工具/脚本失败修复计划

> 触发会话：`f9231297-c802-4264-82a8-961c4dc317d8`（eligibility-screener，2026-08-24，run `f726f5b5`）
> 计划日期：2026-08-24
> 关联：`2026-08-24-in1-in8-dead-loop-fix-plan.md`（同族）, `2026-08-24-eligibility-screener-parallelism-optimization-plan.md`（并行纪律）, memory `eligibility-screener-156a476e-analysis.md`（静默 drop 先例）

## Problem Statement

两部分问题，全部有 run 事件实证：

1. **判定阶段首批 3 个 task 调用静默丢失**（本轮最严重）— 17:02:46 模型回复含 7 个工具调用
   （`task 判定IN-b1, task 判定EX-b1, task 判定IN-b2, read_file, read_file, grep, bash`），
   工具层只执行了后 4 个；3 个 task 无 `Tool start`、无 `llm.tool.result`、无 `subagent.start`、
   无 SubagentLimit 截断日志。模型随后困惑搜索 3 分钟、退化为逐个单发并把 IN-b2/EX-b1 挤到
   队列尾部，判定阶段墙钟损失 ~8-10 分钟。
2. **25 次「工具失败」**（analyze 脚本口径：lead 3 + subagent 22；by tool：bash 19 /
   read_file 4 / apply_json_patches 2）— 其中一部分是真错误，一部分是门禁设计性 exit 2
   被误计，一部分是行为性失败。

## 一、失败根因分析（已逐条核清）

### 1A. task 调用静默丢失（判定首批）

**已排除**：SubagentLimitMiddleware（3≤3，无 truncation 日志）、deferred_tool_filter
（只管 MCP deferred 工具）、summarization compaction（17:02:52 是 `before_model` 正常时序）、
task 工具自身报错（报错会留 ToolMessage）。

**丢失位置**：model 节点输出 → tools 节点路由之间。同回复后 4 个工具全部正常执行而前 3 个
task 消失，全链路零日志——与 `156a476e` 的「残缺 task() 静默 drop」同族，本次是**整轮 3 个
全丢**且可完整复现取证。

### 1B. 真工具错误（8 次，应修）

| # | 时间 | 任务/工具 | 失败 | 根因 |
|---|------|----------|------|------|
| 1 | 16:47:44 | IN轨QC / read_file | `criteria_qc_bundle_IN.md` 不存在 | **lead 漏跑「派 QC 前装配取证素材包」硬前置**：该步骤只写在 `criteria-parser/SKILL.md` L84，**SOUL 编排层没有这一步**（soul/skill split 接线缺口） |
| 2 | 16:49:31 | EX轨QC / read_file | `criteria_qc_bundle_EX.md` 不存在 | 同上。且 QC 子代理没按 `criteria-qc-checklist.md` L308「立即返回拒绝执行」——吞掉错误继续干 |
| 3 | 16:49:11 | lead / read_file | `ocr_coverage.json` 不存在 | OCR 在途时提前读覆盖产物（读得太早） |
| 4 | 17:18:58 | lead / read_file | `judgment_structure_gate_..._b4_rerun.json` 不存在 | 命名不符（子代理产出与实际命名不一致） |
| 5 | 16:50:06 | 修订IN / apply_json_patches | patch 13/25：`/…/IN-10-10/运算符` key 不存在 | 组装 25 个 patch 前未核对现值（没先发 `op=get`） |
| 6 | 16:53:02 | 修订EX / apply_json_patches | patch 8/15：`add` 撞已有 key | 对已存在字段用 `add`，应用 `replace` |
| 7 | 17:07:02 | 判定IN-b1 / bash | `check_reason_alignment.py` 参数错（EXIT:2 usage） | 子代理偏离渲染模板里的闸命令（乱改参数） |
| 8 | 17:07:02 | 判定IN-b1 / bash | 闸6「漏判反查产物缺失」 | **闸执行顺序错**：结构闸跑在 `uncertain_recheck` 落盘之前 |

### 1C. 计量误报（bash 失败计数的大部分，不是错误）

门禁脚本**设计性 exit 2**（结构闸发现问题、闸6/闸8 阻断、`⚠️ 疑似漏判` 提示行）被
`analyze_run_timing.py` 的 `_FAILURE_STATUS`（`EXIT[=:][1-9]`）全部计为「工具失败」。
bash=19 里大部分是门禁在**正确执行它的职责**。把门禁正常告警与工具崩溃混在一个计数里，
监控会持续误报、真实错误被淹没。

### 1D. 行为性失败（非工具错误，单独修）

| 行为 | 证据 | 影响 |
|------|------|------|
| lead 逐脚本跑 `-h` 探索用法 7 次（17:02:46-17:04:35） | 7 条 `usage:` 输出 | 白费 3 分钟 + 全是 EXIT 2 噪声 |
| 熔断已触发仍继续派改判 | message `[15]`：IN-10-7 连续 4 轮 `suspected_missed` | 改判不收敛继续烧 token |
| lead 运行时 `skill_manage` patch 门禁脚本 ×2 | message `[31]`、`[55]` 均 patch `check_judgment_structure.py` | 违反「改数据不改脚本」；运行中改闸=闸的可信度归零 |

## 二、修复方案

### T1（P0）：task 丢失的根因已解 —— `prompt_file` 完整性契约漂移（✅ 已修复）

**根因（2026-08-25 锁死，非路由层缺陷）**：`SubagentLimitMiddleware._is_complete_task_call`
要求 `task` 调用必带 `prompt`，而 `task_tool` 的官方形态是 `prompt_file`（SOUL/模板要求
`render_judge_prompt.py` 渲染后以 `prompt_file` 传路径）。f9231297 五个判定派发批次的
task 调用全部 `prompt=✗ / prompt_file=✓` → 每个都被判「不完整」：

- gateway.log 五条 `Dropped N incomplete task tool call(s)` 与五个派发时刻一一对应
  （17:02:46×3、17:05:42×1、17:10:31×1、17:14:04×3、17:20:27×2）；
- 错误反馈预算 `_MAX_INCOMPLETE_ERROR_FEEDBACK=3` 被 17:02:46 的首批 3 个调用一次耗尽，
  此后「丢弃」警告照发但调用实际被放行执行——行为自相矛盾，正是「零并行假象 + 模型
  困惑 3 分钟」的全部来源。

**修复（已落地）**：
- `_is_complete_task_call` 改为：`description` + `subagent_type` 必带，指令内容
  `prompt` / `prompt_file` **二选一**（`_missing_task_args` 输出缺项清单）。
- `_sanitize_task_calls` 末尾新增 debug 日志「本回复存活进入执行的 task 调用
  (id, description) 清单」——未来丢调用时 gateway.log 一处就能对账。
- 测试：`tests/test_subagent_limit_middleware.py` 新类 `TestPromptFileCompleteness`
  （prompt_file 完整 / 双 prompt 形态皆缺仍拦截）；原 5 个因「保留 incomplete 对齐
  注入反馈 id」而索引过时的用例一并修正。

**验证**：`tests/test_subagent_limit_middleware.py` + `test_create_deerflow_agent.py`
77 项全绿；`tests/test_analyze_run_timing.py` 47 项全绿。

### T2（P0）：analyze 脚本增加「声明未执行」检测 —— 现有数据就能定位历史事故

扩展 `analyze_run_timing.py`：对每个 AI 回复的 tool_calls 与随后 `llm.tool.result` /
`subagent.start` 的 tool_call_id 做对账，输出「declared but never executed」清单
（含 run_id、时间、工具名）。对 b1510d50 / 156a476e / f9231297 三个线程重跑，验证该
检测器的一致性并量化同类事故的完整发生率。

**文件**：`backend/scripts/analyze_run_timing.py`
**测试**：`tests/test_analyze_run_timing.py` 新用例：构造「声明 7 执行 4」的事件序列，
断言报告列出丢失的 3 个调用。

### T3（P0）：派发核对义务 —— 让 lead 在下一轮就发现丢失（不用等 3 分钟搜索）

SOUL.md Phase 3 判定委派节增加一条 dispatch 后核对义务：

```
⛔ 派发判定 task 后，下一轮**先核对**「task_started 流事件数 == 本轮派发的 task 数」；
有缺口说明某个 task 调用被运行时丢弃（发生过的实症：f9231297 首批 3 个判定全丢），
⛔ 不要先读报告/改判——先把缺失的组合原样补派，再处理已返回的结果。
```

（evidence：本次 lead 最终靠 `MISSING drafts` 自愈了，但花 3 分钟搜索猜原因；把它变成
一句核对义务，恢复时间从 3 分钟降到 1 轮。）

### T4（P0）：修补 soul/skill split 接线缺口 —— 装配取证素材包

- `SOUL.md` Phase 2「派 QC 前」补上硬前置（目前只有 criteria-parser/SKILL.md 有）：

```
每轨派 criteria QC 前（⛔ 硬前置，缺了 QC 会读到不存在的 bundle）：
  python3 /mnt/skills/custom/criteria-parser/scripts/criteria_qc_bundle.py \
    --criteria … --track {TRACK} --ocr … --out …/criteria_qc_bundle_{TRACK}.md
```

- 双向兜底已有一半：`criteria-qc-checklist.md` L308 有「不存在→立即返回拒绝执行」，
  但子代理没执行。把它提到 QC 委派模板的**开工前置自检第一条**，并让返回文案带
  `⛔`（与结构闸前置自检同款式）。

**文件**：`backend/.deer-flow/agents/eligibility-screener/SOUL.md`、
`skills/custom/criteria-parser/references/criteria-qc-checklist.md`

### T5（P1）：criteria 修订的 patch 组装纪律（#5/#6 根修）

`criteria-repair.md`（criteria-parser/references/）增加三条：

1. ⛔ 组装多 patch 批前，先发一次 `{"op":"get"}` 探该条目现值（不写文件）；
2. ⛔ 字段已存在只准 `replace`；`add` 撞已有 key 会被拒——见到 `key already exists`
   不要换批次重试，改成 `replace`（工具已给准确报错）；
3. 单批 patch 越少越好：`无法判断`类修订按条目分批（≤5 patch/批），一批 25 个 patch
   第 13 个错了前 12 个不落地是预期行为，但重试成本大。

**文件**：`skills/custom/criteria-parser/references/criteria-repair.md`
（不存在则新建；执行时先读现行文件核对节名）

### T6（P1）：判定门禁执行顺序防呆 —— 一步 wrapper（#7/#8 根修）

两个失败（参数错、顺序错）都是子代理手写闸命令导致的。渲染模板已给命令原文，但
subagent 会「顺手改参数/调顺序」。根修：新增 `run_judgment_gates.py` wrapper，
把三步闸+结构闸按固定顺序封装：

```bash
python3 …/run_judgment_gates.py --patient {id} --track {SHARD} --batch b1 \
    --qc /mnt/user-data/outputs/qc_report_{id}_{SHARD}.json
# 内部固定顺序：uncertain_recheck → reason_alignment → [EX] exclusion_direction
# → check_judgment_structure（带 --qc/--batch 正确参数）
```

子代理只准跑 wrapper（`judgment-repair.md` 与 `render_judge_prompt.py` 模板同步改）；
wrapper 内每步失败输出统一带「第 N 步失败 + 期望参数」，把「错误参数/错误顺序」从
模型自由度里整个移除。

**文件**：`skills/custom/eligibility-judgment/scripts/run_judgment_gates.py`（新建）、
`scripts/render_judge_prompt.py`（模板引用）、`references/judgment-repair.md`
**测试**：wrapper 对放好的产物 exit 0；对缺反查产物/漏判未清分别 exit 2 且信息明确。

### T7（P1）：失败计量口径 —— 门禁预期输出与工具崩溃分离（1C 根修）

`analyze_run_timing.py` 的失败判据加**门禁豁免**：当 bash 命令含门禁脚本名
（`uncertain_recheck` / `check_judgment_structure` / `check_reason_alignment` /
`exclusion_direction_check` / `criteria_qc` 系列）且输出含结构化门禁结论行
（`⚠️ 疑似漏判` / `闸` / `blocking_issues` / `suspected_missed` / `✅ 反查通过`）时，
不计入工具失败——那是门禁在履行职责；`EXIT:2 + usage:` 形态仍计（真参数错误）。
⚠️ 本豁免是**分析脚本计量层**的语义修整，不涉及运行时循环检测的白名单红线
（memory：禁止 harness 中间件硬编码 skill 脚本名——中间件侧依然靠 mutation-aware 重置）。

**文件**：`backend/scripts/analyze_run_timing.py`
**测试**：新用例钉死「闸发现结构问题 EXIT 2 → 不计」/「闸参数错误 usage → 计」。

### T11（P0）：统一两条闸的 `--ocr` CLI 形态（新增——改判子任务实证）

**根因实证**（改判子任务 `call_ZDPEF2KwXJ2EmS4XW`「修订S042002 IN阻断项」，run `3e9ad13f`）：

- `uncertain_recheck.py`：`--ocr nargs='+'` → 正确形态 `--ocr A B`（空格分隔）
- `check_reason_alignment.py`：`--ocr action='append'` → 正确形态 `--ocr A --ocr B`（重复旗标）
- 渲染模板两处命令都是「`--ocr` 该患者 ocr_records.md」**单数表述**；子代理把
  uncertain_recheck 上成功的空格分隔形态复制到 reason_alignment → argparse
  `unrecognized arguments` → EXIT:2 usage → 子代理误读为「脚本只接受单个 --ocr」→
  改成传**单个文件**重跑。
- **后果是被动的**：reason_alignment 只对照一份 OCR 做锚点/引用校验，另一份文档的
  reason 问题（`no_anchor_hit`/`unsourced_number`/`cross_condition_reason`）全部漏检，
  还报 `conflicts=[]`——半失明闸的「全过」比报错更糟。

**修复**：`check_reason_alignment.py` 把 `action='append'` 改成 `nargs='+'`
（与 uncertain_recheck 完全一致）；同步 `render_judge_prompt.py` 模板把命令改为显式
双文件形态 `--ocr /mnt/user-data/workspace/patients/{id}/ocr/筛选期病历/ocr_records.md /mnt/user-data/workspace/patients/{id}/ocr/筛选期检查/ocr_records.md`（doc-key 列表展开，
不再写单数「该患者 ocr_records.md」）。`judgment-repair.md` 机械闸小节同步命令形态。
**文件**：`skills/custom/eligibility-judgment/scripts/check_reason_alignment.py`、
`scripts/render_judge_prompt.py`、`references/judgment-repair.md`
**测试**：`--ocr A B` 与 `--ocr A --ocr B` 两种形态都跑通（向后兼容双形态）。

### T12（P0）：双文档孪生条目改判规则（新增——IN-10-7 半修根因）

**根因实证**（同一子任务）：患者 S042002 有**双文档**（筛选期病历 29 条 + 筛选期检查
29 条），每个条件ID 在 `documents/{source}/judgments/{cid}` 下有**两条孪生条目**。
子任务把 检查/IN-10-7 修为符合（在检查 OCR 找到 `ccr:67` 证据），病历/IN-10-7 仍是
无法判断（reason 称「仅见 eGFR 76.41 未见 CrCl」），`uncertain_recheck` 扫两份 OCR，
见病历那条继续报漏判 → 机械闸清空不了。子代理 17:42:40 已用 inline python 确认
「符合 / 无法判断」双态，却把另一条留到任务末尾只字未动。

**修复**：`judgment-repair.md` 增加硬规则：

```
⛔ 同一条件ID 在多个 document 下存在孪生条目：QC/recheck 点名的任何一条，必须**同批**
复核它在本患者所有 document 下的孪生条目——证据是分文档的，但判定方向必须逐条有据。
禁止「只改点名的那一份」后结束任务：uncertain_recheck 扫全部 OCR，孪生条目漏一条
闸就永远过不了（f9231297 IN-10-7 实测）。
```

委派模板的输入清单加一行「该条件ID 在所有 document 下的孪生条目位置」。

### T13（P1）：summary 同步改为闸前强制（新增——summary 漂移根因）

**根因实证**：15-patch 与 3-patch 两批写入都改了 `conclusion`，但 `筛选期病历.summary`
四计数未随批次同步（闸5 实测：声明 符合11/无法判断14 vs 实际 10/15）。
`judgment-repair.md` 已有「同一批里一并改掉 summary」的规则但子代理不执行——
**规则改为机械强制**：T6 的 `run_judgment_gates.py` wrapper 在结构闸前加一步
`--fix-summary`（重算各 document 四计数并覆写 summary）或等价检查，让 summary 漂移
不再依赖模型自觉。

### T8（P1）：lead 禁止逐脚本 -h 探索（1D 之一）

SOUL 核心原则补一句：

```
⛔ 技能脚本的用法以 SKILL 文档与各 Phase 小节给的命令原文为准——禁止逐脚本跑
`<script> -h`「探索」参数（run f9231297 曾为此烧 3 分钟且产生 7 条 EXIT 2）。
文档没写清楚（会这样的场景）→ 先 grep 脚本 docstring 一次，不要跑 7 个 -h。
```

### T9（P1）：运行中禁止 skill_manage 改门禁（1D 之二）

SOUL 补：

```
⛔ 运行中禁止用 skill_manage 修改本技能任何门禁/闸脚本——改了闸，本会话此前全部
门禁结论的可信度归零，且与在途子代理拿到的模板版本不一致（f9231297 已发生 ×2）。
需要改闸脚本 = 先停 run、改、回归测试、再开新 run。
```

### T10（P1）：熔断后的强制上报（1D 之三）

SOUL P4 改判节补：

```
收到改判子代理返回「stuck_items 非空 / 熔断升级」时：⛔ 立即 ask_clarification 上报
并停派该组合改判；禁止「换个改判子代理再试一轮」——熔断判据是产物哈希未变，换代理
不会改变输入。
```

## 三、实施顺序与预估

| 优先级 | Task | 类型 | 预期效果 |
|--------|------|------|---------|
| P0 | T1 路由插桩 | 观测 | 下次丢 task 直接锁定层级 |
| P0 | T2 声明未执行检测 | 分析脚本 | 三个历史线程重跑量化事故率 |
| P0 | T3 派发核对义务 | prompt | 丢失自愈 3min→1 轮 |
| P0 | T4 bundle 装配接线 | prompt | 消灭 #1/#2 两类失败 + QC 不再读空 |
| P1 | T5 repair patch 纪律 | prompt | 消灭 #5/#6 |
| P1 | T6 门禁 wrapper | 代码+prompt | 消灭 #7/#8，#4 命名统一顺带缓解 |
| P1 | T7 计量口径 | 分析脚本 | 失败计数恢复可信 |
| P1 | T8/T9/T10 行为禁令 | prompt | 降噪/防脚本漂移/防熔断后空烧 |

**建议批次**：T1-T4 第一批（全部是 P0，含唯一代码级观测改动 + 三处 prompt）；
T5-T10 第二批。

## 四、验证方式

1. T1/T2：`tests/test_analyze_run_timing.py` 新用例全绿；对 f9231297 重跑基线，
   「declared but never executed」段列出 17:02:46 的 3 个 task 调用。
2. T6：wrapper 单测 + 一个真实判定批次回归（wrapper exit 0）。
3. T7：f9231297 重跑后 `tool failures total` 应从 25 降到 **≤8**（1B 表 + 少量噪声），
   `gate_findings`（新细分）单独列出门禁阻断数。
4. 下一真实病例对照：判定阶段无丢批（T3 核对义务生效）、QC 起动前 bundle 存在、
   判定任务 s/step 与批内失败归零。
5. `make format` / `make lint` 通过（后端改动）。