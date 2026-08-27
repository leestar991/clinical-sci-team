# 双轨解析委派模板（IN 轨 / EX 轨）


> ⛔ **解析子代理的 `skills` 白名单是 `[]`**：它**不会自动加载任何 SKILL.md**，规则只能靠模板里
> 给出的绝对路径去 `read_file`。所以 `references/parsing-rules.md` 这一行**不得删除、不得改成
> 指向 SKILL.md**（SKILL.md 是主代理的编排手册，38KB 里绝大部分与解析无关）。
> ⛔ 也不要把规则正文抄进模板——30KB 规则抄一遍就是两份权威，必然漂移。

> 入选轨与排除轨的解析彼此独立（条目不重叠、编号前缀不同），派成两个并行子任务：
> 并行度 ×2、每个子代理输入减半、单任务轮次下降。
>
> 前置：`workspace/eligibility_criteria_raw.md` 已通过完整性自检，
> `workspace/criteria_meta.json` 已含 `段行号`（见 `criteria-extraction.md`）。
>
> ⛔ 两轨都**不产出** `方案元数据` / `解析说明` / `汇总统计`——那是全篇级/常量字段，
> 由 `parse_pack.py assemble` 注入与重算。两轨各自只产出 `四分类`（本轨 2 个类目）+
> `描述索引`（本轨前缀）。

## 共同约束（两个模板都必须带）

- **只读本轨那一段**：用 `read_file(start_line, end_line)` 按 `criteria_meta.json.` **`raw段行号`**
  读取本轨区间，**禁止**读全文（两轨都读全文会让 token 翻倍）。
  ⛔ **必须用 `raw段行号`，不是 `段行号`**：后者是 `uploads/试验方案.md` 的坐标（数千行），
  前者才属 `eligibility_criteria_raw.md`（数百行）。两者相差一个数量级，用错必然越界，
  而 `read_file` 对越界切片返回**静默空字符串**（不报错、也不是 `(empty)`）。
- ⛔ **开工第一步自检读到的原文非空**（必做，四条全过才能开始解析）：
  1. `read_file` 返回**非空**；
  2. 首行包含本轨章节标题（IN 轨「入选标准」/ EX 轨「排除标准」）；
  3. 行首编号从 `1.` 开始且最大编号等于 `criteria_meta.json.末条号` 的本轨值；
  4. 条目数与末条号一致（允许子项，但顶层编号必须连续无缺）。
  任一条不过 → **立即停止并在 result 中报告**「输入原文不可用：<具体现象>」，
  ⛔ **绝不允许凭已有知识补写或推测标准内容** —— 该试验的标准只存在于原文里，
  凭空生成的条目看起来完全合理却与方案无关，会一路通过所有结构闸直到污染患者判定
  （历史故障 thread `6e5ac7c1`：两轨因越界读到空串，54 条里编了 50 条，
  出现原方案根本没有的 `DPD 缺乏`/`Gilbert 综合征`/`第三间隙积液`）。
- **禁止读原始方案文档**：**禁止**对 `uploads/` 下的入排标准/方案原始文档
  （`.docx`/`.pdf`/`.md`）调用 `parse_document` 或 `read_file`——其内容已逐字提取进
  `eligibility_criteria_raw.md`，再解析属重复（历史故障：`入排标准.docx` 被重复 parse 一次）。
  若发现本轨区间有缺条，在 result 中报告缺失条号，由主代理补提取，不得自行去解析原始文档。
- ⛔ **结构规则速查（写的时候就照着做，不要写完再靠 QC 发现）** —— 这 5 条与
  `check_track_structure.py` 的机械判据一一对应，违反哪条脚本就报哪条：
  1. **条件ID 唯一**，且前缀与轨一致（IN 轨只 `IN-*`，EX 轨只 `EX-*`）；格式
     `{TRACK}-{原条号}[-{子序号}]`。
  2. **子序号不混用**：一个原条号要么只有不带子序号的 `X-n`，要么只有带子序号的
     `X-n-1..X-n-k`，**不得两者并存**；子序号必须从 1 连续（不跳号）。
  3. **`描述索引` 与 `四分类` 实体双向一一对应**：实体有的键索引必须有，索引有的键实体必须有，
     不多不少。新增/删除/改名条目时**同一批**改掉索引。
  4. **原条号 `1..末条号` 全覆盖**（末条号取 `criteria_meta.json.末条号` 的本轨值）：
     每个原条号至少要有一个实体条目，一个都不能整体缺失。
  5. **每条 `原文` 必须在 `eligibility_criteria_raw.md` 中逐字可查**（脚本抹空白 + NFKC 后做子串
     匹配）。⛔ 不要转述、不要合并两处原文、不要补全原文没有的词；照抄本轨区间里的句子。
- ⛔ **落盘后自己跑结构闸，`exit 0` 才允许返回**：
  ```bash
  python3 /mnt/skills/custom/criteria-parser/scripts/check_track_structure.py \
      --workspace /mnt/user-data/workspace --track {TRACK}
  ```
  `exit 2` → 按 stdout 点名的 problems **自行修**（只用 `str_replace`，禁止 `write_file` 重写），
  修完再跑，直到 `exit 0`。修不过则在 result 里**如实回传 problems 原文**，不得隐瞒或声称已通过。
  > 为什么由你来跑而不是交给主代理：结构问题在你的上下文里最好修（你刚写完、知道每条的来历）；
  > 交回去等于让主代理发现问题后整轨重做，代价是一整个子任务
  > （thread `afb85bcd`：首检命中 `[IN] 闸9 2/27`、`[EX] 闸4 EX-9 混用 + 闸9 6/26`，
  > 主代理只能重派两个「重做解析」子任务；而这些问题上述 5 条速查全都覆盖）。
  > 结构闸修复**不占** QC 轮次预算，跑几次都不亏。
- ⛔ **重做 = 在现有产物上修订，禁止 rm/重建**（会话 `881e7ba8`：重做任务 `rm -f` 初版
  拆分产物后手写重建，上下文已耗尽，27 个实体只精拆了 2 个、17 条沦为「待人工判定」
  占位符——拆分版被销毁、降级版畅通无阻）。`criteria_parsed_*.json` 已存在时：
  先 `read_file` 看既有条目，**只改/只补需要改的条目**（`str_replace` 或分片
  `write_file` 追加），已正确的条目原样保留；`bash rm/mv` 删产物已被
  BashWritePolicy 机械拒绝，不要尝试绕过。与判定域「结构闸不过 = 回派重判，
  禁转码修复」同构。
- **只填本轨前缀**：IN 轨只产 `IN-*` 条目、只写 `入选_*` 两个类目；EX 轨只产 `EX-*`、
  只写 `排除_*` 两个类目。越界会被 `parse_pack.py slim` 的单轨结构闸阻断
  （历史故障 thread `5a1c8d95` 的同类错误：EX-* 被写进「入选_不可从病例获取」，
  切出 IN 46 条 / EX 1 条的残缺判定包）。
- **分片写入**（硬规则，见 `references/parsing-rules.md`「输出必须分片写入」）：禁止单次全量 `write_file`。
- 禁止 `task` / `present_files`；禁止 `ls`/`glob` 探索（路径已给全）。
- **result 只回传**：产出文件路径 + 本轨各类目条目数 + 本轨末条号覆盖情况（`1..N` 是否连续）
  + 缺条清单。**禁止**回传条目正文。
- ⛔ **委派时必须带 `expected_outputs`**（`task` 参数，机械后置校验）：IN 轨传
  `expected_outputs=["/mnt/user-data/workspace/criteria_parsed_IN.json"]`，EX 轨传
  `criteria_parsed_EX.json`；派 QC 时传 `criteria_qc_{TRACK}.json`。
  harness 在子代理被允许报 `completed` 之前探一次该文件，不存在或为空 → 该 task 判 `failed`
  并点名缺失路径，`task` 自动重派一次。分片写入的产物同样适用：**首片都没落地**是最该被立刻
  发现的失败。根因见 `/mnt/skills/custom/eligibility-judgment/references/judge-delegation.md`
  「委派时必须带 `expected_outputs`」（会话 `88df83a8`：子代理写了自创文件名却回报 completed）。

## 模板 ①：IN 轨（入选标准）

```
请按 /criteria-parser 技能规则，把**入选标准**解析为结构化 JSON。本任务只处理入选标准，不涉及排除标准。

输入：
- /mnt/user-data/workspace/eligibility_criteria_raw.md 的**入选段**：read_file(start_line={raw段行号.入选.start}, end_line={raw段行号.入选.end})
  ⛔ 用 `raw段行号`（raw.md 自身坐标），**不是** `段行号`（试验方案.md 坐标，会越界读成空串）；
  读到的内容必须非空、首行含「入选标准」、顶层编号 1..{末条号.入选} 连续，否则停止并报告
- Schema：/mnt/skills/custom/criteria-parser/references/schema_example.json（只看 `四分类.入选_*` 与 `描述索引` 部分）
- 规则（**唯一权威，渲染时已内嵌本任务所需各节，⛔ 无需再读 parsing-rules.md 全文**）：

{PARSING_RULES}

（完整规则在 /mnt/skills/custom/criteria-parser/references/parsing-rules.md，本任务所需各节已全部内嵌。）


输出：/mnt/user-data/workspace/criteria_parsed_IN.json
⛔ 落盘后必须自己跑结构闸到 exit 0 才返回（命令见共同约束；exit 2 就按点名的 problems 自行 str_replace 修，修不过则如实回传 problems 原文）。结构闸不占 QC 轮次，跑几次都不亏。
```json
{
  "四分类": {
    "入选_可从病例获取": { "IN-2-1": { "条件ID": "IN-2-1", "...": "..." } },
    "入选_不可从病例获取": { "IN-1-1": { "条件ID": "IN-1-1", "...": "..." } }
  },
  "描述索引": { "IN-1": "短描述", "IN-2": "短描述" }
}
```

要求：
- **每个类目是以 `条件ID` 为键的对象（dict），不是数组**；key 必须逐字等于条目内的 `条件ID` 字段
  （闸13 阻断校验）。这样修订阶段的 pointer 才能按身份定位 `/四分类/{类目}/{条件ID}/...`
- 逐条拆分入选标准为最小子颗粒度（AND 拆、OR 不拆、豁免入 `除外` 字段）
- 判定每条的可获取性；"可从病例获取"条目必须填非空 `同义词` + `证据位置`（硬规则）
- "不可从病例获取"条目的 `转化条件` 与 `日期维度` 填 `null`
- 条件ID 一律 `IN-{原条号}[-{子序号}]`，**禁止**出现任何 `EX-` 前缀条目
- 只写 `入选_可从病例获取` 与 `入选_不可从病例获取` 两个类目，**禁止**写 `排除_*` 类目
- **禁止**产出 `方案元数据` / `解析说明` / `汇总统计`（由 assemble 注入与重算）
- 按 `parsing-rules.md`「输出必须分片写入」的节奏落盘（每批 ≤15 条），禁止单次全量 write_file
- 若入选段末条号与 criteria_meta.json 的 `末条号.入选` 不一致，在 result 中报缺条号
```

## 模板 ②：EX 轨（排除标准）

```
请按 /criteria-parser 技能规则，把**排除标准**解析为结构化 JSON。本任务只处理排除标准，不涉及入选标准。

输入：
- /mnt/user-data/workspace/eligibility_criteria_raw.md 的**排除段**：read_file(start_line={raw段行号.排除.start}, end_line={raw段行号.排除.end})
  ⛔ 用 `raw段行号`（raw.md 自身坐标），**不是** `段行号`（试验方案.md 坐标，会越界读成空串）；
  读到的内容必须非空、首行含「排除标准」、顶层编号 1..{末条号.排除} 连续，否则停止并报告
- Schema：/mnt/skills/custom/criteria-parser/references/schema_example.json（只看 `四分类.排除_*` 与 `描述索引` 部分）
- 规则（**唯一权威，渲染时已内嵌本任务所需各节，⛔ 无需再读 parsing-rules.md 全文**）：

{PARSING_RULES}

（完整规则在 /mnt/skills/custom/criteria-parser/references/parsing-rules.md，本任务所需各节已全部内嵌。）


输出：/mnt/user-data/workspace/criteria_parsed_EX.json
⛔ 落盘后必须自己跑结构闸到 exit 0 才返回（命令见共同约束；exit 2 就按点名的 problems 自行 str_replace 修，修不过则如实回传 problems 原文）。结构闸不占 QC 轮次，跑几次都不亏。
```json
{
  "四分类": {
    "排除_可从病例获取": { "EX-1-1": { "条件ID": "EX-1-1", "...": "..." } },
    "排除_不可从病例获取": { "EX-20": { "条件ID": "EX-20", "...": "..." } }
  },
  "描述索引": { "EX-1": "短描述", "EX-2": "短描述" }
}
```

要求：
- **每个类目是以 `条件ID` 为键的对象（dict），不是数组**；key 必须逐字等于条目内的 `条件ID` 字段
  （闸13 阻断校验）。这样修订阶段的 pointer 才能按身份定位 `/四分类/{类目}/{条件ID}/...`
- 逐条拆分排除标准为最小子颗粒度（AND 拆、OR 不拆、豁免入 `除外` 字段）
- 判定每条的可获取性；"可从病例获取"条目必须填非空 `同义词` + `证据位置`（硬规则）
- "不可从病例获取"条目的 `转化条件` 与 `日期维度` 填 `null`
- 条件ID 一律 `EX-{原条号}[-{子序号}]`，**禁止**出现任何 `IN-` 前缀条目
- 只写 `排除_可从病例获取` 与 `排除_不可从病例获取` 两个类目，**禁止**写 `入选_*` 类目
- **禁止**产出 `方案元数据` / `解析说明` / `汇总统计`（由 assemble 注入与重算）
- 按 `parsing-rules.md`「输出必须分片写入」的节奏落盘（每批 ≤15 条），禁止单次全量 write_file
- **同一条排除标准同时含"客观可取证部分"与"研究者主观评估部分"时必须按可获取性拆到两个类目**，
  不要整条塞进"不可从病例获取"而丢掉可客观核验的分支（历史阻断项 CQC-R2-002：EX-16
  被整条归入一类，且字段与类目自相矛盾）
- 若排除段末条号与 criteria_meta.json 的 `末条号.排除` 不一致，在 result 中报缺条号
```

## 模板 ③：整轨重做（结构闸未过后的重派，仅单轨）

> 与模板①②的差异只有三处：重做纪律（禁 rm 重建）、结构闸点名注入、产出物是
> **对既有产物的修订**。其余输入/输出/要求与对应轨模板逐字相同。

```
请按 /criteria-parser 技能规则，**重做{轨名}整轨解析**——目标是对现有 /mnt/user-data/workspace/criteria_parsed_{TRACK}.json 的修订，使其通过结构闸。

⛔ **重做 = 在现有产物上修订，禁止 rm/重建**（会话 `881e7ba8`：重做任务 `rm -f` 初版
拆分产物后手写重建，上下文已耗尽，27 个实体只精拆了 2 个、17 条沦为「待人工判定」
占位符——拆分版被销毁、降级版畅通无阻）。`bash rm/mv` 删产物已被 BashWritePolicy
机械拒绝，不要尝试绕过。先 `read_file` 既有产物：**已正确的条目原样保留**，只改/只补
结构闸点名涉及（及其关联）的条目。

输入：
- /mnt/user-data/workspace/eligibility_criteria_raw.md 的**{轨名}段**：read_file(start_line={raw段行号.start}, end_line={raw段行号.end})
  ⛔ 用 `raw段行号`（raw.md 自身坐标），**不是** `段行号`（试验方案.md 坐标，会越界读成空串）
- 结构闸点名（重做依据，来自 criteria_structure_gate_{TRACK}.json 的 problems）：

{GATE_PROBLEMS}

- 规则（**唯一权威，渲染时已内嵌本任务所需各节，⛔ 无需再读 parsing-rules.md 全文**）：

{PARSING_RULES}

（完整规则在 /mnt/skills/custom/criteria-parser/references/parsing-rules.md，本任务所需各节已全部内嵌。）

输出：/mnt/user-data/workspace/criteria_parsed_{TRACK}.json（修订既有文件，分片写入）
⛔ 落盘后必须自己跑结构闸到 exit 0 才返回；修不过则如实回传 problems 原文。
其余拆分/类目/字段要求与对应轨模板（模板①或②）逐字相同，其中「{轨名}」全部按本轨理解。
```

## 收尾（主代理，两轨都返回后）

两轨 QC 各自收敛后（见 `criteria-qc-checklist.md`），主代理按轨切分判定输入包并合成全量包：

```bash
python3 /mnt/skills/custom/criteria-parser/scripts/parse_pack.py slim \
  --criteria /mnt/user-data/workspace/criteria_parsed_IN.json \
  --qc /mnt/user-data/workspace/criteria_qc_IN.json --track IN \
  --out /mnt/user-data/workspace/criteria_judge_IN.json

python3 /mnt/skills/custom/criteria-parser/scripts/parse_pack.py slim \
  --criteria /mnt/user-data/workspace/criteria_parsed_EX.json \
  --qc /mnt/user-data/workspace/criteria_qc_EX.json --track EX \
  --out /mnt/user-data/workspace/criteria_judge_EX.json

python3 /mnt/skills/custom/eligibility-judgment/scripts/parse_pack.py assemble \
  --in-criteria /mnt/user-data/workspace/criteria_parsed_IN.json --in-qc /mnt/user-data/workspace/criteria_qc_IN.json \
  --ex-criteria /mnt/user-data/workspace/criteria_parsed_EX.json --ex-qc /mnt/user-data/workspace/criteria_qc_EX.json \
  --meta /mnt/user-data/workspace/criteria_meta.json \
  --out /mnt/user-data/workspace/criteria_parsed.json
```

闸门语义与不可绕过项见 `parse_pack.py` 模块 docstring；被拦住时不要加
`--force-qc-unconverged` 硬闯，按提示先修标准或按编排层的暂停策略请示用户。
