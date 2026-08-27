# 会话 156a476e 执行分析：P2 收尾亲做渲染 → 上下文爆炸 → 判定任务零派发静默完成

> 会话：`http://localhost:3000/workspace/agents/eligibility-screener/chats/156a476e-c2cb-4cb7-8521-22bd80ea7b55`
>
> thread_id：`156a476e-c2cb-4cb7-8521-22bd80ea7b55`
>
> 分析时间：2026-08-20
>
> 数据来源：`backend/scripts/analyze_run_timing.py`、`backend/scripts/analyze_eligibility_run.py`、`logs/gateway.log`、磁盘 workspace 产物实查、`skills/custom/.history/eligibility-judgment.jsonl`（本会话 5 条 patch 全量 diff）
>
> 状态：**仅分析，含完整优化方案**（§6，含具体改哪里/改什么/为什么安全）。
>
> 修订（2026-08-20 二审）：初版 §3-B 把 `--doc-key` 假阴性归因为「零宽字符 / `/mnt/skills` 挂载漂移」，经 `.history/eligibility-judgment.jsonl` 的 5 条 patch diff **证伪**——真实根因是脚本原版有两个缺陷，主代理在运行中把它们修好了（§3-B 已重写）。6.2/6.4/6.5/6.7 的方案与排序随之修订，并新增 6.7（run 级产物闸）、6.8（lead 上下文预算熔断），原「优先级与实施顺序」改为 §6.9。
>
> 关联：同属「判定阶段静默失败」这一类的还有 [`eligibility-screener-monitoring-session-7512ebd2.md`](eligibility-screener-monitoring-session-7512ebd2.md)（空摘要净删除）、[`eligibility-screener-monitoring-session-88df83a8`](eligibility-screener-subagent-context-and-artifact-gate-changelog.md)（产物闸兜底前的自创文件）；本会话是**第三条路径**——不是子代理丢状态，而是主代理在 P2 收尾/P3 准备阶段亲做机械收口，把主上下文推爆后连判定任务都发不出去。

---

## 0. 结论

会话 **50.7 分钟墙钟、8.77M token、13 个子代理任务全部 `completed`、30 次工具调用失败**，最终**产出 0 份判定、0 份报告**。

两个 run 都是 `success`，没有任何崩溃 / 超时 / 递归上限 / 用户强停。真正的故障是**静默提前完成**：主代理在最后 9 分钟亲做「判定 prompt 渲染」，13+ 次调用失败把主上下文推到 ~99 条消息，随后模型开始吐出**参数残缺的 `task()` 调用**，被 `SubagentLimitMiddleware` 连续丢弃（12 次），run 以 `success` 收尾——**系统说「完成」，业务上是卡死**。

四个独立缺陷叠加：

| # | 缺陷 | 位置 | 效果 |
|---|---|---|---|
| **A** | 主代理凭记忆调技能，路径/flag 全错 | lead 的 `bash`/`read_file` 调用 | 9 分钟 13+ 次失败，每一次失败结果都驻留主上下文 |
| **B** | `render_judge_prompt.py` 原版有两个真实缺陷（硬编码模板路径 + 不归一化全角斜杠），主代理被迫运行中修脚本 | `render_judge_prompt.py` | P3 派发被卡 ~2 分钟；排查与修脚本本身也消耗主上下文轮次（初版归因有误，见 §3-B 修订） |
| **C** | 上下文在 P2.5/P3 边界爆炸，且无任何熔断机制 | 主代理亲做收口 + lead 无上下文预算 | 模型产出残缺 `task()`，被静默丢弃 → 提前"完成" |
| **D** | `skill_manage(action="patch")` 盲 patch 四连败 | lead 修 `criteria_qc_bundle.py` | 4 次 `Patch target not found`，每次失败结果驻留主上下文；同时暴露约束缺退路 |

A 是引擎，B 让 P3 迟迟进不去，C 是压垮流程的最后一步，D 是此前未被指认的早期贡献者。

---

## 1. 会话拓扑

两个 run，同一 thread：

| run | 状态 | token | 内容 |
|---|---|---|---|
| `dfb845ea-b7bc-4a37-b74b-6262da372086` | success | 779,491 | Phase 1 预处理（分类+拆页+入排章节提取） |
| `665ef0ac-e22a-4294-a503-5c2e3b5f4b82` | success | 7,991,964 | Phase 1.5 之后的 P2 三轨 + QC/修订 + P2 收尾 + P2.5 + P3 准备（本次故障所在） |

两 run 之间 idle **648.4s**，是 P1.5 `ask_clarification` 的人机确认（合法 HITL 中断，非异常）。

13 个子代理任务全部 `completed`，按阶段分布：

| 阶段 | 任务数 | 说明 |
|---|---|---|
| P2-OCR | 4 | OCR 病历首批/次批、检查首批/次批 |
| P2-解析 | 2 | IN 轨、EX 轨 |
| P2-QC | 4 | IN 首轮/次轮、EX 首轮/次轮 |
| P2-修订 | 3 | IN 修订首轮、IN 补修 IN7-1、EX 修订首轮 |

产物盘查（`outputs/` 实查）：`criteria_parsed.json`、`criteria_qc_{IN,EX}.json`、`ocr_records_{2150006}_{筛选期病历,筛选期检查}.md`、`patient_index.json` 均已落盘；`patients/2150006/prompts/` 下 8 个 `judge_prompt_*.md` 已渲染（23:11）。**缺 `judgments_*.json`、`screening_report.html`、`criteria_report.html`**——P3 之后的全部缺失。

---

## 2. 故障链（逐步实证）

### 2.1 前半程正常：P2 三轨并行，收口紧

`check_track_structure.py` 全会话执行 **32 次**，全部集中在修订任务（IN 修订 11 次、EX 修订 13 次、IN 补修 5 次、解析 3 次），是修订子代理「改一处→重跑全闸→再撞新项」的门禁循环。但好在三条子代理守卫本会话**全部生效**：

```
whole_file_reread_calls = 0
subagent_compactions   = 0
artifact_gate_failures = 0
```

浪费点已从此前几轮优化的「重复读文件 / 子代理丢状态」转移到「门禁循环 + 主代理亲做收尾」。

### 2.2 最后 9 分钟：主代理亲做收口，13+ 次调用失败

23:02:42 起，P2 已完成（`slim`/`assemble` 在 23:03 产出 `criteria_judge_{IN,EX}.json` + `criteria_parsed.json`），主代理进入 P2.5 聚合 + P3 prompt 渲染。这一串在 SOUL 里本应是**机械操作**，实际由主代理亲做，于是：

| 时间 | 失败 | 归类 |
|---|---|---|
| 23:02:42 | `judge_pack.py` 在 `criteria-parser/scripts/` 下找不到 | **路径错**：脚本在 `eligibility-judgment/scripts/` |
| 23:03:05 | `read_file criteria_judge_IN/EX.json`、`criteria_parsed.json` 均「File not found」 | 文件其实尚未 slim/assemble 出来，主代理读早了 |
| 23:05:18 | `stat: illegal option —— c` | **可移植性**：GNU `stat -c` 跑在 macOS BSD `stat` 上 |
| 23:06:04 | `build_ocr_records.py` 不存在 | **文件名幻觉**：`patient-separator` 只有 `ocr_page_index.py` |
| 23:07:25 | `cp: ...: Not a directory` | **shell 语义**：`cp A B C` 把 C 当目录，C 却是文件 |
| 23:08:06 | `render_judge_prompt.py` usage 报错 | **flag 全错**：`--patient-id/--criteria/--ocr/--output` 应为 `--patient/--batches/--doc-key/--out-dir` |
| 23:09:13 / 32 | `[render] judge-delegation.md 读取失败` | **脚本真实缺陷**：`DEFAULT_TEMPLATE` 硬编码 `/mnt/skills/...` 绝对路径，当次不可达。主代理 23:09:35 patch 改为 `__file__` 相对路径后即通（§3-B） |
| 23:09:55 / 10:19 | `[render] --doc-key 的路径必须在 /mnt/user-data/ 下`（路径「看起来」就在） | **脚本真实缺陷**：入参大概率含全角斜杠 `／`（与 ASCII `/` 肉眼几乎不可分），原版不归一化故前缀校验失败。主代理 23:10:39 patch 加全角斜杠/反斜杠归一化后即通（§3-B） |
| 23:11:54 | `read_file judge_prompt_2150006_IN_b1.md` 不存在 | render 尚未成功，主代理读产物过早 |

23:06:58 主代理用内联 Python heredoc 手动做了 P2.5 聚合（`patient_index.json` + `ocr_records.md`）——本该由 `patient-separator` 脚本完成，因 `build_ocr_records.py` 幻觉而手写兜底。这本身是「绕过技能自造实现」，SOUL 明令禁止（⛔ 禁止绕过技能自造实现），但主代理在没有脚本可用时走了这条路。

### 2.3 上下文爆炸 → 残缺 `task()` 被静默丢弃

主代理每次调用都会重发完整 `<uploaded_files>`（含试验方案.md 全目录大纲）+ 累积历史。23:11:54 起 `message_counts` 快速攀升：`72 → 76 → 80 → 86 → 90 → 94 → 96 → 99`。与此同时：

```
23:12:00  WARNING Dropped 1 incomplete task tool call(s) missing one of ('description', 'prompt', 'subagent_type')
23:12:24  WARNING Dropped 3 incomplete task tool call(s) ...
23:12:35  WARNING Dropped 3 ...
23:12:41  WARNING Dropped 1 ...
23:12:54  WARNING Dropped 3 ...
23:12:59  WARNING Dropped 1 ...
```

6 轮共 **12 个 `task()` 调用因参数残缺被 `SubagentLimitMiddleware` 丢弃**（不是报错、不是排队，是静默 drop）。23:12:59 `Run 665ef0ac -> success`，run 正常收尾——**P3 判定一个都没派出去**。

这不是「超发被丢弃」那个已知陷阱（SOUL 原则 1 写的是"超发 5-6 个被静默丢弃"），而是**参数本身残缺**：`description`/`prompt`/`subagent_type` 缺项，说明模型在 ~99 条消息的上下文下已经吐不出一个完整的 `task` 调用了。

---

## 3. 根因归类

### A. 主代理不查技能契约，凭记忆调脚本（15 次失败的主力）

`judge_pack.py` 归错技能、`build_ocr_records.py` 名字是编的、`render_judge_prompt.py` 的 flag 全错、`cp`/`stat` 用法错。这些在 `references/` 里都有逐字命令，但主代理没有按契约「先读脚本用法再调」，而是靠推理直接发 bash。每一发失败都变成一条驻留主上下文的工具结果，为 §2.3 的爆炸供料。

### B. `render_judge_prompt.py` 原版缺陷 ×2，主代理运行中修复（P3 被卡 2 分钟；初版归因已修正）

`render_judge_prompt.py` 报 `--doc-key 的路径必须在 /mnt/user-data/ 下`，报错里的路径「看起来」以 `/mnt/user-data/` 开头却没过校验。主代理随后写了 5 段内联 Python（23:10:19/35、23:11:00）逐字节 `hex(ord(c))` 排查。

**初版结论（已证伪）**：曾用「同脚本同字符串复现通过」推断脚本无 bug，归因为零宽字符或 `/mnt/skills` 挂载漂移。该复现跑的是 **patch 后**的脚本，结论无效。对 `skills/custom/.history/eligibility-judgment.jsonl` 里本会话全部 **5 条 patch 记录**做 diff 后，真实时间线如下（jsonl 时间为 UTC，+8 即本地）：

| 本地时间 | patch 内容 | 对应现象 |
|---|---|---|
| 23:09:35 | `DEFAULT_TEMPLATE` 从硬编码 `Path("/mnt/skills/custom/eligibility-judgment/references/judge-delegation.md")` 改为 `Path(__file__).resolve().parents[1] / "references" / ...` | 23:09:13/32 的 `judge-delegation.md 读取失败` → patch 后 23:09:55 同一命令即通。初版当作「挂载漂移」证据的「42 秒后又读到了」，实为这次修复生效（为何硬编码路径当次不可达已不可考；可确定的是脚本本身始终可被调用执行，与「/mnt/skills 整体不可见」矛盾，且相对路径修复当即生效） |
| 23:10:39 | `parse_doc_keys` 内先做 `path.replace('／', '/').replace('\\\\', '/')` 再前缀校验 | 23:09:55 / 23:10:19 的 `--doc-key` 前缀失败：入参大概率含**全角斜杠 `／`**（U+FF0F，与 ASCII `/` 肉眼几乎不可分，初版因此误读为「路径明明正确」）。patch 内容本身就是证据——它修的正是这两个字符 |
| 23:11:33 / 23:11:37 / 23:11:40 | 把归一化抽成 `normalize_user_data_path()`（含 `/workspace/` 等虚拟根前缀重写）+ 补 `--page-index` 归一化 | 23:11:54 渲染全部通过，8 个 prompt 落盘 |

修正后的定性：

- **脚本原版有两个真实缺陷**（硬编码模板路径；不归一化全角斜杠/反斜杠），主代理被迫在 run 内逐字节排查并修复。**运行中自改脚本在本会话是救场行为，不是致害行为**。
- 「`/mnt/skills` 可见性漂移」假说失去支撑：初版声称「同一引用文件 mtime 未变，行为却不一致」，但两个观测点之间脚本本身各有一次 patch（初版只看了最终 mtime 23:11:40，漏读 jsonl 里更早的 4 条——即 memory 已记录的「日志 UTC+8 / 库 UTC」时区陷阱：15:09:35 UTC = 23:09:35 本地）。
- 教训换了个方向：修复已固化在当前代码里（`normalize_user_data_path` 即 patch 产物），同类假阴性不会再发；真正要修的是 A（flag 全错导致第一次 render 就失败，才有后续连环排查）和 §6.5（失败演化的终点）。另保留一条通用教训：**分析「行为不一致」类现象时，先 diff `.history/*.jsonl` 的 patch 记录，再谈环境漂移**。

### C. `skill_manage(action="patch")` 盲 patch 四连败（早期上下文供料）

`criteria_qc_bundle.py` 的「段定位不可信」错误（22:46:30）是真实脚本缺陷——EX 轨段的正则 `_HEADING_LINE` 被段内小标题截断。主代理按 SOUL 原则 5 的约束试图修脚本，但 `skill_manage(action="patch")` 要求 `find` 字符串在目标文件里精确出现（`prev_content.count(find) == 0 → raise "Patch target not found"`），而主代理**没读脚本内容就凭记忆构造 find 串**：

| 时间 | 目标 | 结果 |
|---|---|---|
| 22:49:31 | `scripts/criteria_qc_bundle.py` | `Patch target not found` |
| 22:50:07 | 同上 | `Patch target not found`（同轮派发了 IN 轨修订） |
| 22:53:10 | 同上 | `Patch target not found`（同轮派发了 IN 补修） |
| 22:54:08 | 同上 | `Patch target not found` |

四次全部失败，`criteria-parser.jsonl` 历史记录里没有留下任何 thread `156a476e` 的条目。作为对比，同一约束在本会话被**正确执行了 5 次**——主代理对 `render_judge_prompt.py` 的 5 个 patch 全部命中（`eligibility-judgment.jsonl`：23:09:35 / 23:10:39 / 23:11:33 / 23:11:37 / 23:11:40，即 §3-B 时间线），最后一个落地 14 秒后 23:11:54 渲染即通，8 个 prompt 全部落盘。

成功与失败的反差指向同一个根因：**patch 前是否读了目标脚本**。`render_judge_prompt.py` 主代理正在逐字节调试（§3-B），事实上已经读了脚本内容；`criteria_qc_bundle.py` 则是盲猜 find 串。约束本身没错（只有主代理能写 `/mnt/skills`），但约束缺了两条前置：**patch 前必须先 `read_file` 目标脚本确认 find 串真实存在**，以及 **patch 连续失败时的退路**（注意 `skill_manage(action="edit")` 只写 SKILL.md、不支持 `path`，「全量覆写脚本」这条路当前不存在，见 §6.2；约束只说了"不要 write_file"，没给 patch 反复失败时怎么办）。

### D. 主代理亲做收口 → 上下文爆炸（最终压垮）

SOUL 原则 4 明确「修订/改判循环一律委派子代理，主代理不得亲做」，但「P2 收尾/P2.5/P3-prep」这串机械收口没有同等的硬约束，主代理全数亲做，且失败结果不回 summary、不瘦身，逐条堆进主上下文。到 23:12 上下文 ~99 条消息（~100K input），模型开始产出残缺 `task()`。

---

## 4. 时效分析

### 4.1 时间去向（union 口径，不重复计）

| 占用 | 秒 | 占比 | 结论 |
|---|---|---|---|
| 主代理 LLM busy | 1461.2s | 48.1% | 最大头，reasoning 占 43.3% |
| 子代理 busy | 905.0s | 29.8% | — |
| **主/子重叠** | **3.3s** | — | **几乎零并行**，主代理全程在 poll 空等 |
| 合并活跃 | 2362.9s | 77.7% | — |
| 未归因（工具执行/IO/空转） | 677.8s | 22.3% | 大量失败重试的 bash 往返 |

### 4.2 延迟 vs 上下文（主代理 66 次调用）

| 输入区间 | 次数 | 平均延迟 | 说明 |
|---|---|---|---|
| 0–50k | 11 | 6.7s | 正常 |
| 50–100k | 47 | **26.2s** | 主力区间，延迟 4 倍 |
| 100–150k | 8 | 16.0s | 峰值 137.8s（22:25:46，52k 输入读 6 次文件） |

→ 明显**上下文绑定型慢**，不是工具慢。

### 4.3 时效定性

「**前快后崩**」：P2 三轨并行阶段（22:39 → 23:02，约 23 分钟）跑完 13 个任务还算紧凑（虽受门禁循环拖累）；真正的时效黑洞是**最后 9 分钟主代理亲做判定 prompt 渲染**——13+ 次失败 + 5 段内联调试 + 5 次运行中 patch（修脚本方向是对的，但排查与修复的每一步都在消耗主上下文），把上下文推爆，最终连一个判定任务都没派出去。这不是「慢」，是「没跑完却显示完成」。

---

## 5. 其它可量化的浪费

- **token 投入产出失衡**：8.77M token 换 0 份判定、0 份报告；主代理 4.98M / 子代理 3.76M，其中 IN+EX 修订两个任务吃掉 1.96M（22%）。
- **`check_track_structure.py` 32 次执行**：IN 修订 11 次 / EX 修订 13 次，是修订阶段的最大 token 黑洞。
- **lead 空 AI 步 124/137（90.5%）**：几乎每个 AI 步都只发工具调用、无正文（`no_tool_calls=0`，非严格浪费，但说明主代理在做大量「只发工具」的机械轮）。
- **`dedupable_read_calls=0`、`range_overlap=542/6525 行（8%）`**：读取侧浪费已基本被前几轮优化压掉，不是本次瓶颈。

---

## 6. 完整优化方案

每个优化项给出：**改什么文件 / 改什么内容 / 为什么安全 / 预期效果 / 风险与边界**。

---

### 6.1 门禁循环收敛：修订子代理「批量修复 → 一次闸」

**改什么文件**：`skills/custom/criteria-parser/references/criteria-repair.md`

**当前行为**（第 117–126 行，委派模板第 375–376 行）：

```
⛔ 每处理完一条 blocking_issues（即一次 apply_json_patches 调用）就跑一次结构闸，
不要等全部改完再跑：
  python3 /mnt/skills/custom/criteria-parser/scripts/check_track_structure.py \
      --workspace /mnt/user-data/workspace --track {TRACK}
改一条查一条，坏在哪一目了然；改完 5 条再查，得回头找是哪一次改坏的。
```

**改为**：

```
⛔ 全部 blocking_issues 修复完成后，一次性跑结构闸（不要逐条跑）：
  python3 /mnt/skills/custom/criteria-parser/scripts/check_track_structure.py \
      --workspace /mnt/user-data/workspace --track {TRACK}
闸输出按条件 ID 点名，失败时精准定位，不需要逐条查来隔离。
若闸仍非零，只修点名条目，再跑一次——单次修订任务的结构闸控制在 1-2 次。
```

**为什么安全**：本次会话的 32 次闸执行中，每次发现的都是**独立新问题**（EX-12-OR 或组语义、EX-15-3 OR/无或组、EX-16 描述索引……），不是同一条的反复修复。闸输出按条件 ID 点名，批量修复后一次失败，输出会点名具体哪些条件 ID 仍不合格，精确度不低于逐条查。且 `--show <ID>` 仍可用于单条自检，不与批量闸冲突。

**预期效果**：单次修订任务的结构闸从 N+1 次降至 1–2 次（本次 IN 修订 11→1-2、EX 修订 13→1-2），子代理 token 节约 30–50%。

**风险与边界**：
- 如果修订子代理的某条 patch 写错了 JSON pointer（如 `add` 到不存在的父节点），批量闸会点名，子代理可以按 `--show` 定位后补修。最坏情况是多一次闸，不会比现在的逐条模式更差。
- 委派模板里「每处理完一条就跑一次」的指令 ALSO 出现在 `backend/.deer-flow/agents/eligibility-screener/SOUL.md` 中 Phase 2 的修订委派模板（口头描述，非逐字），但 SOUL 的权威来源是 `criteria-repair.md`——两处必须同步改，SOUL 只做引用而不重复规则。

---

### 6.2 `skill_manage` 盲 patch 防护

**改什么文件**：`backend/.deer-flow/agents/eligibility-screener/SOUL.md` 原则 5

**当前行为**（原则 5 "修脚本" 段）：

```
小改动用 `action="patch"` + `path="scripts/x.py"`，⛔ 不要 `write_file` 全量覆盖
```

**在约束后追加两条硬前置**：

```
⛔ patch 前置（缺一不可，违反等同流程失败）：
1. 必须先 `read_file` 目标脚本（至少读目标行 ± 10 行上下文），`find` 必须是脚本里
   真实存在的字面串——拿 `Path target not found` 的教训是盲猜 4 次全败，每次都是一轮
   全量上下文的时间。
2. 连续 2 次 `Patch target not found` → 立即停止盲试，改走：
   a) `read_file` 目标脚本的完整内容，定位真实正则/常量后重新构造 find；
   b) 仍失败则 `ask_clarification` 汇报脚本缺陷与尝试过的 find 串，请用户提供精确的 old_string。
   ⛔ 注意：`skill_manage(action="edit")` **不支持** `path` 参数（edit 分支只写 SKILL.md，
   见 `skill_manage_tool.py`）。「edit 全量覆写脚本」这条路当前不存在，不要在退路里引用它；
   若确需该退路，需先扩展 edit 分支支持 `path`。
```

**为什么安全**：成功的 case（`render_judge_prompt.py` 的 5 个 patch，§3-B）正是主代理正在逐字节调试、事实上已经读了脚本内容后才打的。前置 1 把这个 implicit 前提变成 explicit 硬约束。前置 2 给了退路——当前约束只有"不要 write_file"，没有"patch 反复失败时怎么办"。

**预期效果**：消除盲 patch 四连败（4 次失败 patch 不消耗主上下文轮次），skill_manage 失败率从 80%（4/5）趋向 0。

**风险与边界**：`ask_clarification` 中断运行，用户不回答则流程暂停——这是预期行为，比静默四连败更可控。全量覆写脚本在当前工具契约下没有合法入口（见上），不存在「绕过 patch 契约直接覆写」的风险面。

---

### 6.3 P2 收尾/P2.5/P3-prep 机械脚本化

**改什么文件**：`backend/.deer-flow/agents/eligibility-screener/SOUL.md` Phase 2 收尾段 + Phase 2.5 + Phase 3 入口

**改什么**：在 SOUL 中新增一条原则（原则 12 或扩展现有原则 4 的覆盖范围）：

```
⛔ P2 收尾/P2.5/P3-prep 一律委派子代理或合并为单次 bash，主代理不得亲做、不回读结果。
具体：
- slim ×2 + assemble → 一个 bash（已在 `references/parse-delegation.md` 有命令，加 `set -e` 包裹）
- patient_index + ocr_records 聚合 → 一个 bash（`patient-separator` 的脚本链）
- judge_pack.py plan-batches + render_judge_prompt.py ×2 → 一个 bash（`set -e` 包裹）
每步只写 `phase{N}_summary.json`，主代理只读 summary，不读产物全文。
```

**为什么安全**：这些操作全是机械性的（`slim`/`assemble`/`plan-batches`/`render`），不涉及语义判断，不存在"必须主代理亲做"的理由。本会话主代理亲做全部失败——13+ 次失败 + 内联 Python 手写 P2.5 聚合，反而是"委派给机械脚本"更可靠。

**预期效果**：消除主代理亲做收口的 9 分钟 thrash，主上下文不再被机械操作的失败结果推爆。

**风险与边界**：初版认为本项依赖「先修 `--doc-key` 假阴性」（且把序号误写为 §6.5，路径归一化实为 §6.4）——该依赖不成立：根因是脚本原版缺陷，已在当次 run 内修复并固化于当前代码（§3-B）。§6.4 的 NFC/零宽加固是可选防御，不阻塞本项。

---

### 6.4 `render_judge_prompt.py` 归一化加固（降级为可选防御）

**初版定位有误，已修正**：本项原被当作「消除 `--doc-key` 假阴性」的根因修复。经 §3-B 修正，观测到的假阴性是脚本原版缺陷（不归一化全角斜杠/反斜杠），且当次 run 内的 patch 已修复并固化为 `normalize_user_data_path()`。本项降级为针对**其余**不可见字符的防御性加固，不阻塞任何其它项。

**改什么文件**：`skills/custom/eligibility-judgment/scripts/render_judge_prompt.py`

**改什么**：在 `normalize_user_data_path` 开头增加两行防御：

```python
import unicodedata

def normalize_user_data_path(path: str) -> str:
    path = unicodedata.normalize('NFC', path)                          # 新增：组合字符归一
    path = re.sub(r'[\u200b\u200c\u200d\u200e\u200f\ufeff]', '', path)  # 新增：剔除零宽字符
    path = path.replace('／', '/').replace('\\\\', '/')
    ...
```

**为什么安全**：NFC 归一化消除 Unicode 组合差异，零宽字符剔除消除 LLM 重发含中文路径时的隐藏字符。两者都无副作用：`startswith`/`==` 的判定语义不变；零宽字符在 POSIX 路径中无合法语义。

**预期效果**：防住全角斜杠/反斜杠之外的其余不可见字符类入参污染。对本会话类故障**无增量收益**（根因已修）。

**已撤销的子项**：初版附带的「⛔ skill_manage 修改后，下一轮才可调用该脚本」规则**撤销**。其依据（patch 写入与 `/mnt/skills` 读取端存在刷盘竞态）被 jsonl 时间线证伪：本会话最后一个 patch（23:11:40）落地 14 秒后调用即成功，无竞态实证；而运行中修脚本恰是本会话**唯一奏效的救场行为**（§3-B），硬禁它会关掉这条修复路径。若担心极端刷盘延迟，至多作建议级提示（patch 后立即可调；行为异常先重试一次再排查），不作硬禁令。

### 6.5 缺参 `task` 丢弃：可见化 + 错误回喂

**改什么文件**：`backend/packages/harness/deerflow/agents/middlewares/subagent_limit_middleware.py`

**当前行为**：`WARNING Dropped N incomplete task tool call(s) missing one of ('description', 'prompt', 'subagent_type')` 只在日志打一条 WARNING（`subagent_limit_middleware.py:117`）；丢弃的实现是把 tool_call 从 AIMessage 里直接移除（`:122`），**模型得不到任何反馈**，于是下一轮继续吐残缺调用（本会话 6 轮 12 个），run 照常以 `success` 收尾，前端无感知。

**两处修改**：

1. **可见化（不改行为）**：middleware 累计本 run 被丢弃的 incomplete task 数，经 AgentState / `step_events` 通道上抛，run 收尾时若 > 0 在 run 的 `error` 字段写入摘要（如 `"12 task(s) dropped due to incomplete arguments"`），前端运行卡片可见。已核实 `RunStore.update_status(run_id, status, *, error=None)`（`persistence/run/sql.py:159`）本身接受任意场景的 error 写入；真正缺的是 middleware → RunStore 的通道，需经 state/事件传递、由 run 收尾方落盘（fallback：`RunJournal` 新增 `run_warning` 事件类型）。
   ⛔ 初版「`description` 含 `判定`/`judgment` 才升级」的过滤**不可实现**：被丢弃的调用恰恰缺 `description`（缺 REQUIRED_TASK_ARGS 之一才会被丢），没有可匹配的字段。一律全量可见化，不做语义过滤。
2. **错误回喂（改行为，更值得做）**：丢弃时不再移除 tool_call，改为保留调用并向该 tool_call_id 回一条错误 ToolMessage（如 `task 调用缺少必需参数 description，请补全后重发`）。模型由此获得自修正机会——本会话 6 轮里若有这条反馈，P3 判定有机会真正派出去。与超发截断（`Truncated N excess`）语义不同：超发丢的是合法调用，回喂会破坏并发控制；缺参丢的是非法调用，回喂错误结果不占用并发槽。

**为什么安全**：可见化零行为变更。回喂只是把「静默移除」换成「显式失败」，与其它 middleware 的 `wrap_tool_call` 错误反馈同一模式。

**预期效果**：下次主代理在上下文爆炸后产出残缺 `task()` 时，用户看到红色提醒而非静默的绿色 `success`；且模型有 1-2 次自修正机会，而非无限沉默。

**风险与边界**：回喂在上下文已爆炸时可能诱发反复重发（每轮 +1 条错误消息）——需配重试上限（同一 run 内累计 N 次缺参后转为终止性错误并写 run error）。这是治标；上下文为何能涨到 99 条消息才是治本（§6.8）。

### 6.6 主代理调技能前强制查契约

**改什么文件**：`backend/.deer-flow/agents/eligibility-screener/SOUL.md` 原则 4

**在原则 4 末尾追加**：

```
⛔ 调脚本前先读其用法：首次调用任何技能脚本前，必须 `read_file` 该脚本的 argparse
   定义或 `--help` 输出（二选一），禁止凭记忆构造 flag。本规则的成本是 1 次 ranged
   read_file（~200-500 字符），收益是避免整轮失败 + 失败结果驻留上下文。
```

**为什么安全**：本会话的 `render_judge_prompt.py` flag 全错（`--patient-id`/`--criteria`/`--ocr`/`--output`）、`judge_pack.py` 归错技能目录、`build_ocr_records.py` 幻觉文件名，三项都可以被"先 `read_file` 脚本头 50 行"挡住。成本 ~500 字符 vs 失败后整轮 ~100K 字符的上下文重传。

**预期效果**：消除「凭记忆调技能」导致的 15 次失败中约 60%（路径/flag 类错误）。

---

### 6.7 run 级产物闸（新增：兜住整类「静默提前完成」）

**改什么文件**：run 收尾路径（harness runtime 的 run 完成处理）+ 各 agent 的终端产物清单声明

**改什么**：run 标记 `success` 前，校验本流程的**终端产物**是否落盘（eligibility-screener：`judgments_*.json` 与两份报告 HTML）。缺件则不得标 `success`——改为 `failed` 并在 error 指明缺哪些产物，或至少进入带醒目 warning 的完成态。

**为什么安全**：这是把子代理层已生效的 artifact gate（本会话 `artifact_gate_failures=0`）提升到 run 级。判定逻辑是纯文件存在性检查，不依赖对失败路径的归因——7512ebd2（空摘要净删除）、88df83a8（自创文件）、本会话（task 静默丢弃）诱因各不相同，但都终结于「应有产物缺失」这同一个可观测事实。逐路径修是打地鼠，产物闸一次兜住整类，对未来未知的第四条路径同样生效。

**预期效果**：「系统说完成、业务上卡死」从静默变为显式失败。

**风险与边界**：终端产物清单需按 agent/流程声明（不同 agent 交付物不同）；用户主动中止的 run 豁免；只闸终端交付物（报告 HTML、judgments），不闸中间产物（judge_prompt 等），避免误杀合法的提前中止。

### 6.8 lead 上下文预算熔断（新增：治 C 的系统性根因）

**改什么文件**：lead 侧上下文管理（summarization/预算中间件或等效机制）+ eligibility-screener 的 config

**改什么**：lead 输入上下文超过阈值时强制干预：触发压缩/摘要，或至少向 run 写 warning。SOUL.md:109 自己定的目标是主代理输入 **< 35K**；本会话实际到 ~100K、99 条消息，超标近 3 倍，**没有任何机制介入**——当前 summarization 守卫默认关闭（中间件 `__init__` 默认 0、config 也关），lead 上下文是无上限增长的。

**为什么安全**：6.3（少亲做机械活）只是减少供料，不封顶爆炸半径；C 的直接死因是「模型在超大上下文下吐不出完整 tool call」。预算熔断是这一失效模式的总闸，无论哪条路径把上下文推高都生效。

**预期效果**：上下文被封在阈值内，模型不会进入「吐残缺 task()」的失能区；4.2 观测到的上下文绑定型延迟（50-100k 区间平均 26.2s，为 0-50k 区间的 4 倍）同时被压回。

**风险与边界**：lead 压缩有丢状态风险（7512ebd2 的空摘要净删除教训）——熔断必须与「压缩摘要不得净删除关键路径/待办」的守卫配套，否则引入新故障路径。阈值定多少（SOUL 目标 35K，实际可从 50-60K 起步）需实测校准。

### 6.9 优先级与实施顺序（修订版）

第一轮（阻断本会话的 P3 瓶颈）：
  6.3  P2 收尾/P2.5/P3-prep 机械脚本化     —— 本会话最直接杠杆；初版的「先修 6.4」依赖已证伪，直接做
  6.7  run 级产物闸                        —— 兜住整类「静默提前完成」，从纯防御提升为第一轮

第二轮（消除修订阶段 token 黑洞）：
  6.1  门禁循环收敛                         —— 需改 criteria-repair.md + SOUL 同步

第三轮（防御性加固）：
  6.5  缺参 task 丢弃：可见化 + 错误回喂
  6.8  lead 上下文预算熔断                  —— 治 C 的系统性根因，与 6.5 配套
  6.2  skill_manage 盲 patch 防护
  6.6  调技能前强制查契约

可选：
  6.4  NFC/零宽加固                         —— 根因已修，仅防其余不可见字符

一个需要自省的结构性问题：6.2/6.3/6.6 三条方案都在往 SOUL 加约束，而本会话根因之一恰是主代理**不读既有约束、凭记忆行动**（references 里逐字命令都有，它没用）。SOUL 每加一条，「靠记忆」的命中率更低。三条里只有 6.6 对准这个元问题；6.3 更好的落点不是新原则，而是写进「阶段推进检查表」的硬性条目 + 在 references 里给可直接复制的整段 bash--让正确路径比凭记忆的路径更省事，比再立一条禁令有效。
