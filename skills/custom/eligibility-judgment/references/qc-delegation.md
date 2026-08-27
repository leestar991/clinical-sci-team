# QC 核验委派模板 + QC 报告结构（按轨）

> 任务矩阵 = **患者 × 轨**（每组合一个 QC 子任务）；`reason` 由判定阶段写定，无独立理由阶段。
> `{SHARD}` = `IN` 或 `EX`；`{分片名}` = 入选 或 排除；`{id}` = 患者ID。
>
> 通过 `task(quality-control)` 核验判定时，传入**路径**（不粘贴正文——粘贴要求主代理先读入
> 全量判定，直接引爆主代理上下文；子代理自读同样内容只在它的隔离 context 里）。
>
> 收敛机制：bash 收敛客观结构 + `task` 语义核验 + 阻断/建议分级 + **最多 3 轮**
> （首检 1 轮 + 改判后复检 2 轮 = **2 次改判机会**）；
> 达上限且仅剩**建议级**时带建议放行、不阻塞交付。但下方两个机械闸非空**一律阻断**，
> 不适用"带建议放行"。
>
> **第 1 轮 vs 第 2/3 轮的范围不同**：第 1 轮委派用下方模板做**全量**核验（清单 8 项全跑）。
> 第 2、3 轮（改判后复检）委派时，主代理必须在模板前加一段收窄说明——只核对上一轮
> `blocking_issues` 是否已按 `action` 改判清空、两个机械闸产物是否重跑至空，**不得**
> 对上一轮未点名的条目重新做全量语义核验、**不得**报出上一轮未出现过的新阻断项（改判
> 副作用导致的新结构/方向错误除外，这类算收敛核验范围内）。目的：防止 QC 临近轮次上限时
> 又从头挑出新问题，白白耗尽改判机会却仍不收敛。

## 并行调度（dispatch 纪律，与判定阶段同一条）

QC 任务矩阵 = **患者 × 轨**，各组合彼此完全独立——QC 子代理只读本组合的产物、
只写自己的 `qc_report_{id}_{SHARD}.json`，不同组合零交集。主代理派 QC 必须遵守与
判定阶段（`judge-delegation.md`）相同的派发纪律：

- **独立组合打满 3 并发**：同一轮 AI 回复一起发出多个 `task(quality-control)` 调用，
  不要等上一个返回再派下一个。
- **dispatch-first**：任一个 QC 返回后，先滑动补派下一批独立组合，再处理这份
  `qc_report`（读阻断项、评估是否派改判）。「等全部处理完再派」会把矩阵退化成串行。
- **组合内串行是唯一例外**：同一 `{患者,轨}` 的 QC → 改判 → 复检必须顺序进行
  （见 `judgment-repair.md`「唯一的串行约束」）；这不影响其他组合继续并行在途。

## 委派模板

**第 2、3 轮（改判后复检）在下方模板正文前插入这一段收窄声明**（第 1 轮不加，直接用下方模板）：

~~~
本轮是第 {N} 轮复检（收敛核验），不是全量核验。只做两件事：
1. 核对上一轮 qc_report_{id}_{SHARD}.json 的 blocking_issues 是否已按其 action 逐条改判清空；
2. 核对 uncertain_recheck_{id}_{SHARD}.json.suspected_missed 与
   【仅 EX 轨】exclusion_direction_check_{id}_EX.json.conflicts 是否已重跑至空。
⛔ 不得对上一轮未点名的条目重新发起全量语义扫描，不得报出上一轮未出现过的新阻断项。
例外：若某次改判本身引入了新的结构/方向错误（例如改坏了 conclusion / exclusion_triggered /
reason 三字段联动），这类"改判副作用"仍要报，不算"扩大范围"。
上一轮 residual_issues（建议级）本轮不强制复核，除非它恰好也在本轮改判涉及的条目范围内。
~~~

~~~
请核验患者 {id} 的**{分片名}标准**判定结论。

⛔ **本轨边界（硬规则）**：只审 {分片名}。禁止读写另一轨的任何产物——看到对侧文件存在**不是**
做对侧的理由（两轨并行，对侧可能正在被写入）。QC **不改写任何产物**，只出 `qc_report`。
⛔ 只读自检用 `read_file` 或 `apply_json_patches` 的 `{"op": "get"}`，不要用 `bash` 现写 python
统计——口径每次现写就会漂移。

⛔ 开工前置自检（第一步，必做）：
read_file /mnt/user-data/workspace/patients/{id}/judgment_structure_gate_{id}_{SHARD}.json。
- `exit_code != 0` → **立即停止并返回**「前置结构闸未过（exit_code=N，problems=…），拒绝执行本轮 QC」，不要开始核验。
- 文件不存在 → 同样返回「结构闸未运行，拒绝执行」。
- 读完 judgments_draft 后，用 `bash sha256sum` 比对其前 16 位与闸产物的 `content_sha256_16`；
  不一致说明闸跑完后判定文件又被改过 → 返回「闸产物已失效，需重跑结构闸」。
- 另查两条机械闸产物是否**真空通过**：`uncertain_recheck_*.json.unreadable_judgments == true`、
  或 `reason_alignment_*.json` 的 conflicts 含 `unreadable_judgments`、或其 `coverage` 分子远小于分母
  → 说明判定文件结构读不出来、这两条闸的「全过」不成立 → 返回「机械闸真空通过，拒绝执行本轮 QC」。
（结构问题会被语义 QC 当成语义问题报出来，白吃掉一轮配额；历史故障 thread `345f2bf4`：
结构闸与 QC 同轮发出，JSON 语法错误占掉了 criteria 侧 R2 一整轮。）

输入文件（自行 read_file，路径已给全，禁止 ls/glob/find 探索；每份最多读一次）：
- 结构闸产物（上方前置自检用）：/mnt/user-data/workspace/patients/{id}/judgment_structure_gate_{id}_{SHARD}.json
- 本轨判定结论：/mnt/user-data/workspace/patients/{id}/judgments_draft_{id}_{SHARD}.json
- 本轨机械反查产物（O1 兜底闸）：/mnt/user-data/workspace/patients/{id}/uncertain_recheck_{id}_{SHARD}.json
- 本轨 reason 对齐校验产物（原则十）：/mnt/user-data/workspace/patients/{id}/reason_alignment_{id}_{SHARD}.json
- 【仅 EX 轨】排除项方向校验产物（原则九 C）：/mnt/user-data/workspace/patients/{id}/exclusion_direction_check_{id}_EX.json
- 本轨标准包（核对条件语义/阈值）：/mnt/user-data/workspace/criteria_judge_{SHARD}.json
- **取证素材包（先读它，取证的默认入口）**：/mnt/user-data/workspace/patients/{id}/evidence_bundle_{id}_{SHARD}.md
  已按条目装配好：条件原文 + 标准包锚点 + 当前判定 + reason + **每条 evidence 引文的逐字核验结果**
  （✅ 带 `source:行号` / ❌ OCR 中未找到）+ 合并去重后的 OCR 命中窗口。
- 该患者 OCR 原文（**仅在证据包不足时按行号定点补读**，禁止读 uploads 原始 PDF）：
  - /mnt/user-data/workspace/patients/{id}/ocr/{source1}/ocr_records.md
  - /mnt/user-data/workspace/patients/{id}/ocr/{source2}/ocr_records.md

⛔ **取证方式硬规则**：先读证据包，再核验。**禁止**逐条 `grep` + `read_file` 去找证据 ——
那会把核验拆成几十步，而**每一步都要重传此前所有步的上下文**（会话 `93d8a2c6` 实测重传倍数
18×~30×）。同一会话的 `IN judgment QC round 2` 就是这么烧掉 1.97M token 并耗尽 150 步额度
**失败**的：31 次 read_file + 15 次 grep、零个闸脚本。
证据包不够用时，只对**个别**条目按它给出的行号定点补读，不要整篇重读、不要重新 grep 全文。

核验清单（**第 1 轮全跑；第 2/3 轮仅对上一轮 blocking_issues 涉及的条目 + 两个机械闸产物重跑结果适用**）：
0. **引文可溯源（阻断级，看证据包的「引文核验」列即可，⛔ 不要自己 grep 复核）** —
   任何标 `❌ OCR 中未找到` 的引文都是阻断项：引文要么来自**别的患者**、要么是**编造**
   （历史故障 thread `81562273`：M018 的 reason 引用了只见于其他患者的 133 / 80.1）。
   逐条报出 `条件ID` + 该引文，`action` 写「按 OCR 原文改为真实值并补 evidence 引用；
   若本就无该项检查，改判为「无法判断」并写明缺失项」。
   ⚠️ 证据包的「引文核验」已做过空白/全半角归一，✅ 就是真的逐字可溯源，不必重验。
1. 数值比对正确性 — 运算符方向是否正确，数值是否准确
2. 逻辑一致性 — AND/OR 组合结论是否符合规则；排除项的"或"须按结论空间的 AND 汇总（任一子条件"不符合"→ 整条"不符合"）
2b. **`或组` 汇总方向（阻断级）** — 带 `或组`/`或组语义` 的条目是同一原文标准的 OR 异质替代分支，须**按组**汇总而非逐条 AND：排除轨组内任一 `不符合`（触发）→ 整组触发；入选轨组内任一 `符合` → 整组满足，其余分支的「无法判断」**不构成障碍**。⛔ 若发现入选 `或组` 被按"全部符合"汇总（导致整体判为不符合入选/需补充），即为**阻断级**错误——那会把"满足其一即可"变成"必须全部满足"，错误淘汰患者。同组分支缺失（带 `或组` 却无同组兄弟）同样阻断，说明切包丢字段或解析漏拆。
3. 证据充分性 — 每条判定是否有证据原文支撑
4. **【仅 EX 轨】排除项结论方向（阻断级，机械核验优先）** — 语义约定：排除项 `符合` = 排除**未触发**（可入选）；`不符合` = 排除**被触发**（应排除）。逐条核验：
   a. 先核对 `exclusion_direction_check_{id}_EX.json`：`conflicts` 中每个条件ID 都是**阻断级方向反转**，必须按其 `expected_conclusion` 与 reason 语义要求改判（同时改 `conclusion` + `exclusion_triggered` + reason 措辞）；`advisories` 为建议级（reason 未显式声明"触发/未触发"或方向弱信号），要求补齐措辞。
   b. 再用 LLM 语义复核脚本未覆盖的角度：对每条 `conclusion=不符合` 的排除项，逐条问"该 reason 是否真的在陈述'该患者命中了这条排除标准'？"——若 reason 实际在陈述"未见/阴性/正常/未描述/无相关记录"，即为方向反转，标**阻断级**；对每条 `conclusion=符合` 的排除项，同样确认 reason 不是在陈述"存在该排除情形"。
   c. 一致性回推：把所有 `不符合` 的排除项列出，问"是否确实建议排除这些患者"（约束 #17）；若整体建议与 `不符合` 数量明显矛盾，说明存在方向污染，标阻断级。
   d. 排除项在证据不足时应为 `存疑`/`无法判断`，不得用 `不符合` 表达"看起来不像被排除"。
5. 【仅 IN 轨】入选项判定语义 — 入选项 `符合` = 条件满足；未提及不可默认符合
5b. **从严判断（阻断级）** — 逐条核验负面结论是否有病历原文支撑：对每条 `不符合`，问"这是病历
   证明了它不成立，还是只是没找到它成立的证据？"。后者即为**阻断级**错误，须改判为「无法判断」
   （或「存疑」）并列明待补材料。入选项判 `不符合` 等于淘汰患者、排除项判 `不符合` 等于建议排除，
   两者都不允许由"查不到"推出。
5c. **「不可从病例获取」条目必须已核查（阻断级）** — 对每条 `可从病例获取=false` 且结论为
   「无法判断」的条目，核对 `uncertain_recheck_{id}_{SHARD}.json` 中该条的 `grep_hits`：
   - `hit=true` → 病历里其实有客观记录（知情同意签署时间、自愿参加声明、承诺记录、预后评估等），
     属**阻断级漏判**，须据命中行原文改判；
   - `hit=false` 且 `no_keywords` 不为 true → 确已机械检索且无记录，可保留「无法判断」，
     但 reason 必须含三要素；
   - `no_keywords=true` → 该条派生不出可用关键词（如纯"研究者判断"），属"查不了"而非"查过没有"，
     需你用语义判断病历中是否有等价痕迹，不得直接放过。
   ⚠️ 特别留意 reason 写"已查 X"但证据其实在 Y 文档的情况（S042002 IN-1：reason 称"已查筛选期
   检查"，而知情同意记录在筛选期病历里），这类是**伪造检索过程**，一律阻断。
6. 物料间一致性 — 基本信息是否一致。统一证据源判定下该检查**已在判定时完成**（矛盾记入 reason 与 `warnings`，见 `judgment-principles.md` 原则一）：核验判定产物 `warnings` 里点名的矛盾是否已在对应条目的 reason 中说明，未说明的列为阻断项
7. 遗漏检查 — 是否有明显应判但标为"无法判断"的条目；本轨条目数是否等于本轨标准包 `条件数`
8. **漏判反查（阻断级，机械核验优先）** —
   - 首先核对 `uncertain_recheck_{id}_{SHARD}.json` 的 `suspected_missed`：其中每个条件ID 都是"证据关键词在 OCR 命中却仍判无法判断"的**阻断级漏判**，必须据其 `grep_hits` 原文要求改判（给出应改判结论 + 证据原文 + 页码）。
   - 其次对未覆盖的「无法判断」条目（如脚本缺失时），按其"可从病例获取"条件的同义词/匹配字段，`grep`/`read_file` 上方 OCR 路径复核；命中即标阻断级漏判。

9. **reason ↔ 条件ID 对齐（阻断级，机械核验优先）** — 核对 `reason_alignment_{id}_{SHARD}.json`：
   `conflicts` 中每条都是**阻断级**（`cross_condition_reason` 张冠李戴 / `no_anchor_hit` 标准包锚点零命中 /
   `unsourced_number` 数值无据 / `duplicate_reason` 理由逐字重复 / `empty_reason`），必须按其 `action` 要求改判；
   `advisories`（回退锚点零命中）为建议级，需你用语义判断该 reason 是否真的在讲该条件 —— 是则放过、
   不是则升级为阻断项写入 `blocking_issues`。
   ⚠️ 该闸只能证明"理由在讲哪个条件"，证明不了"判定对不对"：词面重叠但未引用关键证据的条目
   （如"进展性 mCRPC"只提到软组织/骨、未提"进展"证据）闸抓不到，**仍须你逐条语义核验**。

10. **药物/治疗归类的依据与适用对象（阻断级）** — 对每条 reason 声称了药物归类
   （"…属于全身性药物治疗/新型内分泌治疗/紫杉类/免疫抑制剂…"）的条目，核验两件事：
   a. **归类有据**：evidence 里是否有来源（外部查证的标题/URL，或病历自述原文）。
      `reason_alignment_*.json.advisories` 中的 `unsourced_drug_class` 会点名这类条目；
      无据即要求判定侧补 `web_search` 查证结果（⛔ 提醒其查询串不得含任何患者信息）。
   b. **归类对象正确（更易错，务必逐条问）**：该药物是否**针对本条所述的疾病/情形**？
      对"有 X 病史 **且** 仍需 Y 治疗"这类条件，按**判定规则 §原则十一 B 的三步判据**核验：
      ①病历有该病史 ②有**针对该病史**的治疗记录（再看归类）③查不到→`存疑`。
      两类误用都是**阻断级**，要求改判并改写依据：
      - 拿患者恰好在用的全身**抗肿瘤**药去满足"仍需全身性药物治疗"——肿瘤试验候选者
        人人在用全身抗肿瘤治疗，按那种读法这条排除标准会排掉 100% 的人；
      - 拿研究者写的"筛选失败 / 不适合入组"当依据——那是**结论不是治疗记录**，落不到②。
      ⛔ 本模板不给任何具体条件的结论，正例反例看 §原则十一 B（
      `/mnt/skills/custom/eligibility-judgment/references/judgment-principles.md`，QC 子代理
      `skills` 亦为 `[]`，须自行 read_file）与
      `references/schema_example.json` 的 EX-1-3 示例。
   c. **缺失断言的真伪**：reason 若写"未见/无……记录"，该类别的关键词是否真的不在 OCR 里？
      `reason_alignment_*.json.advisories[].false_absence_claim` 会点名此类条目——
      结论可能仍正确（如该药不针对本条所述疾病），但 reason 必须改成"病历有 X，
      但它治的是 Y、不算"，不得保留事实错误的缺失断言。
   d. **时间窗条件的悬置理由**：结论为「无法判断/存疑」而理由是「缺参考日期」时，
      先问**事件到底发生过吗** —— 事件零命中则时间窗不适用，任何参考日期都不能让一个
      不存在的事件落进窗口。EX 轨应判 `符合`（未触发）、IN 轨负向要求（「未接受 X」）应判
      `符合`（SKILL「日期/时间窗判定」C 条）。`reason_alignment_*.json.advisories[].window_moot_absence`
      会点名此类条目，逐条核：⛔ 但若事件其实**存在**、只是措辞与锚点不同（如"重大外科治疗"
      vs 病历"冷冻切除术"），则要求改写 reason 引用那段原文，**不要**据此改判。

⚠️ QC 纪律：语义/漏判核验必须用 LLM 推理 + 对 OCR 的 grep/read_file 完成，**禁止**编写/执行 Python 脚本做语义修订（`uncertain_recheck.py`、`exclusion_direction_check.py` 已在判定阶段跑完，QC 只核对其产物，不重跑、不改写）。**禁止** `task` / `present_files`。

⚠️ 阻断级判定口径：本轨 `exclusion_direction_check_*.json.conflicts` 非空、或 `uncertain_recheck_*.json.suspected_missed` 非空、或 `reason_alignment_*.json.conflicts` 非空 → QC **一律不通过**（`passed=false`），不得以"带建议放行"绕过。

⚠️ 轨道边界：**禁止**读对侧轨的任何产物、全量 `criteria_parsed.json`、`phase*_summary.json`、其他患者目录。

输出：`write_file` 到 `/mnt/user-data/outputs/qc_report_{id}_{SHARD}.json`（结构见下节），result 只回传**该路径 + `passed` + 阻断问题条件ID 列表**，禁止回传判定正文。

⛔ **委派时必须带 `expected_outputs`**（`task` 参数，机械后置校验）：

```
expected_outputs=["/mnt/user-data/outputs/qc_report_{id}_{SHARD}.json"]
```

harness 在子代理被允许报 `completed` 之前探一次该文件：不存在或为空 → 该 task 判 `failed`
并点名缺失路径，`task` 自动重派一次。它替主代理挡住「QC 子代理只在 result 里描述了结论、
报告其实没落盘」这一类失败 —— 与会话 `88df83a8` 里判定子代理写错文件名同一类根因
（见 `judge-delegation.md`「委派时必须带 `expected_outputs`」）。

输出格式：
```json
{
  "issues": [
    { "id": "QC-001", "severity": "高|中|低", "condition_id": "IN-10-7", "finding": "...", "action": "..." },
    { "id": "QC-002", "severity": "高", "condition_id": "EX-10", "issue_type": "排除项方向反转",
      "finding": "reason 陈述'未见活动性感染、HBsAg/HCV/HIV 阴性'（未触发），conclusion 却为'不符合'（=被触发/应排除），方向相反",
      "action": "改判 conclusion 为'符合'，补 exclusion_triggered=false，reason 末尾加'未触发该排除条件'" }
  ],
  "confirmed_correct": ["IN-2-1", "IN-2-2", ...]
}
```
~~~

## QC 报告结构（`qc_report_{patient_id}_{SHARD}.json`）

QC 结论必须落盘为结构化报告，**`passed` 由机械闸决定，不由主观判断决定**：

```json
{
  "patient_id": "M016_ZALO",
  "track": "EX",
  "round": 1,
  "passed": false,
  "objective_checks": {
    "condition_count": 23,
    "judgment_file_exists": true,
    "recheck_file_exists": true,
    "direction_check_file_exists": true,
    "suspected_missed_empty": true,
    "reason_alignment_file_exists": true,
    "reason_alignment_conflicts_empty": false,
    "reason_alignment_conflicts": ["IN-6", "IN-11"],
    "reason_alignment_advisories": ["IN-13-2"],
    "exclusion_direction_conflicts_empty": false,
    "exclusion_direction_conflicts": ["EX-10", "EX-12", "EX-15", "EX-16"],
    "exclusion_direction_advisories": []
  },
  "blocking_issues": [
    { "id": "QC-001", "type": "排除项方向反转", "condition_id": "EX-10",
      "finding": "reason 为'未见活动性感染、HBsAg/HCV/HIV 阴性'（未触发），conclusion 为'不符合'（=被触发/应排除）",
      "action": "改判 conclusion='符合'，exclusion_triggered=false，reason 末尾补'未触发该排除条件'" }
  ],
  "residual_issues": [],
  "semantic_review": { "conclusion": "...", "focus": ["排除项结论方向", "时间窗条件", "研究者判断条款"] }
}
```

**`passed` 计算规则（确定性，禁止人工覆盖）**：

```
passed = blocking_issues 为空
         AND 本轨 uncertain_recheck_*.json.suspected_missed 为空
         AND 本轨 reason_alignment_*.json.conflicts 为空
         AND 【仅 EX 轨】exclusion_direction_check_*.json.conflicts 为空
```

- 上述任一机械闸非空 → `passed=false`，**不得**以"达 QC 轮次上限，带建议放行"绕过
  （这两类是可机械证实的客观错误，不是主观偏好）；
- `objective_checks` 中的 `suspected_missed_empty` / `reason_alignment_conflicts_empty` /
  `exclusion_direction_conflicts_empty` **必须**从对应产物文件实际读取填写，禁止凭印象写 `true`；
- EX 轨的 `semantic_review.focus` 必须包含「排除项结论方向」；
- IN 轨没有方向校验产物，`direction_check_file_exists` 填 `false`、
  `exclusion_direction_conflicts_empty` 填 `true`（不适用）。
