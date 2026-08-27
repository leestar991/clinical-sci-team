---
name: eligibility-judgment
description: >
  入排判定规则技能 — 统一证据源逐条判定入排匹配程度，4级判定体系（符合/不符合/存疑/无法判断），证据可追溯，含QC标准。
  触发: '入排判定', '逐条比对', '匹配判定', 'eligibility judgment', '/eligibility-judgment'
---

# 入排判定规则

## 概述

本技能定义入排标准与患者病历逐条比对的判定规则：4级判定体系、统一证据源原则、证据追溯、质控标准。

## 输入资料（唯一判定依据）

判定由子代理**只依据以下两类输入**完成，其余文件（试验方案原文、uploads 原始 PDF/MD、脚本、中间产物、QC 结果等）**均不得作为判定证据来源**：

1. **结构化入排标准** — 本轨的**判定输入包** `criteria_judge_IN.json` / `criteria_judge_EX.json`
   （由 `/criteria-parser` 的 `parse_pack.py slim` 按轨产出）
   - 四分类条目 + 每条 `原文` / `子条件` / `逻辑关系` / `可从病例获取` / `转化条件`（含可选 `证据位置`/`同义词`）/ `日期维度`。
   - 标准原文已内嵌在 `原文` 字段，**无需**再读试验方案。
   - **双轨判定时**每个子任务**只读自己那一轨**的包，**禁止**读全量 `criteria_parsed.json`、
     对侧轨的包——包保留 `四分类` 外层结构，技能内所有规则与两个机械闸脚本均可直接消费。
   - 全量 `criteria_parsed.json`（`assemble` 产物）只用于**报告生成**与**合并后全量终检**，
     不作为判定子代理的输入。

2. **OCR 识别后的患者病历及检查报告** — 两种形态，**取决于用户在编排层选定的处理模式**，由编排层给出实际路径：
   - **整份解析产物**（单患者 + 一次性整份 OCR，`/pdf-image-extractor` 路线 A）：
     `/mnt/user-data/workspace/ocr/{source}/{source}_full.md`，首行为 `（来源文档：...）`、**无分页页块**
     → 该来源的证据 `page` 留空、无 `screenshot_ref`（见 `references/judgment-schema.md`「证据截图引用」的整份解析例外）。
   - **按患者聚合的分页汇总**（单患者 + 逐页 OCR，或多患者混合 + 逐页 OCR）：
     `/mnt/user-data/workspace/patients/{patient_id}/ocr/{source}/ocr_records.md`，含逐页「来源图片」页块
     → 证据**必须**带 `page` 与 `screenshot_ref`。

3. **【仅分页聚合模式】页码索引** — `patients/{patient_id}/ocr_page_index.json`
   （聚合后由 `/patient-separator` 的 `scripts/ocr_page_index.py` 机械产出）
   每页给出 `page`/`image`/`start_line`/`end_line`，**1-based 闭区间**，直接作 `read_file` 的行区间。
   **先读它再决定读哪些行**（它很小，第一轮与标准包并行读掉）：⛔ 有了它就不要再 grep 摸页边界、
   不要为"看看有什么"整份读几千行的 `ocr_records.md`
   （代价见 `references/failure-archive.md`#整轨一次判定撞-recursion_limit）。
   路线 A 无页块、不产此文件，不要去找。

   下文凡提到 `ocr_records.md` 的规则（一次读取、兜底闸检索范围、页块定位等），对整份解析产物同样适用，只是没有页块可定位。
   ⛔ **判定阶段不选模式、不猜模式**：拿到哪种路径就按哪种处理；**禁止**因为想要页码而去 `images/` 自行逐页 OCR，也**禁止**在整份解析模式下编造 `page`。
   - 按患者、按来源（如"筛选期病历""筛选期检查"）聚合的 OCR 原文，每个 `{source}` 一份；prompt 内逐个列出该患者实际存在的路径。
   - **兜底**：仅当结构化表单/手写页 OCR 文本不足或疑似错抄时，可 `view_image` 该页原图——路径取自 `ocr_records.md` 内每个页块的「来源图片」字段（`/mnt/user-data/workspace/images/{source}/{stem}_page_{NNN}.{jpg|png}`，扩展名以实际文件为准）；这是对 OCR 文本的核对，不算读取 uploads/原始 PDF。**每轮 ≤ 2-3 张**（payload 硬限制）。
   - **`ocr_records.md` 页块结构**：每一页以一行 `来源图片：{该页原图绝对路径}` 作为**页块起始行**（在该页正文之前），文件名内的 `_page_{NNN}` 即该页页码；部分页可能另有 `第 N 页` 文本行，但**页码以文件名 `_page_{NNN}` 为准**（`第 N 页` 可能缺失/错位）。判定时定位证据原文所在页块（取该证据上方、下一条 `来源图片` 之前的那条 `来源图片` 行），即得该页原图路径与页码——此字段是 `evidence[].screenshot_ref` 的**唯一来源**（取值规则见 `references/judgment-schema.md`）。

> 判定不再经过独立的证据提取步骤（已取消 data-extractor）：子代理一步完成"读取该患者 `ocr_records.md` → 逐条判定"。

### 子代理上下文纪律（硬约束，直接影响时延）

子代理的上下文 = **prompt + 它自己产生的工具调用历史**，主代理的历史不会带过去。
因此时延的决定因素是"给它读多少、让它跑多少轮"：

- **只给必要输入**：判定子代理**只允许**读上述 3 类文件。**禁止**读全量 `criteria_parsed.json`、
  对侧轨的判定输入包、**本轨其它批次的产物**（`_b{其它批号}`）、任何 `phase*_summary.json`、
  `patient_index.json`、`criteria_qc_*.json`、其他患者目录、`uploads/`。
- **每份输入文件本任务内最多 `read_file` 一次**；多份 `ocr_records.md` **同轮并行**读取。
  按页码索引读**不同页区间**不算重复读，同一区间不得读第二遍。
- **禁止在 prompt 内内联正文**：prompt 只给**绝对路径**与规则，不粘贴标准原文/OCR 正文
  （内联会让同一份内容在上下文里出现两次）。
- **判定结果一次 `write_file` 落盘**，不分多次追加改写。
  ⚠️ 仅限**首次**落盘；QC 后**改判**已有判定走相反规则——只允许 `apply_json_patches` 的
  对象级形态、禁止 `write_file` 与 `str_replace`，见「QC 后改判」与 `references/judgment-repair.md`。
- ⛔ **禁止用 `bash` 内联脚本（`python3 -c`、heredoc、`echo >`）生成或改写任何 `.json` 产物。**
  结构化产物只能由 `write_file`（首次）或 `apply_json_patches`（修订）写。理由见
  `references/failure-archive.md`「内联脚本生成 JSON」。bash 只用来跑闸脚本与看文件。
- **`parse_document` 是工具调用入口、非 Python 模块**，禁止 import/find_spec 探测。
- 禁止 `task` / `present_files`；禁止 `ls`/`glob`/`find` 探索（路径已给全）。
- **result 只回传摘要**：产出文件路径 + 结论计数 + 闸门状态。**禁止**回传判定条目正文、
  reason、证据原文。

### references 索引（按需加载，勿全量读）

| 文件 | 何时读 |
|---|---|
| `references/judge-delegation.md` | 派逐条判定子任务时（按轨参数化的委派模板） |
| `references/qc-delegation.md` | 派 QC 子任务时（委派模板 + 核验清单 + `passed` 计算规则） |
| `references/judgment-repair.md` | QC 后改判时（改写硬规则、机械闸、委派模板、故障档案） |
| `references/judgment-schema.md` | 落盘/校验判定产物结构时（字段契约、`evidence` 形态、`screenshot_ref` 取值、`exclusion_triggered` 配对、`或组` 汇总口径、与各闸的对应关系） |
| `references/judgment-principles.md` | **判定规则唯一权威**：核心原则一~十一、数值/逻辑/时间窗判据、判定约束清单（42条）。派判定/改判子任务时由模板指向它 |
| `references/schema_example.json` | 需要可直接对照抄写的完整 JSON 样例时 |
| `references/failure-archive.md` | 需要知道某条硬规则背后是哪次故障时（按需读单节，⛔ 勿整篇加载） |

**规则去哪查**（本 SKILL.md 只管**编排**：分片、派发、验收、合并、交付。「一条标准怎么判」全部在
`references/judgment-principles.md`）：

| 要找什么 | 去哪 |
|---|---|
| 统一证据源 / 证据可追溯 / 保守判定 / 按概念取证 / 禁止伪「无法判断」/ 图片兜底（原则一~六） | `judgment-principles.md` §原则一~六 |
| 从严判断、证据缺失不得转负面结论（原则七之前） | `judgment-principles.md` §原则七之前 |
| 判「无法判断」前的穷尽取证与三要素理由（原则七） | `judgment-principles.md` §原则七 |
| `uncertain_recheck.py` 兜底闸的触发与命中处理（原则八） | `judgment-principles.md` §原则八 |
| 排除项方向自检与 `exclusion_direction_check.py`（原则九） | `judgment-principles.md` §原则九 |
| `check_reason_alignment.py` 对齐闸与冲突处理（原则十 A/B/C） | `judgment-principles.md` §原则十 |
| 药物/治疗归类三步判据、`web_search` 用法、正反例（原则十一 A/B/C） | `judgment-principles.md` §原则十一 |
| 数值比对 / 逻辑关系（AND·OR·或组）/ 日期时间窗 / 适用臂 | `judgment-principles.md` §数值比对 起 |
| 落盘前必扫的 42 条判定约束（delegation 与 QC 按编号引用） | `judgment-principles.md` §判定约束清单 |

⛔ **判定/改判子代理的 `skills` 是 `[]`**，它不会自动加载任何 SKILL.md。规则到达子代理的唯一通道是
委派模板——模板必须**原样复制**并指向 `references/judgment-principles.md` 的具体小节，
⛔ 不要在模板里转述规则（转述即漂移）。

## 交付文件清单

判定相关产出统一移动到 `/mnt/user-data/outputs/` 并 `present_files`：

- `outputs/judgments_{patient_id}.json` --最终判定（必交付，两轨合并后产物）
- `outputs/qc_report_{patient_id}_{IN|EX}.json` --按轨 QC 报告（过程文件，需交付）
- `outputs/uncertain_recheck_{patient_id}.json` --「无法判断」确定性反查产物（O1 兜底闸，两轨合并后；过程文件，需交付）
- `outputs/exclusion_direction_check_{patient_id}.json` --排除项结论方向确定性校验产物（原则九 C，合并后全量终检；过程文件，需交付）
- `outputs/reason_alignment_{patient_id}_{IN|EX}.json` --reason ↔ 条件ID 对齐校验产物（原则十，按轨；过程文件，需交付）

> 轨道中间文件（`judgments_draft_{id}_{IN|EX}.json`、`uncertain_recheck_{id}_{IN|EX}.json`、
> `reason_alignment_{id}_{IN|EX}.json` 的 workspace 副本）
> 是过程产物，**不交付、不 present**，合并后由上表文件代表。

**规则**：每个文件仅 `present_files` 一次；调度 `task(quality-control)` 时在 prompt 内显式传入输入文件绝对路径，并要求子代理将产出移动到 `outputs/` 且在 result 中声明产出文件路径清单。

## 判定分片与合并（双轨并行 + 上下文最小化）

入选项与排除项的判定彼此独立（逐条独立判定，无跨组依赖），因此从标准解析一路到判定、QC
都按 **IN 轨 / EX 轨** 双轨并行；每个子任务只吃一半标准包 → 并行度 ×2、
单任务输入减半、轮次与时延同步下降。**合并只发生一次**：在两轨的判定与 QC 都完成之后。

### 轨道产物与合并点

`{T}` = `IN`/`EX`；`{N}` = 批号。完整路径清单见 `judge-delegation.md`「轨道产物命名」。

| 阶段 | 产物（两轨各一份） | 合并 |
|---|---|---|
| 标准输入包 | `criteria_judge_{T}.json` | —（全量 `criteria_parsed.json` 由 `assemble` 另行合成，供报告与全量终检） |
| 批次清单 | `judge_batches_{id}_{T}.json` | —（纯派发信息，⛔ 不切标准包） |
| 逐条判定（含 `reason`，**按批**） | `judgments_draft_{id}_{T}_b{N}.json` | **两次**：①各批 → 本轨 draft ②两轨 → `outputs/judgments_{id}.json` |
| 机械兜底闸（**按批**） | `uncertain_recheck_*` + `reason_alignment_*`（+【EX】`exclusion_direction_check_*`），均带 `_b{N}` | `uncertain_recheck` 随①合并 |
| QC（**整轨口径，不分批**） | `qc_report_{id}_{T}.json` | 否 |

⛔ **中途不合并**：判定完成后**不要**立刻把两轨拼起来。两轨各自走完 QC，
最后一次性合并 + 全量终检（见下方「合并与终检」）。

### 标准输入包（解析收尾 `slim` 产出，本技能消费）

产出只保留本轨 `四分类` 类目与判定必需字段（条件ID/来源标准/原文/子条件/逻辑关系/
可从病例获取/转化条件/日期维度/非空备注），剔除 `方案元数据`/`解析说明`/`汇总统计`/`描述索引`。
**保留 `四分类` 外层结构**（每个类目是以 `条件ID` 为键的对象），因此 `uncertain_recheck.py`、`check_reason_alignment.py` 与 `exclusion_direction_check.py`
可直接以该包作 `--criteria`。

闸门（任一不过即 `exit 2` 且不产出任何文件）：

| 闸门 | 拦什么 | 可否绕过 |
|------|--------|----------|
| QC 闸 | 该轨 `criteria_qc_{TRACK}.json` 的 `passed != true`、`blocking_issues` 非空，或检出「带建议放行 / 轮次上限 / `round_limit_released`」自我放行痕迹 | 仅 `--force-qc-unconverged`，且**只在用户明确知情同意后**使用 |
| 单轨结构闸 | 本轨缺本轨类目；含对侧类目条目；条件ID 前缀与轨不符 | **不可绕过** |
| 产出闸 | 该轨 `条件数 == 0` | **不可绕过** |

> 设计意图：判定输入包是逐条判定的**唯一**标准来源，一旦在 QC 收敛前或四分类归属错误时切出，
> 错误会按「条件 × 患者」放大到每一条判定，且下游 QC 兜不住「标准本身就错」。
> 故障档案：`references/failure-archive.md`#切分与-qc-同轮发出

**切分后自检**：`IN 条件数 + EX 条件数` 应等于全量包 `汇总统计.子条件总数`；
不等说明标准结构仍有问题，回 QC/修订，不要带残缺包开判。

### P3-prep（plan-batches ×2 + render ×2，机械单次 bash）

判定的输入准备全是机械操作，合为单次 `bash`（`set -e` 包裹），主代理不得亲做逐条命令、
不得回读产物全文（`judgment-date` 由主代理 `date -I` 取一次，同批共用）：

```bash
set -e
P=/mnt/user-data/workspace/patients/{id}
python3 /mnt/skills/custom/eligibility-judgment/scripts/judge_pack.py plan-batches \
  --criteria /mnt/user-data/workspace/criteria_judge_IN.json \
  --patient-id {id} --track IN --out-dir $P/
python3 …/judge_pack.py plan-batches \
  --criteria /mnt/user-data/workspace/criteria_judge_EX.json \
  --patient-id {id} --track EX --out-dir $P/
python3 …/render_judge_prompt.py --patient {id} --track IN \
  --judgment-date $(date -I) \
  --doc-key "筛选期病历=$P/ocr/筛选期病历/ocr_records.md" \
  --doc-key "筛选期检查=$P/ocr/筛选期检查/ocr_records.md" \
  --out-dir $P/prompts/
python3 …/render_judge_prompt.py --patient {id} --track EX \
  --judgment-date $(date -I) \
  --doc-key "筛选期病历=$P/ocr/筛选期病历/ocr_records.md" \
  --doc-key "筛选期检查=$P/ocr/筛选期检查/ocr_records.md" \
  --out-dir $P/prompts/
```

`--doc-key` 形如 `"来源名=OCR路径"`（可重复，来源名逐字取 phase2_summary 的
`ocr_results[].source`，两轨同一套、顺序一致）。

### 分片判定（双轨 × 轨内 12 条一批）

判定任务矩阵 = **患者 × 轨 × 批次**。双轨是语义边界（不可跨），轨内再按 **12 条一批**细分：

```bash
python3 .../judge_pack.py plan-batches --criteria .../criteria_judge_{IN|EX}.json \
  --track {IN|EX} --patient {id} --batch-size 12 \
  --out .../patients/{id}/judge_batches_{id}_{IN|EX}.json
```

每批一个 `task`，四类产物均带 `_b{N}` 后缀（清单见 `judge-delegation.md`「轨道产物命名」），
批级结构闸加 `--batch {N}`。各批到齐后 `merge-judgments` 合成本轨 draft，
再跑**整轨口径**结构闸（⛔ 不带 `--batch` —— 唯一会因"漏派一整批"报错的地方），
`exit 0` 后才进入 QC。QC 与改判一律走**整轨**口径，不分批。

**为什么分批**：整轨一次派会让单任务撞 `recursion_limit` 且**整单作废**（该分支不打捞部分
产物）；分批后每批各自落盘，撞限只损失一批。实测见
`references/failure-archive.md`#整轨一次判定撞-recursion_limit。

⛔ **不切标准包、不按 `四分类` 类目切、批次不跨轨** —— 判据见
**`references/judge-delegation.md`**「批次拆分」（切包会同时废掉闸 2 的整轨恒等校验
与 `merge-judgments` 的或组重算）。委派模板同一文件。

### 合并与终检（两轨 QC 都完成之后，机械操作，不改结论/理由）

⚠️ 这是**第二次**合并（两轨 → 最终）。第一次是各批 → 本轨 draft，在判定阶段末尾、QC 之前，
命令见 `references/judge-delegation.md`「各批完成后」。

```bash
P=/mnt/user-data/workspace/patients/{id}
# 合并两轨 → 最终判定（reason 已在判定阶段写入 draft，无需回填）
python3 .../judge_pack.py merge-judgments --shards $P/judgments_draft_{id}_IN.json $P/judgments_draft_{id}_EX.json \
  --criteria /mnt/user-data/workspace/criteria_judge_IN.json /mnt/user-data/workspace/criteria_judge_EX.json \
  --out /mnt/user-data/outputs/judgments_{id}.json
python3 .../judge_pack.py merge-recheck --shards $P/uncertain_recheck_{id}_IN.json $P/uncertain_recheck_{id}_EX.json \
  --out /mnt/user-data/outputs/uncertain_recheck_{id}.json
```

两轨**顶层 `judgments` 直接合并**、重算顶层 `summary`（条件口径）、条件ID 自然排序、去重 warnings，
并**一次全量重算主条件组级汇总** `criteria_rollup` / `rollup_summary`（见「逻辑关系处理」）。
合并不改 `reason`/`conclusion`/`evidence`/`exclusion_triggered`——`reason` 在落盘 draft 时一次写定。

**合并后终检（硬闸）**：对合并结果再跑一次 `exclusion_direction_check.py`
（`--criteria` 用 `assemble` 出的全量 `criteria_parsed.json`），防止分片改写/合并引入方向回归：

```bash
python3 .../scripts/exclusion_direction_check.py \
  --judgments /mnt/user-data/outputs/judgments_{id}.json \
  --criteria  /mnt/user-data/workspace/criteria_parsed.json \
  --out       /mnt/user-data/outputs/exclusion_direction_check_{id}.json
```

`conflicts` 非空 → 回派对应轨改判，清空后方可交付报告。这一步同时是**跨轨语义的机械兜底**
（两轨 QC 各自看不到对侧，方向反转是最需要全局复核的一类错误）。


## 4级判定体系

| 判定 | 图标 | 入选标准含义 | 排除标准含义 |
|------|------|-------------|-------------|
| **符合** | 🟢 | 该入选条件被满足 | 该排除条件**未被触发**（患者可入选）|
| **不符合** | 🔴 | 该入选条件未被满足 | 该排除条件**被触发**（患者应被排除）|
| **存疑** | 🟡 | 找到相关内容但不足以确定 | 找到相关内容但不足以确定 |
| **无法判断** | ⚪ | 全部材料中无对应内容 | 全部材料中无对应内容 |

### ⚠️ 排除项语义是**反直觉**的（最高频故障，务必先读）

`符合 / 不符合` 在排除项上**不是**"与标准文字描述是否一致"，而是"**与可入选是否一致**"：

| 病历事实 | 口语直觉（❌ 错） | 本技能唯一正确写法 |
|---|---|---|
| HBsAg/HCV/HIV/梅毒全阴性，无活动性肝炎 | "不符合该排除标准" → 写 `不符合` | ✅ `符合`（排除**未触发**，可入选）|
| 确诊活动性肺结核，正在抗结核治疗 | "符合该排除标准" → 写 `符合` | ✅ `不符合`（排除**被触发**，应排除）|

**记忆口诀**：**「符合 = 符合入组」**——不论入选项还是排除项，`符合` 永远意味着"这条对入组有利"，`不符合` 永远意味着"这条挡住入组"。

⚠️ **这是本技能最高频、后果最严重的单点错误**：语义写着"未触发"、`conclusion` 却写 `不符合`，
按约束 #17 就等于宣告应排除，整体结论被反向污染。纯提示兜不住，因此额外设置原则九
（措辞绑定 + 机械校验闸）。故障档案：`references/failure-archive.md`#排除项方向写反

### 判定选择标准

- **符合**：证据明确、数值在阈值范围内、条件清晰满足（排除项：确认未触发）
- **不符合**：证据明确、数值超出阈值、条件明确不满足（排除项：确认被触发）
- **存疑**：有相关数据但属于边界值、数据不完整、多处信息矛盾、单位不明确需确认
- **无法判断**：**穷尽取证后**（见 `references/judgment-principles.md` §原则七）该条标准对应的证据文档/记录在**全部** OCR 材料中确实缺失，或仅有不足以判定的间接线索。判此结论必须给出**具体**原因（已查范围 + 缺失的具体信息 + 可解除条件），**禁止**"未提及/无对应内容/OCR未给出"等空泛表述。

## 判定输出格式

结构契约（顶层字段 / 判定条目字段 / `evidence` 形态 / `screenshot_ref` 取值 /
`exclusion_triggered` 配对 / `或组` 汇总口径 / `criteria_rollup` 主条件组级汇总 /
与各机械闸的对应关系）见
**`references/judgment-schema.md`**；可直接对照抄写的完整样例见
**`references/schema_example.json`**。

落盘两步：初稿 `workspace/patients/{id}/judgments_draft_{id}_{TRACK}.json`（按轨），
终稿 `outputs/judgments_{patient_id}.json`（两轨合并后）。

只需记住三条最易错的（细则与故障档案在上述 references 里）：

- ⛔ **`evidence` 必须是对象数组** `[{source,page,screenshot_ref,quote}]`，无证据写 `[]`。
  写成对象不会报错，只会让报告证据栏静默变「—」（闸12 阻断；故障档案：`references/failure-archive.md`#evidence-写成对象）。
- ⛔ **顶层 `judgments` 键集合恒等于本轨标准包条件ID**（闸2），且
  `evidence[].source` 只能逐字取主代理给定的物料来源名清单
  （`phase2_summary.ocr_results[].source`；闸9 白名单把守，故障档案 `references/failure-archive.md`#documents-键自创）。
- ⛔ **【仅 EX】`exclusion_triggered` 必须与 conclusion 配对**：`false ⇔ 符合`、`true ⇔ 不符合`；
  存疑/无法判断省略（闸4 把守）。


## 物料间一致性标注

统一证据源判定下，该检查在判定时完成：矛盾记入 reason 与 warnings。

| 检查项 | 严重程度 | 处理 |
|--------|---------|------|
| 姓名不一致 | 🔴 高 | "疑非同一患者"警告 |
| 性别不一致 | 🔴 高 | "疑非同一患者"警告 |
| 年龄差异 > 2岁 | 🟡 中 | 标注差异 |
| ECOG评分不一致 | 🟡 中 | 建议以最新评估为准 |
| 诊断信息矛盾 | 🟡 中 | 标注矛盾，建议确认 |
| 化验值不同时间点差异 | ⚪ 低 | 正常现象，仅记录 |

## 质控（QC）核验

按轨核验（`患者 × 轨`），委派模板、核验清单 8 项、QC 报告结构与 `passed` 计算规则见
**`references/qc-delegation.md`**。要点：

- **只传路径不粘正文**：粘贴要求主代理先读入全量判定，直接引爆主代理上下文。
- ⛔ **前置结构闸**：QC 子代理**开工第一步**要读
  `patients/{id}/judgment_structure_gate_{id}_{SHARD}.json`，`exit_code != 0` 或文件缺失即
  **自行拒工返回**（QC 还需比对 `content_sha256_16` 确认判定文件未在闸后被改动）。
  这是对「结构闸未过不得派 QC」的硬化——不依赖主代理守规矩。
- **收敛机制**：bash 收敛客观结构 + `task` 语义核验 + 阻断/建议分级 + **最多 3 轮**
  （首检 1 轮 + 改判后复检 2 轮 = **2 次改判机会**）；
  达上限且仅剩建议级时带建议放行、不阻塞交付。
- **轮次范围收窄（第 2、3 轮只查收敛，不扩大范围）**：第 1 轮是**全量**语义核验（核验清单
  8 项全跑，覆盖本轨所有条目）。第 2、3 轮（改判后复检）**只核对上一轮 `blocking_issues`
  是否已按 `action` 改判清空 + 两个机械闸产物是否重跑至空**，不得对第 1 轮未点名的条目重新
  发起全量语义扫描、不得报出第 1 轮未出现过的新阻断项。已改判条目之外没有变化的条目**不重审**。
  例外：若改判本身引入了新的结构/方向错误（如改坏了 `exclusion_triggered` 三字段联动），
  该新错误算作"上一轮改判的副作用"，仍在收敛核验范围内，不算"扩大范围"。
  这条防止 QC 在轮次接近上限时又从全量扫描角度挑出新问题，导致轮次耗尽却仍不收敛。
- **三个机械闸一律阻断，不适用「带建议放行」**：本轨 `uncertain_recheck_*.json.suspected_missed`
  非空（漏判）、`reason_alignment_*.json.conflicts` 非空（reason 与条件ID 错位/编造数值/复制粘贴）、
  【仅 EX 轨】`exclusion_direction_check_*.json.conflicts` 非空（方向反转）
  → `passed=false`，必须改判至清空（改判方式见「QC 后改判」与 `references/judgment-repair.md`）。
  二者都是可机械证实的客观错误，不是主观偏好。
- **`passed` 由机械闸决定，禁止人工覆盖**：
  `passed = blocking_issues 为空 AND suspected_missed 为空 AND（EX 轨）conflicts 为空`。
- **QC 只核对机械闸产物，不重跑不改写**；禁止用脚本做语义修订；禁止 `task` / `present_files`。
- 产出 `outputs/qc_report_{id}_{SHARD}.json`，result 只回传路径 + `passed` + 阻断条件ID 列表。

### 派 QC 之前：装配取证素材包（⛔ 硬前置）

```bash
python3 /mnt/skills/custom/eligibility-judgment/scripts/evidence_bundle.py \
  --criteria  /mnt/user-data/workspace/criteria_judge_{SHARD}.json \
  --judgments /mnt/user-data/workspace/patients/{id}/judgments_draft_{id}_{SHARD}.json \
  --ocr       /mnt/user-data/workspace/patients/{id}/ocr/{source}/ocr_records.md \
  --out       /mnt/user-data/workspace/patients/{id}/evidence_bundle_{id}_{SHARD}.md \
  --patient {id} --track {SHARD}
```

多份 OCR 时 `--ocr` 可重复传入。它把 QC 取证要的素材一次装配好：条件 + 锚点 + 当前判定 + reason
+ 每条 evidence 引文的**逐字核验**（✅`source:行号` / ❌未找到）+ 跨条目去重后的 OCR 命中窗口。
⚠️ 只摆素材、不下结论；exit 2 仅表示输入不可读（有找不到的引文仍是 exit 0，那是 QC 的判断对象）。

⛔ **不装配就不许派 QC**：QC 逐条 `grep`+`read_file` 取证会把核验拆成几十步，而每步都要重传全部
历史。判据与代价见 `references/failure-archive.md`「QC 逐条取证耗尽步数额度」。

## QC 后改判（主代理亲做 或 委派改判子代理）

改判是本技能中**直接改写已落盘判定结论**的唯一步骤，也是最容易出静默事故的一步。
完整处置手册（触发来源、改写硬规则、机械闸、委派模板、故障档案）见
**`references/judgment-repair.md`**（唯一权威）。

**并行性**：改判任务矩阵 = **患者 × 轨**，全并行——不同患者不同目录、同患者两轨不同文件，
可与其他 `{患者, 轨}` 的 QC 同时在途。⛔ 唯一串行约束：同一 `{患者, 轨}` 的
改判 → 机械闸重跑 → 结构闸 → 下一轮 QC 必须顺序进行。每个 `{患者, 轨}` 的 `task` 槽位在
QC / 改判之间**串行复用**，委派改判不会把并发推到 3 以上。

**执行者二选一**：主代理亲做，或委派改判子代理（患者数多、阻断项多时推荐）。委派须同时满足 4 条：

- ⛔ **`subagent_type` 必须有 `str_replace`**：用 `general-purpose` 或 `data-extractor`。
  **不得用 `quality-control`**——它的工具白名单里没有 `str_replace`，只能 `write_file`。
- ⛔ **委派模板必须逐条复述改写硬规则**（子代理不会自动读 `references/judgment-repair.md`）。
- ⛔ **改判子代理禁止写 `qc_report_{id}_{SHARD}.json`**：改的人不能同时宣布改好了。
- ⛔ **同组合串行**：该 `{患者, 轨}` 改判在途时，主代理与任何脚本都不得碰该文件。

**不可违反的三条**：

- ⛔ **改判 `judgments_draft_{id}_{SHARD}.json` 只允许 `apply_json_patches` 的对象级形态
  （`{"pointer","op","value"}`），`write_file` 与 `str_replace` 一律禁止**
  ——全量、分片、`append=True` 都不行，**对主代理与改判子代理同等适用**。
  `write_file` 会让 QC 没点名的条目被顺手改掉或消失；字符串定位则保证不了跨字段一致性
  ——「改了 reason 漏改 conclusion」就是这么反复被门禁抓出来的
  （`references/failure-archive.md`#改判用字符串替换漏改字段）。
  一条 `blocking_issues` 的所有字段（`conclusion`/`reason`/`evidence`【EX 加
  `exclusion_triggered`】+ `summary` 计数）**必须一次调用内**改完；`op` 只用
  `replace`/`add`/`remove`/`get`。⛔ `remove` **只允许**删 QC 点名的条目。
  复核单条用 `{"op":"get"}`，**不要为看一条重读整份文件**。`.md`/纯文本仍用 `str_replace`。
  pointer 写法与调用示例见 `references/judgment-repair.md`。
  （与「首次一次 `write_file` 落盘」是两条独立规则：那条针对首次，本条针对改已有。）

- ⛔ **`evidence[].source` 必须逐字取主代理给定的物料来源名清单**
  （`phase2_summary.json.ocr_results[].source`），禁止子代理自创——统一证据源判定后物料维度
  在产物里只剩这一处，自创来源会被报告渲染成假物料（故障档案：`references/failure-archive.md`#documents-键自创）。
  把守：落盘与改判后 `check_judgment_structure.py` 闸 9（白名单，`exit 2`）。
- ⛔ **【EX 轨】排除项改判必须三字段一起改**：`conclusion` + `exclusion_triggered` + reason 措辞
  （原则九 B 冗余互校）。只改一个会让方向校验闸继续报冲突。同一批里一并更新 `summary` 计数。
- ⛔ **改判前必须 `--snapshot` 留基线，改判后必须重跑两个机械闸再跑结构闸**：
  ```bash
  python3 /mnt/skills/custom/eligibility-judgment/scripts/check_judgment_structure.py \
      --workspace /mnt/user-data/workspace --patient {id} --track {SHARD} --snapshot   # 改判前
  # …改判…，然后重跑 uncertain_recheck.py + check_reason_alignment.py（+ EX 轨 exclusion_direction_check.py）至清空，再：
  python3 .../check_judgment_structure.py --workspace /mnt/user-data/workspace \
      --patient {id} --track {SHARD} --qc /mnt/user-data/outputs/qc_report_{id}_{SHARD}.json
  ```
  9 个闸：顶层结构 / **条件ID 覆盖恒等于标准包 `条件数`** / 结论枚举 / 【EX】方向字段一致 /
  summary 自洽 / **机械闸产物已清空** / **QC 目标条目存在** / **改判守恒**（无基线则闸 8 跳过）/
  **evidence source 属于真实 OCR 来源集合**（闸 9 白名单）。
  每次运行落盘 `judgment_structure_gate_{id}_{TRACK}.json`（`exit_code` + 内容哈希），
  供 QC 子代理开工前自检前置、未过闸自行拒工。
  ⛔ **`exit 2` 的唯一处置是回派该 `{患者, 轨}` 重判，禁止写脚本把畸形产物转码成合规形态**
  （转码是猜字段名、脚本不幂等重跑即清零数据、还会烧轮次触发循环保护——三样都实际发生过；
  完整理由见 `references/judgment-repair.md`「结构闸不过时的唯一处置」，
  故障档案：`references/failure-archive.md`#把畸形产物转码）。
  委派重判时必须把判定委派模板**原样**带上（含四条闸命令与 `judgment-schema.md` 指引），
  ⛔ 不得转述/精简——转述漏掉结构闸命令就会产出自创 schema 的判定文件。
  闸 8 专抓两类静默事故：**无操作改判**（QC 点名却三字段全未动）与**连带误伤**
  （QC 未点名却被改了结论）。⛔ `exit 2` 时禁止派下一轮 QC、禁止进入合并汇总；
  结构闸**不得与 `task(quality-control)` 同轮发出**。
  ⛔ **禁止现写内联 bash/python 顶替这些检查**——口径每次现写就会漂移。

## 推断理由（在判定阶段一次写定，**无独立理由阶段**）

`reason` 字段由**判定子代理**在落盘 `judgments_draft_{id}_{SHARD}.json` 时一次写定，
要求见 `references/judge-delegation.md`（「无法判断」三要素；【仅 EX 轨】显式写
「触发/未触发（该）排除条件」+ `exclusion_triggered` 布尔字段）。QC 核验 reason 与
条件语义、证据是否匹配；改判时 reason 与 conclusion 一并 `str_replace` 修正。

⛔ **禁止新增"统一重写理由"的独立阶段/子代理**（原 `task(report-writer)` + `reasons_{id}_{SHARD}.json`
+ `merge-reasons` 回填链路已整体移除）。理由：该阶段的子代理只拿到 `judgments_draft` 而拿不到
本轨标准包与 OCR，无法知道每个条件ID 对应什么条件，于是按"脑内通用标准顺序"位置映射、
凭先验补数值，用一批新理由整体覆盖判定阶段已核对过的 `reason`，且覆盖发生在 QC 通过之后、
无任何闸复核。实测损坏（16/24 条错位 + 编造化验值 + 逐字重复的 reason）见
故障档案：`references/failure-archive.md`#张冠李戴与编造数值。

若要提升理由质量，**在判定阶段或 QC 阶段做**，不要新开覆盖已落盘 reason 的阶段。
