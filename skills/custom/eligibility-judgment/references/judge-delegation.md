# 逐条判定委派模板（按轨）

> ⛔ **本模板必须逐字到达子代理，禁止转述/精简/摘要**。
> **落实方式 = `scripts/render_judge_prompt.py` 机械渲染 + `task(prompt_file=...)`**
> （见下方「渲染 prompt」）：模板原文不经任何模型之手，占位符按白名单精确替换，
> 比手抄更忠实、也不必为固定模板重复付输出 token。
> ⛔ **不要**把本模板正文抄进 `task` 的 `prompt` 参数 —— 那是旧做法，代价见下方 `247a535f`。
> 只允许替换 `{}` 占位符（`{id}` / `{SHARD}` / `{分片名}` / `{EVIDENCE_SOURCES}` / `{JUDGMENT_DATE}` 与 OCR 路径），
> 其余花括号（`{"op": "get"}` / `{conclusion,reason,evidence}` / `{符合:N, …}` 等）是规则正文，
> 渲染脚本按白名单精确替换，不会碰它们。
> **判定规则在哪**：模板里所有 `§原则N` / `§判定约束 N` 指的都是
> `/mnt/skills/custom/eligibility-judgment/references/judgment-principles.md`。
> ⛔ 该路径必须留在 prompt 里：判定子代理的 `skills` 白名单是 `[]`，它**不会自动加载任何
> SKILL.md**，只能按这个路径 `read_file`。删掉它等于让子代理凭通用知识判定。
> ⛔ 也**不要**把规则正文抄进模板——抄写即漂移，规则只有一处权威。
>
> 尤其**不得删掉任何一条闸命令**：四条闸（`uncertain_recheck` / `check_reason_alignment` /
> 【EX】`exclusion_direction_check` / `check_judgment_structure`）与「产物结构以
> `references/judgment-schema.md` 为准」这两块是产物能否被下游消费的全部保证。
>
> 真实故障 会话 `9a83ccc9`：主代理把本模板压缩成 1.8k 字符的自述版，写着「落盘后必须跑三个机械闸」
> 却只给了**两条**命令——`check_judgment_structure.py` 整条消失，也没提 `judgment-schema.md`。
> 子代理于是自创 schema（顶层 `judgments` 写成**列表**，外加 `criteria_track` / `total_criteria` /
> `evidence_index` / `statistics`，还把或组本身当条件写成 `IN-3-OR`），跑完剩下两条闸后回报
> 「闸全绿」——因为那两条闸读不到条目时会真空通过（现已修，见各自 docstring）。
> 结果：判定语义正确但产物无法进入合并，主代理花 24 次 bash 试图转码修复，
> 撞上循环保护被强制收尾，最终仍靠**回派重判**才解决。

> 任务矩阵 = **患者 × 轨 × 批次**，共 `Σ(每轨批数) × 患者数` 个子任务，彼此完全独立。
> 入选组与排除组判定互不依赖（判定按条逐条独立），双轨拆分后并行度 ×2、每个子代理输入减半；
> **轨内再按 12 条一批细分**（下方「批次拆分」），把单任务的条目数再降到约 1/3。
>
> `{SHARD}` = `IN` 或 `EX`；`{分片名}` = 入选 或 排除；`{id}` = 患者ID；
> `{BATCH}` = 批号（1 起）；`{BATCH_IDS}` = 本批条件ID（空格分隔）；`{BATCH_COUNT}` = 本轨总批数。
>
> **`{EVIDENCE_SOURCES}` 由主代理填**（物料来源名清单，逐字取 `phase2_summary.ocr_results[].source`）
> `phase2_summary.json.ocr_results[].source` 的全部值，逐字照抄，一行一个：
> ```
> - "筛选期病历"   ← 对应 patients/{id}/ocr/筛选期病历/ocr_records.md
> - "筛选期检查"   ← 对应 patients/{id}/ocr/筛选期检查/ocr_records.md
> ```
> ⛔ 不得让子代理自己命名物料来源——evidence[].source 白名单由结构闸闸 9 机械核验。
> 编排层按患者模式填入实际 OCR 路径，**不让子代理自行 glob 搜索**。
>
> **`{JUDGMENT_DATE}` 由主代理填**：委派前取一次真实当天（`bash date -I` → `YYYY-MM-DD`），
> **同一批判定的所有患者 × 两轨共用同一个值**（保证同批可复现、可比对）。它是时间窗条件
> 「参考日期取不到时」的唯一合法兜底来源。
> ⛔ 子代理**不得**自行取日期、不得凭记忆写"今天"——模型对当前日期的猜测可能偏数月，
> 会让所有时间窗静默偏移，事后还无法区分"算错"与"基准不同"。

## 批次拆分（⛔ 派判定前必做）

派本轨判定**之前**先规划批次，每批 12 条：

```bash
python3 /mnt/skills/custom/eligibility-judgment/scripts/judge_pack.py plan-batches \
  --criteria /mnt/user-data/workspace/criteria_judge_{SHARD}.json \
  --track {SHARD} --patient {id} --batch-size 12 \
  --out /mnt/user-data/workspace/patients/{id}/judge_batches_{id}_{SHARD}.json
```

产物给出每批的 `condition_ids` 与 `draft_file`（`judgments_draft_{id}_{SHARD}_b{N}.json`）。

## 渲染 prompt（⛔ 派判定前必做，紧接批次拆分）

⛔ **不要把下方模板正文抄进 `task` 的 prompt** —— 用脚本渲染，然后用 `prompt_file` 传路径：

```bash
python3 /mnt/skills/custom/eligibility-judgment/scripts/render_judge_prompt.py \
  --batches /mnt/user-data/workspace/patients/{id}/judge_batches_{id}_{SHARD}.json \
  --patient {id} --track {SHARD} \
  --judgment-date $(date -I) \
  --doc-key "{doc1}=/mnt/user-data/workspace/patients/{id}/ocr/{doc1}/ocr_records.md" \
  --doc-key "{doc2}=/mnt/user-data/workspace/patients/{id}/ocr/{doc2}/ocr_records.md" \
  --page-index /mnt/user-data/workspace/patients/{id}/ocr_page_index.json \
  --out-dir /mnt/user-data/workspace/patients/{id}/prompts
```

`--doc-key "来源名=OCR路径"` 的来源名逐字取 `phase2_summary.json.ocr_results[].source`；
统一证据源判定下两轨给同一套来源名（顺序也相同），evidence[].source 只能取自该清单。
整份解析模式把路径换成 `ocr/{source}/{source}_full.md` 并省略 `--page-index`。
脚本 stdout 直接给出每批的 `prompt_file` 与 `expected_outputs`，照抄派发即可：

```
task(description="判定{分片名}轨b1批", subagent_type="general-purpose",
     prompt_file="/mnt/user-data/workspace/patients/{id}/prompts/judge_prompt_{id}_{SHARD}_b1.md",
     expected_outputs=["/mnt/user-data/workspace/patients/{id}/judgments_draft_{id}_{SHARD}_b1.json"])
```

**为什么用脚本而不是手抄**：模板必须逐字到达子代理（理由见本文顶部 `9a83ccc9`：转述会丢闸命令），
而机械渲染比手抄**更**忠实——模板原文没有任何模型经手，占位符按白名单精确替换，
渲染后还会自检「四条闸命令 + `judgment-schema.md` 指针」是否都在、白名单占位符是否还有残留，
任一不过 `exit 2` 且**不产出任何文件**。
手抄的代价是按 token 计费的：会话 `247a535f` 的三路判定派发是全会话最慢的一次 lead 调用 ——
**143.6 秒 / 15,265 输出 token**，只为吐出三份各约 7.5k 字符的固定模板。
⚠️ 渲染产出的 prompt 文件是**派发用的中间产物**，不是判定输入，⛔ 不要让子代理去读它
（它自己的 prompt 就是这份内容），也不要把 `prompts/` 目录写进 `expected_outputs`。

**为什么必须分批**（会话 `09eeaffb`）：IN 轨 28 条 / EX 轨 45 条整轨一次派，两个子代理各跑
99 个 AI 回合、**0 次 `write_file`**，双双撞满 `recursion_limit=420`，合计 10.02M token /
42 分钟、产物为零。`recursion_limit` 分支不打捞部分产物 → 整单作废。分批把「一个做不完的任务」
换成「几个做得完的任务」，且**每批各自落盘**：某批撞限只损失那一批，主代理据批次清单补派缺的那批，
不必整轨重判。

**三条硬规则**：

1. ⛔ **批次只在轨内切，不跨轨**。IN/EX 是判定的语义边界（排除项方向反直觉、EX 轨独有
   `exclusion_triggered` 与方向校验闸，两轨模板与闸命令都不同）。
2. ⛔ **不按 `四分类` 类目切**。`plan-batches` 按条件ID 自然序**跨类目连续切分**——类目边界
   （可从病例获取 / 不可从病例获取）不是工作量边界，按它切会让 IN 轨「不可从病例获取」那 4 条
   单独成批，而它们同样要全量核查病历（见模板里「『不可从病例获取』条目同样必须核查病历」），
   等于多派一次任务、多付一份 OCR 读取。
3. ⛔ **不切标准包**。每批的子代理仍读整份 `criteria_judge_{SHARD}.json`，批次只是「本批判哪些
   条件ID」的清单。切包会同时废掉三样东西：闸 2 的整轨恒等校验（漏一整批不会有任何闸报错）、
   `merge-judgments --criteria` 的或组重算（`或组` 分支跨批时找不齐成员 → `RollupBlocked`
   或静默退化成 AND），以及"标准只有一处权威"这条纪律。

**批级产物一律带 `_b{N}` 后缀**（draft、`uncertain_recheck_*`、`reason_alignment_*`、
【EX】`exclusion_direction_check_*`、结构闸产物、闸 8 基线）：批级闸产物只覆盖本批条目，
用整轨产物核批级 draft 会把别批的漏判算到本批头上。

**批级结构闸**加 `--batch {BATCH}`——闸 2 的期望集合换成本批清单，**仍是恒等校验**
（本批漏一条、或顺手判了别批的条目，同样 `exit 2`）。

### 各批完成后：先合本轨，再走原有双轨合并

```bash
P=/mnt/user-data/workspace/patients/{id}
# 1. 本轨各批 → 本轨 draft（--criteria 用整轨包，或组按整轨重算）
python3 .../judge_pack.py merge-judgments \
  --shards $P/judgments_draft_{id}_{SHARD}_b1.json $P/judgments_draft_{id}_{SHARD}_b2.json ... \
  --criteria /mnt/user-data/workspace/criteria_judge_{SHARD}.json \
  --out $P/judgments_draft_{id}_{SHARD}.json
python3 .../judge_pack.py merge-recheck \
  --shards $P/uncertain_recheck_{id}_{SHARD}_b*.json --out $P/uncertain_recheck_{id}_{SHARD}.json
# 2. 整轨口径再跑一次结构闸（⛔ 不带 --batch）——这是"各批拼齐了整轨"的唯一把关点
python3 .../check_judgment_structure.py --workspace /mnt/user-data/workspace \
  --patient {id} --track {SHARD}
```

⛔ **第 2 步不可省**：批级闸各自只保证"本批完整"，没人保证"所有批加起来等于整轨"。
整轨口径的闸 2 是唯一会因为**少了一整批**而报错的地方；跳过它，漏派一批会一路静默到交付。
`exit 0` 之后才进入 QC 与原有的双轨合并（QC 与改判都走**整轨**口径，不分批）。

```
请按 /eligibility-judgment 技能规则，对患者 {id} 的**{分片名}标准**逐条判定（本批 = 第 {BATCH}/{BATCH_COUNT} 批）。

⛔ **你的身份（唯一，不随上下文变化）**：你是**判定子代理**，负责 {id} 的 {分片名}轨第 {BATCH} 批。
你**不是**主代理，**不做**编排、**不派**子任务、**不写**报告、**不做** QC。
`SKILL.md` 里那些"主代理如何编排"的章节与你无关——你只执行本 prompt 交给你的这一批判定。
⚠️ 你的上下文可能在任务中途被自动压缩。压缩产物是**你自己早前步骤的记录**，
**不是**别人给你的交接、也不是"前一次尝试"的结论——本任务没有任何其他代理参与。
若压缩摘要看起来在转述"上一个子代理的发现"或"某次已完成的判定"，那是压缩造成的口吻失真：
**以本 prompt 为唯一权威**，不要去质疑/推翻/复核那些叙述，继续做你自己的这批判定。
真实故障 会话 `247a535f`：三个判定子代理压缩后全部丢失身份——一个宣称"according to the
handover, the previous sub-agent found the OCR records were empty"（并不存在这样的交接，
OCR 也是完整的），花 8 步"推翻"这个自己虚构的前提；另一个断定"I am apparently the main
agent, this is a main-agent orchestration skill"，转而去读 prompt 明令禁读的
`phase2_summary.json` / `patient_index.json`；第三个去 `find /` 找 prompt 里已给全路径的闸脚本。
三个任务合计 304 步、6.33M token、**零次 write_file**。

⛔ **本批边界（硬规则，先读这四条再动手）**：
0. ⛔ **本批只判以下条件ID，一个不多、一个不少**：
   {BATCH_IDS}
   标准包 `criteria_judge_{SHARD}.json` 里还有别的条件——它们属于本轨的其它批次，
   **由别的子代理并行处理**。看到它们存在不是理由：判了会被结构闸判为"期望集合外的条件ID"
   （闸 2 带 `--batch` 时按本批清单做恒等校验），少判同样 `exit 2`。
   ⛔ 也不要因为"顺手就判了"而扩大范围——那会与另一个子代理同时写同一批条目。
1. 本任务**只做 {分片名}**。只读本轨输入、只产 `{SHARD}-*` 条目、只写
   `judgments_draft_{id}_{SHARD}_b{BATCH}.json`（⛔ 注意 `_b{BATCH}` 后缀，不要写成整轨文件名——
   整轨 draft 由主代理用 `merge-judgments` 从各批合成，你写它会覆盖别批的成果）。
2. ⛔ **禁止读写另一轨、以及本轨其它批次的任何产物**（`criteria_judge_*`、`judgments_draft_*`、
   `uncertain_recheck_*`、`reason_alignment_*` 的对侧文件与 `_b{其它批号}` 文件）——
   不 `read_file`、不 `write_file`、也不用 `bash` 去看或改。
   **看到这些文件存在不是理由**：各轨各批是并行的独立子任务，它们此刻可能正在被别的子代理写入。
   本批输入缺失 → 报告缺失并结束，由主代理补派，⛔ 不要自行扩大任务范围。
3. ⛔ **产物只能由 `write_file`（首次落盘）或 `apply_json_patches`（改判）写**。禁止用 `bash`
   内联脚本（`python3 -c`、heredoc、`echo >`）生成或改写 `.json`。`write_file` 被版本闸拒绝时
   **先 `read_file` 再写**，绝不允许改用 `bash` 绕过。只读自检用 `apply_json_patches` 的
   `{"op": "get"}`，不要现写 python。
   完整判据见判定规则 §本轨边界与写入方式。

判定当天：{JUDGMENT_DATE}
- 仅用于时间窗条件在**参考日期取不到**时兜底（先按 `日期维度.参考事件` 去病历里找该基准日期，命中即用并把日期原文写进 evidence）
- 落盘时必须把它逐字写进判定产物顶层 `judgment_date`
- ⛔ 不要自己 bash 取日期、不要凭记忆写今天；⛔ 兜底假设不得用来下负面结论（详见技能「日期/时间窗判定」D）

输入（只允许读这三类文件，路径已给全，禁止 ls/glob/find 探索）：
- /mnt/user-data/workspace/criteria_judge_{SHARD}.json（{分片名}标准判定输入包，含 四分类 与判定必需字段。
  ⚠️ 它是**整轨**包，你只判上面第 0 条列出的那些条件ID）
- 该患者的 OCR 原文（由主代理按模式填入实际路径，逐个列出）：
    · 整份解析（单患者 + 整份 OCR）：/mnt/user-data/workspace/ocr/{source}/{source}_full.md（**无分页页块** → evidence 不填 page/screenshot_ref）
    · 分页聚合（单患者逐页 / 多患者混合逐页）：/mnt/user-data/workspace/patients/{id}/ocr/{source}/ocr_records.md（有「来源图片」页块 → evidence **必须**带 page + screenshot_ref）
- 【仅分页聚合模式】/mnt/user-data/workspace/patients/{id}/ocr_page_index.json
  （**页码 → 行区间索引**，聚合阶段机械产出：每个 source 的每一页给出 `page` / `image` /
  `start_line` / `end_line`，1-based 闭区间，可直接用作 read_file 的行区间参数）

上下文纪律（硬约束，直接影响时延）：
- **先读页码索引，再决定读哪些行**：`ocr_page_index.json` 很小，**第一轮就和标准包一起并行读掉**。
  它已经给出每页的行区间，⛔ **不要**再用 grep 去摸页边界、⛔ **不要**为了"看看有什么"整份读
  几千行的 ocr_records.md。取证时按索引定位到目标页的 `start_line`-`end_line` 做区间读。
  （真实代价：会话 `09eeaffb` 的子代理不知道有这张表，把一份 7,604 行的 OCR read_file 了 34 次、
  请求区间 69% 重复，到第 143 个回合才靠 grep 自己拼出这张表——而它在聚合时就已经存在。）
- 每份输入文件本任务内**最多 read_file 一次**（同一份 ocr_records.md 的**不同页区间**不算重复读，
  但同一区间不要读第二遍）；多份 ocr_records.md **同轮并行**读取
- ⛔ **规则文件（`judgment-principles.md` / `judgment-schema.md` / `schema_example.json` /
  `judge-delegation.md` / `SKILL.md`）每份只读一次，读完就开始判。**
  它们是**规则**不是**待办**：读第二遍不会让你更有把握，只会把你已经取到的病历证据挤出上下文。
  如果你发现自己"想再确认一下 schema"——那说明该落盘了，落盘后由闸来告诉你结构对不对，
  这正是四条机械闸存在的意义。
  真实代价（会话 `247a535f`，三个判定子代理同时发生）：`judgment-schema.md` 被完整重读 7 次、
  `ocr_page_index.json` 8 次、`criteria_judge_IN.json` 7 次、同一份 7,604 行 OCR 读 16 次；
  三个任务合计 304 步、6.33M token、**零次 write_file**，全部被产物闸判定为无产出。
- ⛔ **判定不是"读完所有材料再开始"，而是"边取证边落盘"。**
  拿到标准包 + 页码索引 + 两份 OCR 的相关页区间后就**先写出初稿**（哪怕部分条目还是
  「无法判断」），再按闸的反馈补。⛔ 绝不允许在没有任何 write_file 的情况下走过 30 步以上——
  真到那一步，说明你已经在原地打转，应当**立即落盘当前结论**并在 result 里报告缺口。
- **禁止**读全量 criteria_parsed.json、对侧轨的判定输入包、本轨其它批次的产物（`_b{其它批号}`）、
  phase*_summary.json、patient_index.json、criteria_qc_*.json、其他患者/其它 source 的 OCR、
  uploads/（整份解析模式下 `patients/{id}/ocr/` 与 `ocr_page_index.json` 不存在，不要去找）
- 本批判定结果**一次 write_file 落盘**，不分多次追加改写
- 禁止使用 task / present_files

要求：
- 按4级判定体系（符合/不符合/存疑/无法判断）逐条判定，引用条件ID + 原文证据（来源+页码）
- **先建"取证索引"再判**：按取证规则（判定规则 §原则四，见下方「判定规则在哪」）把每条"可从病例获取"条件映射到其证据文档/记录类型（检验报告单、影像/病理报告、专项评估表、病程/入院记录、既往史/用药记录等），按概念/等价表述取证；若条件的转化条件带「证据位置/同义词」字段则优先按其取证。
- **禁止伪"无法判断"（判定规则 §原则五）**：判"无法判断"前必须先按取证索引检索；对应证据文档/记录存在于 OCR 却判无法判断=漏判。
- **结构化表单图片兜底（判定规则 §原则六）**：结构化表单/量表/含手写页 OCR 文本不足或疑似错抄时，view_image 该页原图（路径取自 ocr_records.md 的「来源图片」；若该来源是整份解析产物无页块标注，则从 `/mnt/user-data/workspace/images/{source}/` 下按页序取图）复核后再判，每轮 ≤ 2-3 张。
- **药物/治疗归类用 `web_search` 查证，但归类 ≠ 触发（判定规则 §原则十一）**：条件里出现"全身性药物治疗 / 新型内分泌治疗 / 紫杉类 / 免疫抑制剂 / 强效 CYP3A4 抑制剂 / 抗肿瘤中成药 /
  减毒活疫苗"等类别时，若某具体药物的归属读不出病历（国产新药与氘代类似物尤其易错，如**氘恩扎如胺**），用 `web_search` 查证并把**结论 + 来源**写进该条 evidence。
  ⛔ **隐私红线**：查询串只放药物通用名与类别术语，**严禁**含患者ID/姓名/年龄/诊断/检验值/病历片段。
  ⛔ **归类只回答"这药属于哪一类"，不回答"本条是否被触发"**：对"有 X 病史 **且** 仍需 Y 治疗"
  这类条件，按**判定规则 §原则十一 B 的三步判据**执行——①病历有该病史（只满足前半句）
  ②有**针对该病史**的治疗记录，再对该药做归类（全身性→`不符合`；局部/外用/吸入→`符合`）
  ③查不到针对该病史的治疗记录→判 `存疑`，写明缺该病史的治疗方案记录。
  两条硬约束：患者**恰好在用**的全身性药物（尤其抗肿瘤药）不能用来满足②——否则肿瘤试验
  候选者人人在用全身抗肿瘤治疗，这条排除标准会排掉 100% 的人；研究者写的
  "筛选失败/不适合入组"是**结论不是治疗记录**，也落不到②。
  ⛔ 本模板**不给**任何具体条件的结论，正例反例与真实案例一律看 §原则十一 B，
  别在这里找答案（历史上这里塞过一句自带结论的断言，与 SKILL 相反）。
  同一药物的归类查一次即可，写入 `workspace/drug_class_notes.json` 供跨患者复用；
  ⛔ 复用的是**归类**，不是"是否触发"——后者每个条件都要重判②。
- **`或组` 条目按组汇总，不逐条按 AND 算**：标准包里带 `或组`/`或组语义` 的条目是同一条原文
  标准的 OR 异质替代分支（如 EX-1 的"活性成分过敏/辅料过敏/病史且仍需全身治疗/其它严重过敏反应"）。
  每支**各自独立取证判定**并各自落盘，不要合并成一条；汇总时按 `或组语义`：
  排除轨 `任一触发即整条触发`（组内任一 `不符合` → 整组触发）；
  入选轨 `任一满足即整条满足`（组内任一 `符合` → 整组满足，其余分支的「无法判断」**不构成障碍**）。
  ⛔ 入选 `或组` 绝不能按"全部符合"汇总——IN-5（PSA 进展/软组织进展/骨病灶进展）患者只满足一支时，
  逐条 AND 会把整体判成不符合入选，等于错误淘汰。
  若某条带 `或组` 却找不到同组兄弟，按阻断级回报，不要自行猜汇总方向。
  组级汇总由 `merge-judgments --criteria` **机械重算**（`或组` 从标准包取，不依赖你转抄），
  所以你只需**逐支独立判定**；但仍请把 `或组`/`或组语义` 原样带进条目供交叉核对。
- ⛔ **产物结构以 `references/judgment-schema.md` 为准**（形态样例见 `references/schema_example.json`）。
  最易错的一条：**`evidence` 必须是对象数组** `[{source, page, screenshot_ref, quote}]`，
  每一条都是，无证据写 `[]`；写成对象（`{"年龄": {...}}`）不会报错，只会让报告证据栏静默变「—」
  （故障 thread `dfbb4554`：IN 轨 26 条全错、EX 轨正确，当时结构闸 exit_code=0）。现由闸12 阻断。
- ⛔ **结构只有这一个来源，workspace 里的任何 JSON 都不是结构参考**：
  · **输入包 `criteria_judge_{SHARD}.json` 不是输出模板**。它的 `四分类` / `转化条件` 是**待判条件**
    的形态，判定产物的形态是顶层 `judgments.{条件ID}.{conclusion,reason,evidence}`。
  · **workspace 下已有的 `*judgment*.json` 一律不得拿来照抄**——它可能是上一次失败尝试留下的
    错误产物。看到它存在不是理由；要形态就 `read_file schema_example.json`。
  真实故障 会话 `7512ebd2`：判定子代理开局读对了 schema，上下文压缩后凭记忆落盘，
  照着输入包写出 `{"患者":…,"轨":…,"判定":{"IN-2-1":{"子条件":…,"结论":"符合入选","依据":…,"证据":[…]}}}`
  并存成自创路径 `eligibility_judgment_IN_MCRC-2150006.json`；下一轮重派的子代理又读到这个残留文件、
  拿它当模板抄了一遍。**6 次尝试、9.87M token、零产物**。
  ⛔ 落盘前对着 `schema_example.json` 逐字段核一遍顶层键（`patient_id` / `judgment_date` / `judgments` / `summary`）；
  你写出的顶层键若出现中文（`患者` / `轨` / `判定`），就是抄错了源。
- **从严判断（证据缺失不得转为负面结论）**：四个结论里只有「符合/不符合」是对事实的断言，
  「存疑/无法判断」是对证据状态的断言。**没找到证据 ≠ 事实不成立**。
  入选项找不到支持证据 → 判「无法判断」+ 列明待补材料，**禁止**判 `不符合`（那等于直接淘汰患者）；
  排除项找不到"未触发"的证据 → 判「无法判断」/「存疑」，**禁止**判 `不符合`（那等于建议排除）。
  写 `不符合` 前必须能指出**支持该负面结论的病历原文**；只有间接线索不足定论时判「存疑」。
- ⛔ **「不可从病例获取」条目同样必须核查病历**：`可从病例获取=false` 只是"这类条件通常不在病历里"
  的先验提示，**不免除核查义务**。知情同意找签署时间/知情过程记录/"自愿参加"字样、承诺类找筛选期
  访谈记录、预后类找病程评估、主观评估类找"研究者认为…"。命中即据以判定；确实查不到才判
  「无法判断」并列明待补材料。**禁止**因分类就直接落「无法判断」而不检索；**禁止**未检索却写
  "已查…未见…"。真实故障：S042002 的 IN-1 知情同意被静默判无法判断，reason 写"已查筛选期检查"
  （证据其实在筛选期病历），evidence 编了"未见知情同意相关记录"`hit=false`；病历实际写着
  "知情同意书签署=2026-04-15 16:21…患者经过充分考虑后表示完全理解并自愿参加本研究"，应判**符合**。
- **时间窗条件的参考日期先取证、再兜底**（技能「日期/时间窗判定」）：条件写「**签署知情同意书**前 6 个月内
  接受过锶-89/钐-153/镭-223…」时，基准是 **ICF 签署日**，不是筛选日。先按 `日期维度.参考事件` 去病历里找
  该基准的明确日期（`知情同意书签署=` / `研究医生签署` / `筛选日期` / `首次给药` / 该项检测日期 / `末次用药` /
  `手术日期`），命中即用它并把日期原文写进本条 evidence；取不到才用上面给的**判定当天**兜底。
  ⛔ **兜底假设不得用来下负面结论**：若结论是靠兜底日期才成立的负面结论（EX 触发 / IN 不符合）、
  而把参考日期换成"未知"结论就会翻转 → 判「存疑」并把该日期列为待补材料（同「从严判断」）。
  reason 必须三选一声明来源：「参考日期取自病历〈字段〉=YYYY-MM-DD」/「参考日期缺失，按判定当天
  YYYY-MM-DD 推断」/「结论取决于缺失的参考日期〈事件〉，需补充该日期」。
- 遵循判定技能核心原则：**统一证据源**（同一患者全部 OCR 材料是共享证据，每条条件只判一次）、
  证据可追溯、保守判定；**判「无法判断」前须穷尽取证**（同义词/间接旁证/跨源/图片/换算，判定规则 §原则七 A），
  任一步找到证据即据以判定、只有弱线索则优先判「存疑」；**理由必须具体含「已查范围 + 缺失的具体信息 +
  可解除条件」三要素**（禁止「未提及」等空泛表述），检索到的相关旁证记入 evidence；不得为补证据去 grep uploads/原始 PDF
- write_file 输出**本批**初稿：/mnt/user-data/workspace/patients/{id}/judgments_draft_{id}_{SHARD}_b{BATCH}.json
  （格式参照技能 schema，顶层 `judgments` 结构：
  `{"patient_id","judgment_date","judgments":{条件ID:{conclusion,reason,evidence}},"summary"}`，
  只含**本批**条目；`summary` 按本批条目计数。⛔ 文件名必须带 `_b{BATCH}`——写成整轨文件名
  会覆盖别批的成果，而整轨 draft 由主代理 `merge-judgments` 合成）
- ⛔ **统一证据源判定（本患者全部 OCR 材料为共享证据）**：
  {EVIDENCE_SOURCES}
  1. 上面清单是该患者**全部物料来源名**——每条条件**只判定一次**，不得按物料各判一套。
  2. `evidence[].source` 必须**逐字**取自该清单（证据来自哪份物料就写哪个来源名）；
     多份物料有证据就全部写入 evidence 数组（对象数组，无证据写 `[]`）。
  3. **多物料证据冲突时结论按 `不符合 > 符合 > 存疑 > 无法判断` 折叠**：取优先级最高的结论，
     reason 中说明各物料证据与折叠依据。不允许「符合 + 不符合」「符合 + 存疑」「存疑 + 无法判断」
     等矛盾结论共存。
  4. 物料间一致性矛盾（姓名/性别/年龄>2岁/ECOG 不一致等）→ 记入 reason 并写入 `warnings`。
  5. 落盘后由 `check_judgment_structure.py` 闸 9 机械核验 evidence source 白名单。
- **机械闸一步式 wrapper（硬步骤，落盘后必须执行，⛔ 只跑这一条命令，不要拆开、不要改参数、不要调顺序）**：
  ```bash
  python3 /mnt/skills/custom/eligibility-judgment/scripts/run_judgment_gates.py \
    --workspace /mnt/user-data/workspace \
    --patient {id} --track {SHARD} \
    --judgments /mnt/user-data/workspace/patients/{id}/judgments_draft_{id}_{SHARD}_b{BATCH}.json \
    --ocr {OCR_PATHS} \
    --condition-ids {BATCH_IDS} \
    --batch {BATCH} \
    --fix-summary
  ```
  固定顺序：uncertain_recheck.py → check_reason_alignment.py →【仅 EX】exclusion_direction_check.py
  → fix-summary → check_judgment_structure.py（含 `--batch` 口径与 document 键核验）。
  参数/顺序事故（会话 f9231297：`--ocr` 双文档形态传错 EXIT:2、结构闸跑在反查落盘前）不再可能发生。
  各闸语义与处置（wrapper 输出会带闸名，逐个对照）：
  - `uncertain_recheck`：`suspected_missed` 非空 → 对每个命中条件 read_file 其 grep_hits 命中行±5 行
    上下文，据实改判为 符合/不符合/存疑，把命中原文写入 evidence，重写本批初稿后重跑。
    **只查 OCR，绝不查 uploads。**（`--criteria` 用整轨包，脚本只核你落盘的那些条目。）
  - `check_reason_alignment`：`conflicts` 非空 → 逐条按 `action` 改判后重跑至清空。四类阻断含义：
    `cross_condition_reason`=理由讲的是别的条件（须连 conclusion 一起复核）、`no_anchor_hit`=标准包锚点零命中、
    `unsourced_number`=引用了不在 evidence/本患者 OCR 的数值（编造或跨患者污染）、`duplicate_reason`=多条理由逐字相同。
    ⛔ 禁止把 reason 改成照抄标准原文来骗过匹配——要写本条对应的病历字段与数值。
    历史故障 thread `81562273`：IN 轨 24 条中 16 条错位（IN-6 配"ECOG 1分"、IN-11 配"病毒筛查"），
    并引用该患者 OCR 命中 0 次的 ANC 3.55 / PLT 206 / HGB 133 / 肌酐 80.1，四道既有闸全部放过。
  - 【仅 EX 轨】`exclusion_direction`：`conflicts` 非空 → 逐条按 `expected_conclusion` 与 reason 语义
    确认真实方向，改写 conclusion + `exclusion_triggered` + reason 措辞后重写本轨初稿，再跑直到清空；
    `advisories` 非空 → 补齐 reason 的「触发/未触发」措辞。
  - `check_judgment_structure`：`exit 2` → 自行修到 `exit 0` 再返回（闸 9 会指出 document 键应取什么值）。
  - 排除项语义（判定规则 §原则九）：排除项 `符合`=排除**未触发**（可入选）、`不符合`=排除**被触发**（应排除）。
    reason 必须显式写「未触发（该）排除条件」或「触发（该）排除条件」，并填布尔字段 `exclusion_triggered`
    （false⇔符合、true⇔不符合，存疑/无法判断省略）。**禁止**按"病历与标准描述不一致→填不符合"的思路写。
  ⛔ 产物结构以 `/mnt/skills/custom/eligibility-judgment/references/judgment-schema.md` 为准，
  形态样例见同目录 `schema_example.json`——**不要自己发明字段或层级**：顶层 `judgments`
  必须是「条件ID → 条目」的**嵌套 dict**（不是列表），条目键用 `conclusion`/`reason`/`evidence`。
  或组只在条目内用 `或组`/`或组语义` 标注，⛔ **不得把或组本身写成一个条件条目**（`IN-5-OR`
  不是条件ID，会被闸2 判为标准包外条目）。
- **result 只回传**：本批号 + 本批条件ID 数 + 产出文件绝对路径清单 + 结论计数
  `{符合:N, 不符合:N, 存疑:N, 无法判断:N}`（四类之和必须等于本批条件ID 数）+ 闸门状态：
  **四条闸逐条给出**（`check_judgment_structure` 的 `exit_code`、`uncertain_recheck.suspected_missed`、
  `check_reason_alignment.conflicts` 与 `coverage`、【EX】`exclusion_direction_check.conflicts`）。
  ⛔ 不得只写「闸全绿」——必须点名每条闸及其数字，漏跑哪条一眼可见。
  **禁止**回传判定条目正文、reason、证据原文。
- ⛔ **四条闸全绿 = 终点线，立刻写 result 结束**：不许"再确认一遍"、不许换参数复跑、
  不许为建议级（`advisories` / 非阻断提示）再改一轮。闸说过就是过了；建议级提示原样写进
  result 交主代理，就是正确处置。同一条闸命令在本任务内**跑第 3 次就是在浪费预算**——
  子代理链已挂 `LoopDetectionMiddleware`，恒等重复到阈值会被直接打断（那时任务已经废了）。
```

## 委派时必须带 `expected_outputs`（机械后置校验，`task` 参数）

派本批判定时,`task` 调用**必须**带上**本批**初稿的绝对路径:

```
expected_outputs=["/mnt/user-data/workspace/patients/{id}/judgments_draft_{id}_{SHARD}_b{BATCH}.json"]
```

⛔ 声明的是**批级**路径(带 `_b{BATCH}`)。写整轨路径会让每一批都去探同一个文件:
第一批产出后,后面每批不论有没有真的落盘都能通过这道后置校验。

harness 会在子代理被允许报 `completed` **之前**探一次该文件:不存在或内容为空(`{}` / `[]` / 空白)
→ 该 task 直接判 `failed` 并在回报里点名缺失路径,`task` 随即自动重派一次。

⛔ 这**不替代**四条闸,也不替代下面主代理的验收 —— 闸检查的是「产物对不对」,
`expected_outputs` 检查的是「产物在不在」。两者管的是不同的失败模式:

> 真实故障 会话 `88df83a8`:EX 轨判定子代理在任务内被压缩 4 次后改写了目标 ——
> 去读一个不存在的 `current_qc_report.json`、glob 搜索"已有 QC 报告"、跨轨读 `criteria_qc_IN.json`,
> 最终写出 `qc_review_report.json`(自创文件名)而不是 `judgments_draft_MCRC-2150006_EX.json`,
> 并以 `completed` 回报一份 Markdown「QC 判定报告」。`task` 把它当成功回给主代理,
> 主代理直到 8 分钟后自己跑结构闸才看到「闸1 文件不存在」,重派时撞上 run 结束,整轨判定作废。
> 有 `expected_outputs` 的话,这次失败会在子代理返回的那一刻就暴露,并自动重派。

⛔ 只声明「没有它这个任务就白做了」的产物(本批初稿)。闸产物
(`uncertain_recheck_*` / `reason_alignment_*` / `exclusion_direction_check_*`)不要写进去:
它们由闸脚本生成,漏跑闸应该由闸状态回报暴露,而不是被误报成"产物缺失"。

## 主代理收到 result 后的验收（不信自述，只信闸产物）

⛔ 子代理自称「闸全绿」不构成通过。主代理必须自己跑一遍结构闸再决定下一步。
**分两层**：每批回来先验该批，全批到齐后合并再验整轨。

```bash
# ① 每批回来：批级口径
python3 /mnt/skills/custom/eligibility-judgment/scripts/check_judgment_structure.py \
    --workspace /mnt/user-data/workspace --patient {id} --track {SHARD} --batch {BATCH}

# ② 全批到齐、merge-judgments 合成本轨 draft 之后：整轨口径（⛔ 不带 --batch）
python3 /mnt/skills/custom/eligibility-judgment/scripts/check_judgment_structure.py \
    --workspace /mnt/user-data/workspace --patient {id} --track {SHARD}
```

- ① `exit 0` → 该批收下，等其余批次。
- ② `exit 0` → 进入 QC。
- 任一 `exit 2` → ⛔ **回派对应批次重出产物，禁止自己转码/写脚本修结构**。理由见下方，
  详细处置在 `judgment-repair.md`「结构闸不过时的唯一处置」。

⛔ **②不可省，也不能用「每批都过了」代替**：批级闸各自只保证「本批完整」，
没有任何一道闸会因为**少了一整批**而报错——只有整轨口径的闸 2 会。
漏派一批而跳过②，缺口会一路静默到交付。

## 判定 task 失败后的处置（⛔ 禁止盲目重派）

`task` 对失败的子代理**只自动重试一次**，且**资源上限类失败不重试**。回报文本里出现
`Stop reason: recursion_limit` 或 `Stop reason: token_budget` 时，说明这次失败是
**额度用尽**（步数上限 / token 预算），不是偶发故障 —— 同样的活儿再跑一遍只会把同样的
额度再烧一遍。故障档案：`references/failure-archive.md`#判定失败被盲目重派
（会话 `d393714d`：IN 轨判定烧到 6.36M token 后 failed，重试又花 5.21M，两次撞同一个上限）。

带 `Stop reason` 的失败，主代理**必须**按下面三步走，**禁止**原样重派：

1. **先读产物再决策**：`read_file` 失败那一批的 `judgments_draft_{id}_{SHARD}_b{BATCH}.json`
   与 `uncertain_recheck_{id}_{SHARD}_b{BATCH}.json`，确认已经判完哪些条目、卡住哪些条目
   （门禁产物里的 `suspected_missed` / `stuck_items` 就是卡点清单）。
   ⚠️ **只看失败那一批**：其余批次是独立文件、独立子代理，与本次失败无关，不要一起重判。
2. **只补跑还能推进的部分**：把**该批未判完的条目 ID 显式列进新的委派 prompt**（第 0 条），
   让子代理在已有批级初稿上续判；⛔ 不得整批重判、更不得整轨重判
   （那是把已成功的部分再花一遍钱）。若该批反复撞限，用更小的 `--batch-size` 重新
   `plan-batches` 只切该批的残余条目。
3. **卡点无法自证时转人工**：若卡住的条目全是阻断级且证据不足，标 `存疑` +
   `gate_escalated=true` 并在最终报告里点名，交人工复核；⛔ 不得靠反复重跑碰运气。

> 无 `Stop reason` 的普通失败（JSON 不合法、工具报错等）按原规则处理：`task` 已自动重试
> 过一次，若仍失败同样按上面三步走。

**分批本身就是对这类失败的结构性防护**：会话 `09eeaffb` 整轨一次派时，撞限 = 整轨 0 产物
（`recursion_limit` 分支不打捞部分产物），主代理只能整轨重判。分批后同样的撞限只损失一批，
其余批次的判定已在磁盘上——这是分批相对整轨派发的主要收益，不是副作用。

## 派发节奏

- 按并发预算打满、滑动窗口补派（编排层负责）。
- **按患者流水**：某患者 OCR 就绪即可派该患者的两轨任务，不等其他患者。
- 派发顺序：同一患者的 IN/EX 尽量放同一批（便于后续按患者收口），患者间按 OCR 体量从大到小排队。
- **批次之间无依赖**，可全并行：不同批写不同文件、读同一份只读的标准包与 OCR。
  受并发预算限制时按批号顺序补派即可，⛔ 不要因为"想省一次派发"而把两批合成一个任务
  —— 那就退回了会话 `09eeaffb` 的失败形态。

## 轨道产物命名

判定阶段的产物**按批**落盘（`_b{N}`），合并后才有本轨文件：

| 产物 | 路径 |
|---|---|
| **本批**判定初稿 | `workspace/patients/{id}/judgments_draft_{id}_{SHARD}_b{N}.json` |
| **本批**兜底闸产物 | `workspace/patients/{id}/uncertain_recheck_{id}_{SHARD}_b{N}.json` |
| **本批** reason 对齐产物 | `workspace/patients/{id}/reason_alignment_{id}_{SHARD}_b{N}.json` |
| **本批**结构闸产物 | `workspace/patients/{id}/judgment_structure_gate_{id}_{SHARD}_b{N}.json` |
| EX 轨**本批**方向校验产物 | `workspace/patients/{id}/exclusion_direction_check_{id}_EX_b{N}.json` |
| 批次清单 | `workspace/patients/{id}/judge_batches_{id}_{SHARD}.json` |
| 本轨判定初稿（各批合并后，主代理产出） | `workspace/patients/{id}/judgments_draft_{id}_{SHARD}.json` |
| 本轨兜底闸产物（各批合并后） | `workspace/patients/{id}/uncertain_recheck_{id}_{SHARD}.json` |
| 本轨结构闸产物（整轨口径，QC 前置读这一份） | `workspace/patients/{id}/judgment_structure_gate_{id}_{SHARD}.json` |

以上均为**过程产物**：不 present、不移入 `outputs/`，由合并后的文件代表（见 SKILL.md「交付文件清单」）。

⚠️ QC 子代理的前置自检读的是**整轨**那份 `judgment_structure_gate_{id}_{SHARD}.json`
（批级的带 `_b{N}` 后缀，不会覆盖它）——所以整轨口径的结构闸必须真的跑过，
否则 QC 会读到一份不存在或过期的闸产物并按规矩自行拒工。
