# eligibility-screener 静默失败闸门加固 + token 优化变更汇总

> 实施时间 2026-07-31 ～ 2026-08-01。关联分支：`feature/gpt-team`。
>
> 来源：连续 5 个真实会话的监控分析。每个会话暴露一类**静默失败**——错误被工具或自检吞掉，
> 流程一路报「通过」直到污染最终交付。最后一项是基于同一批会话的 token 成本核算与流程优化。
>
> 上一轮变更见 [eligibility-screener-monitoring-optimization-changelog.md](./eligibility-screener-monitoring-optimization-changelog.md)。
> 双轨拆分方案见 [plans/eligibility-screener-soul-skill-split-dual-track-plan.md](./plans/eligibility-screener-soul-skill-split-dual-track-plan.md)。

---

## 1. 变更概览

| # | 来源会话 | 症状 | 根因类别 | 新增机制 |
|---|---------|------|---------|---------|
| 1 | `345f2bf4` | P2 QC 空转 3 轮报同样阻断项；两轨判定合并成 2 份假文档 | 无「上游缺陷」出口；委派参数未指定 | QC 第三档 `upstream_issues`、闸8 打转探测、`{DOC_KEYS}`、闸9 document 键、闸产物自拒工 |
| 2 | `ec37dc7d` | Phase 1 该交付的 2 份产物用户没拿到 | 工具只收 `outputs/` 路径且**不校验存在性** | 原则 9「present 三步法」+ 四处 present 站点修正 |
| 3 | `ec24d087` | 排除标准 20 条只提取 11 条，研究周期等章节全漏 | `grep -n` 与 `read_file`（`splitlines()`）行号错位，自检在同一错误坐标系里自证 | `locate_criteria_sections.py`（独立源基线 + 坐标系告警） |
| 4 | `6e5ac7c1` | 54 条入排条件里 50 条（92%）内容凭空生成 | 段行号用于读错文件 → 越界切片**静默返回空串** | `raw段行号` + 结构闸 9（原文忠实性）+ 子代理开工自检 |
| 5 | `69612125` | 26 页里 11 页文本层证据丢失，判定因此错判 | 覆盖率分母只算 `scanned` 页 | `collect_text_pages.py` + 分母口径改为全部页按型分派 |
| 6 | `69612125` | 单会话 **16.74M tokens** 且未跑完 | 修订亲做、子任务过细、轮次欠载 | 修订强制委派、`--show`、OCR 6-9 页/子任务、SOUL 压缩 |

**统计**：SOUL 1 份 + 3 个 skill（15 份文档/脚本）+ 2 个新脚本 + 1 份新 reference；
测试 319 个用例（新增 4 个测试文件共 170 例，改写 1 例）。

### 贯穿全部 5 类故障的同一模式

**自检与被检对象同源 → 检查退化成自证。** 五次故障里有四次属于这一类：

- `#3` 源末条号从模型自己的提取结果数出来，`n == N` 恒成立；
- `#4` 条件ID 体系自洽（IN-1..11 / EX-1..20 全覆盖、子序号连续、索引对齐），
  闸 1-8 全过，但**没有任何一道闸比对过内容与原文**；
- `#5` 分母由「需要 OCR 的页」定义，而丢的正是「不需要 OCR 但需要进证据库」的页；
- `#2` present 的目标文件不存在，工具却返回 `Successfully presented files`。

⇒ 本轮所有新闸的共同设计要求：**基线必须独立于被检对象**。
源末条号只能来自源文件；`原文` 必须在 raw.md 中逐字可查；document 键必须等于真实 OCR 来源集合；
分母必须来自 manifest 的全部页而非「需要 OCR 的页」。

---

## 2. 逐项变更详情

### 2.1 `345f2bf4` — QC 不收敛 + 合并成假文档（A–E 五项）

**问题 1：QC 空转。** 5 轮 QC 里第 3/4/5 轮报**完全相同**的阻断项（`IN-10-4` + `IN-10-6`），
配额烧尽并触发两次用户暂停。根因是一个逻辑上无解的困境——入选第 10 条原文在 PDF→MD 提取中
**丢了否定词**，句子自相矛盾：

> 「血小板 ≥ 100×10^9/L，在 1 周内没有输注过血小板，接受过促血小板生成素或血小板受体激动剂等
> 血小板生成刺激剂治疗。」

R5 的 finding 逐字记下了死锁：「如实体条目仍以明确否定约束执行，则属于超出原文补写；
如仅以待核实备注保留，则当前子条件本身缺乏可执行判定口径。**无论哪种情况**…」
两条修复路径（R3→R4 否定式、R4→R5 待核实备注）都被阻断。三层根因：
P1 完整性自检只校验末条号连续性（条目级），检测不到**句内**缺词；QC 只有阻断/建议两档，
没有「上游无解」出口；轮次上限暂停后用户「继续」又跑了一遍同样的循环，没有任何机制察觉是同一项在打转。

**问题 2：假文档。** `merge-judgments` 产出：

```
条目 60 条；summary={'combined_ocr':      {符合15, 不符合0, 存疑5, 无法判断8},   ← IN 轨 28 条
                    'screening_bundle':  {符合24, 不符合0, 存疑0, 无法判断8}}   ← EX 轨 32 条
```

`merge_judgments()` 按 `doc_key` 走 `documents.setdefault(...)` 合并，而 `judge-delegation.md`
只写了「`documents.{doc}.judgments` 结构不变」，**从未规定 `{doc}` 该填什么**。
两个隔离的子代理各自取名，永远不会相撞。后果：报告的文档维度是假的；
约束 #17（任一 EX `不符合` → 建议排除）无法在单文档内聚合；约束 #7 逐文档独立判定从未真正执行。

**修复 A — document 键三层固定**

- `judge-delegation.md` 新增 `{DOC_KEYS}` 为**主代理填充参数**，两轨必须填同一套，
  逐字照抄 `phase2_summary.json.ocr_results[].source`；共同约束里加禁止自创的硬规则；
  落盘后自跑结构闸。
- `check_judgment_structure.py` **闸 9**：document 键集合必须等于真实 OCR 来源集合。
  用故障数据实测：`⛔ 闸9 document 键与真实 OCR 来源不符：实为 ['combined_ocr']，应为 ['筛选期检查','筛选期病历']`
- `judge_pack.py` 新增 `check_shard_documents_consistent()`，在 `merge-judgments` 入口
  以 `try/except SplitBlocked → return 2` 包裹（`slim` 路径原有此模式，`merge-judgments` 缺失）。
  实测 `exit=2` 且**不产出文件**。

**修复 B — QC 第三档 `upstream_issues`**

`criteria-qc-checklist.md` 新增一节，3 条判据：连续两轮同一 `condition_id` 且 `action` 方向相反 /
finding 自承两条路都不合规 / 原文缺失必要成分。`passed = blocking_issues 为空`
（`upstream_issues` **不**计入）。主代理三步处置：**允许破例**回读原始方案文档
（「禁止读原始方案文档」开一个窄口子，限定为「按 `upstream_issues` 点名的那句、仅核实、不重新解析」）
→ 回写 raw md → 修订；核实不了则一次性 `ask_clarification` 请用户裁定。

**修复 C — QC 原地打转探测（`check_track_structure.py` 闸 8）**

比对本轮与上一轮 `blocking_issues` 的条件ID **集合**（顺序无关），历史存于
`criteria_qc_history_{TRACK}.json`。集合为空时跳过；**同一轮重复跑脚本不误报**
（修复 E 要求每次 `str_replace` 后都跑闸，这点是必需的）。报错指向 `upstream_issues` 路径，
而不是「再试一次」。

**修复 D — 闸产物硬化（子代理自行拒工）**

两个结构闸脚本每次运行落盘 `criteria_structure_gate_{TRACK}.json` /
`judgment_structure_gate_{id}_{TRACK}.json`，含 `exit_code`、被检文件路径、
`content_sha256_16`（sha256 前 16 位）、`problems`、`checked_at`。
QC 委派模板首步强制读取：`exit_code != 0` **或文件缺失** → 立即返回「前置结构闸未过，拒绝执行」。
哈希用于发现「闸跑完后文件又被改过」。实测：

```
闸产物 content_sha256_16 = 276386afee070680
sha256sum 前16位        = 276386afee070680     ← 一致，可开工
改动判定文件后（未重跑闸）：
当前文件 sha256 前16位   = ad6048463dac163b     ← 失配，拒工
```

这是本轮唯一不依赖主代理守规矩的加固：`seq 804` 那次 `['bash','task','task']`
把结构闸与两个 QC 子代理同轮发出，违反了已写在三处的规则；R2 一整轮配额被浪费在
报告结构闸早已抓到的 JSON 语法错误上。

**修复 E — 每次 `str_replace` 后即跑结构闸**

`criteria-repair.md` 逐条流程与委派模板都改为改一条查一条，不等全部改完。
理由写入文档：本次连续两次 `Expecting ','`（line 313，修完又 line 401），
改一条查一条坏在哪一目了然；脚本调用不占 `task` 预算。
委派模板另加出口：若 `exit 2` 报的是闸 8 打转，**不要试第三种写法**，
在 `result` 里报告疑似上游原文缺陷。

**D 层补充（本轮后续追加）**：判定侧 `qc-delegation.md` 与 `reasons-delegation.md`
补上同样的闸产物前置自检。理由子代理的拒工理由不同并已写入模板——
理由是贴着结论写的，在未过闸的判定上写理由等于给还要被改判的结论配好文案，
改判后全部作废，还可能被 `merge-reasons` 的「结论不一致」闸拦住（返工两次）。
`judge-delegation.md` **不加**前置自检：判定子代理是闸产物的**生产者**，
开工时文件尚不存在，加了必然误拒；它的对应机制是落盘后自跑结构闸到 `exit 0` 再返回。

---

### 2.2 `ec37dc7d` — present 了不存在的文件，工具报成功

**现象**：Phase 1 产物都正常（自检 `IN_OK True` `EX_OK True`），但用户什么也没拿到。
`seq 71` present 的是 `/mnt/user-data/outputs/criteria_parsed.json`——P2 才有的文件，此刻不存在。

**根因**（`present_file_tool.py`）：

```python
# :78 —— 只接受 outputs/ 下的路径
raise ValueError(f"Only files in {OUTPUTS_VIRTUAL_PREFIX} can be presented: {filepath}")
```

两个硬约束此前都没写进 SOUL：**① 只接受 `/mnt/user-data/outputs/` 下的路径**；
**② 不校验文件是否存在**（只做 `relative_to(outputs_dir)` 归属检查）。
SOUL Phase 1 收尾写的是 present `workspace/pdf_classification.json`、
`workspace/eligibility_criteria_raw.md`——**一条不可能执行的指令**。
模型「修正」成一个 `outputs/` 下的路径，挑了后续阶段的交付物，因约束②工具返回
`Successfully presented files`，错误被完全吞掉。

**系统性范围**：四处 present 站点里三处指向 `workspace/`。

| 位置 | 原写法 | 问题 |
|---|---|---|
| Phase 1 收尾 | present `workspace/{pdf_classification.json,eligibility_criteria_raw.md}` | 无 cp，路径在 workspace → 必然失败 |
| Phase 2 收尾 | `cp criteria_parsed.json outputs/` 后 present `workspace/criteria_parsed.json` + 两个 qc | cp 了却仍传 workspace 路径；**两个 qc 文件根本没 cp** |
| Phase 2.5 | present `workspace/patient_index.json` + 各患者 `ocr_records.md` | 都在 workspace，无 cp |
| Phase 5 | present `outputs/*.html` | 唯一正确的 |

另外原则 9 的交付清单里**根本没有** `pdf_classification.json` 和 `eligibility_criteria_raw.md`
——既不在必交付、也不在过程文件、也不在不交付。

**修复**：规则收敛到原则 9 一处「present 三步法」，各阶段只列清单不重复命令：

```
1. bash cp <本阶段交付清单> outputs/ && ls -l outputs/
   —— 拷进 outputs 并确认存在且非 0 字节（ls 不可省，工具不会替你查；
      写在同一条 bash 里才保证串行）
2. 下一轮 present_files，传的路径必须是 outputs/...（⛔ 不是 workspace/...）
⛔ 两步不得同轮：同轮的 bash 与 present_files 并发，present 可能跑在 cp 之前
```

四处站点全部改写（Phase 1 拆成第六轮 cp+ls、第七轮 present，`ask_clarification` 顺移到第九轮，
Phase 1.5 轮次引用同步；Phase 2 补上漏掉的两个 qc 文件；Phase 2.5 用扁平命名
`outputs/ocr_records_{id}_{source}.md` 避免子目录）。必交付清单补上两份 P1 产物，
目录规范同步登记。

**未做**：`present_files` 不校验存在性属 backend 共享工具行为，改动影响所有 agent，
已向用户说明并留待决定（在 `_normalize` 加 `if not actual_path.is_file(): raise` 即可从根堵住）。

---

### 2.3 `ec24d087` — 换页符导致行号错位，自检循环论证

**现象**：排除标准真实 20 条只提取 11 条；`3.6 研究周期` 等补充章节全漏。
会话自检输出 `10 11`（IN 末条号 10、EX 11），而原文权威计数是 IN 11 / EX 20。

**根因**：`read_file` 在 sandbox 的实现是

```python
# backend/packages/harness/deerflow/sandbox/tools.py:1781
content = "\n".join(content.splitlines()[start_line - 1 : end_line])
```

`str.splitlines()` 除 `\n` 外还在 `\f`(换页) `\v` `\x1c` `\x85` `\u2028` 等处断行，
而 `grep -n` / `awk NR` 只认 `\n`。这份 PDF 转出的方案 .md 有 **131 个 `\f`**，
其中 65 个在排除标准之前。复现该会话的自检脚本：

```
模型脚本（splitlines）  → IN 10  EX 11    ← 与会话输出「10 11」逐字一致
同脚本改 split("\n")    → IN 11  EX 20    ← 正确答案
```

**两个错误在同一错误坐标系里互相抵消**：`read_file` 按偏移的行号去读，只抄到 EX 1–11；
自检脚本在同一坐标系里数「源末条号」也得 11 → `n == N` 成立，自检报通过。
参考文档规定的自检口径本身是对的（源末条号从**源文件**取），但模型没执行第 1 步，
而是从自己的输出里数——这是本轮第一次出现的「循环论证」形态。

**修复**：新增 `criteria-parser/scripts/locate_criteria_sections.py`（349 行），
把定位与自检都做成机械脚本。对真实文件：

```
坐标系：splitlines（与 read_file 一致）
⚠️ 检出异常断行符：\f 换页×131 → splitlines 比 grep -n 多 131 行；
   **禁止**把 grep -n / awk NR 的行号喂给 read_file，用下方段行号
入选：行 3766-3881，源末条号 11
排除：行 3881-4052，源末条号 20
章节：4.1 入选标准 / 4.2 排除标准 → 终点 5 药物与治疗
补充章节（须一并提取）：3 研究设计、3.6 研究周期、6.2 筛选期、6.2.5 筛选期检查、
                        6.3.2 现场访视、6.4.1 现场访视、12 附录
```

`--verify-raw` 对该会话实际产出 `exit 2`，报出的正是用户反馈的两个问题：

```
⛔ 排除标准丢条：源文件声明 1..20，提取结果只到 1..11（缺 [12..20]）；
   源区间 3881-4052（splitlines 坐标，可直接喂 read_file）
⛔ 源文件存在但提取结果未包含的入排相关补充章节：
   3 研究设计（源行 3246）、3.6 研究周期（源行 3718）、6.2.5 筛选期检查（源行 4911）…
```

设计要点：

- **源末条号只能来自源文件**——打破循环论证的关键，主代理无从干预。
- 编号连续性用**从 1 起的最长连续前缀**而非 `max()`：`[1,2,3,3,4]→4`、`[1,2,9]→2`、`[2,3]→0`，
  对跨页重复编号、子项误命中、尾部杂散数字都稳健。
- **章节标题与条目必须区分**：`5.  首次给药前…` 是排除第 5 条，`5 药物与治疗` 是终止标题，
  差别只在编号后是否紧跟 `.`+空白。判错会让排除段在第 5 条就截断。
- **正文提及不算标题**：raw.md 里 `- 来源：试验方案.pdf 第4章（4.1 入选标准 / 4.2 排除标准）`
  一行同时提到两个标题，最初的分段逻辑被它骗到并报了假错误；现在只认 Markdown `#` 标题或编号标题行。
- **补充章节按「源文件里有才要求」**判定，方案里没有研究周期就不该报错。
- 偏移量计算需抹掉 `split("\n")` 对尾随换行多出的空元素，否则偏移量被凭空 -1，
  刚好抵消一个 `\f`（该 off-by-one 由测试抓出）。

`criteria-extraction.md` 里两段 `grep -n` / `awk NR` 片段全部删除换成脚本调用，
并写明为什么禁止（含 `read_file` 实现代码与完整因果）。

---

### 2.4 `6e5ac7c1` — 越界读成空串，92% 条件凭空生成

**现象**：54 条实体里 **50 条（92%）** 的 `原文` 在 `eligibility_criteria_raw.md` 中查不到。
不是编号错位，是内容属于另一个（通用 mCRC）试验：

| 条号 | 原文实际内容 | 解析结果写的 |
|---|---|---|
| IN-4 | 结直肠癌转移性疾病阶段：Ib 期要求既往至少一线治疗 | 根据 RECIST v1.1，至少有一个可测量病灶 |
| IN-6 | 需同意提供组织样本和外周血用于生物标志物研究 | ECOG 体力状态评分 0 或 1 分 |
| IN-7 | 必须至少有一个 RECIST v1.1 可测量的非颅内病灶 | 预期生存期≥3 个月 |
| EX-7 | 受试者存在显著的出血风险 | 有无法控制的第三间隙积液 |
| EX-18 | 既往有严重过敏史者 | 已知存在 DPD 缺乏 |
| EX-19 | 妊娠期或哺乳期患者 | 已知存在 Gilbert 综合征 |

`二氢嘧啶脱氢酶`/`DPD`、`Gilbert`、`第三间隙积液`、`肠梗阻` 在 raw.md 和 `试验方案.md` 里
**都是 0 次命中**。阈值也被改了：原文「预期生存时间≥6 个月」变成「预期生存期≥3 个月」——
这种看起来合理的错误比明显的错误危险得多。

**根因**：委派提示词本身写得清楚，但把 `段行号 3766-3881`（`uploads/试验方案.md` 的坐标）
用在了 **只有 794 行**的 `eligibility_criteria_raw.md` 上：

```
raw.md 实际行数 = 794
read_file(入选段 3766-3881) → 0 字符  ← 空字符串！
read_file(排除段 3881-4052) → 0 字符  ← 空字符串！
```

`if not content: return "(empty)"` 的兜底在切片**之前**，所以越界切片返回的是**裸空字符串**
——不报错，也不是 `(empty)`。两个子代理拿到空输入没有停下，按常识把整轨编了出来。

错误指令源头是 `parse-delegation.md` 第 36/68 行。这个矛盾一直存在
（`段行号` 从设计上就是源方案坐标，而双轨解析被明令「禁止读 uploads/」），
**是 `#2.3` 的修复把它引爆的**——`locate_criteria_sections.py` 让行号变得精确权威，
主代理于是放心照传；此前模型大概会因为数字可疑而干脆读整个 raw.md。

**为什么八道闸全过**：`[IN] total=25 原条号=11 个 miss_in_index=[] extra_in_index=[] ✅ 结构闸全过`
——条件ID 体系完全自洽，八道闸查的全是 ID 与结构的关系。

**修复三层**：

1. **结构闸 9（原文忠实性）**，不依赖病因的兜底。每条 `原文` 必须在 raw.md 中逐字可查，
   抹空白 + NFKC 归一后子串匹配（PDF 提取的空格/换行/全半角不稳定，直接匹配会大量假阳性）。
   实测 `[IN] 22/25，88%`、`[EX] 28/29，96%`。报错文案里带一条关键禁令：
   **⛔ 禁止逐条改写 `原文` 去迁就已生成的结论**——最省事的「修法」正是如此，
   那会让伪造的判定逻辑留下来还骗过闸。
2. **`raw段行号`**，根因修复。脚本落盘两套坐标各自标注归属文件：

   ```
   段行号     {"入选": {3766, 3881}, "排除": {3881, 4052}}  ← uploads/试验方案.md（⛔ 不可用于 read_file raw.md）
   raw段行号  {"入选": {126, 236},   "排除": {236, 387}}    ← workspace/eligibility_criteria_raw.md（双轨解析用这一套）
   raw总行数  794
   ```

3. **子代理开工自检**四条：读到非空、首行含本轨章节标题、顶层编号从 1 开始且最大值等于末条号、
   条目数一致。任一不过就停下报告，并明令**禁止凭已有知识补写标准内容**。

测试夹具需注意：「段行号越出 raw.md」这个故障条件在小夹具上复现不出来，
加 400 行前言把量级差还原成真实形态（方案数千行 vs raw.md 数百行）后才成立，
`test_protocol_far_larger_than_raw_is_the_realistic_shape` 显式守着这个前提。

---

### 2.5 `69612125` — 文本层页丢失导致错判

**现象**：`images/筛选期检查/` 有 15 个 `.jpg` + **11 个 `.txt`**（page_016–026），
`ocr/筛选期检查/` 只有 15 个 `.md`。manifest 记录完整：
`{"total_pages": 26, "text_pages": 11, "scanned_pages": 15}`。

**链条**：三个环节各自「合理」，合起来漏了。

1. `pdf_to_image.py --text-mode auto` 对有文本层的页只写 `.txt`、跳过图片渲染（省钱的正确设计）；
2. OCR 子代理只处理图片，`.txt` 无人认领；
3. `ocr_coverage.py` 分母只算 `type == "scanned"` 的页 → `need=15 done=15 covered=True ✅ 覆盖完整`。

第 3 步是关键，且是**上一次修复引入的**。脚本 docstring 还记着当时的理由：
「用 `total_pages` 当分母…文本层页没有图片也不需要 OCR，于是永远判不覆盖、白跑补漏轮次」。
那次修掉了假阴性却引入假阳性——**推理错在「不需要 OCR」被当成了「不需要进证据库」**。

**实际影响**：11 页共 32 KB，7 页含 RAS/KRAS。page_016 是基因检测报告：
`KRAS NM_004985.5 Exon 2 c.38G>A p.(G13D) 26.29% I 类`。而判定结果：

```
IN-4-1 [筛选期病历] 无法判断：缺基因检测报告及RAS突变具体结果；需补充分子检测报告
IN-4-1 [筛选期检查] 存疑：当前OCR片段未完整呈现RAS突变具体结论，需补充分子检测报告全文确认
```

判定层准确说出了缺什么，而缺的正是被覆盖率闸判为「已覆盖」的那 11 页。本该 `符合` 变成
`无法判断`/`存疑`。

**修复**：

- **分母改为全部页，按页型分派不同补法**（两次相反故障共同确定的口径）：
  `scanned` 缺 → 补派 OCR；`text` 缺 → 跑 `collect_text_pages.py` 归集，**禁止派 OCR**
  （图片都没渲染，派了只会触发 `view_image` 兜底白烧轮次）。分开报
  `missing_scanned` / `missing_text` 是必要的——混在一个 `missing` 里主代理只会派 OCR 补漏。
- **新增 `pdf-image-extractor/scripts/collect_text_pages.py`**（176 行），纯机械归集。
  逐字复制；**不生成 `key-fields` 速览**（需要语义理解，脚本做等于编造，
  与「严禁 bash 脚本做语义修订」一致），产物头部显式写「无 key-fields 速览，判定请读正文」；
  幂等；只认 manifest 里 `type == "text"` 的页；`.txt` 为空则 `exit 2` 并提示该页应改按扫描页渲染 + OCR。

端到端实测（真实数据副本）：

```
归集前：done=15  covered=False
归集：  ✅ 已归集 11 页文本层内容到 ocr/
归集后：done=26  covered=True   ✅ 覆盖完整
幂等：  新归集=0  幂等跳过=11
ocr/ 文件数 15 → 26，含 KRAS 的页 2 个
```

**测试改写**：既有 `test_route_b_denominator_excludes_text_pages` 固化的是旧口径，
改写为 `test_route_b_denominator_counts_all_pages_by_type`，保留两次故障的完整理由，
并**额外断言归集后必须转为 `covered=True`**——防止退回「永远判不覆盖」的老问题。
同时修了夹具一处不真实：原来所有页的 filename 都写成 `.jpg`，现按类型给 `.txt`/`.jpg`。

---

### 2.6 `69612125` — token 优化（只改流程与技能，不动阈值）

**基线**（同一会话，未跑完）：

```
RUN       状态          calls        in     out     total      lead       sub
ed78d450  success        20 1,012,240  22,149 1,034,389 1,033,855         0
064fe09b  success        29 7,362,353 117,042 7,479,395 2,570,776 4,893,783
2f06e23f  success        26 4,443,744  57,179 4,500,923 2,308,275 2,155,362
9a8d2fc2  success         9   920,698  15,672   936,370   811,199    98,649
4bcf5d32  interrupted    18 2,752,722  37,514 2,790,236 1,651,858 1,130,209
合计                     102 16,491,757 249,556 16,741,313 8,375,963 8,278,003
```

lead 50% / subagent 49% / middleware ≈0%。

**关键修正：成本不在 context 大小，在轮次数。** input 一直被 compaction 压在 **85–92K**，
压不下去。真实成本模型：

- **一轮 lead ≈ 89,105 tokens**（94 轮）
- **一个子代理全程 ≈ 295,642 tokens**（28 个 `task`）

⇒ 把 N 轮 lead 工作搬进 1 个子代理净省 `N×89K − 296K`，**N≥4 即回本**。

**四个浪费源与对应改动**

| # | 实测 | 根因 | 改动 |
|---|---|---|---|
| ① | QC+修订区间占 **54/93 轮（58%）**，`read_file` 79 次对 `str_replace` 15 次（读写比 5.3:1）；那 25 个 `task` 里**一个修订的都没有**（更早的同类故障 `5d987e97`：主代理为修订连续 14 分钟、7 轮各 6-7 个 `read_file`，触发 3 次上下文压缩） | `criteria-repair.md` 写成「亲做 **或** 委派（二选一）」，还有一段说「亲做不占 task 槽位」把亲做说得更省；4 个「委派前提」全是主代理写模板时就能满足的，是个假出口 | 标题与执行者节改成**一律委派**；4 条改为「委派模板必备要素，不构成改回亲做的理由」；删掉误导并加实测账。`judgment-repair.md` 同步 |
| ② | `criteria_parsed_EX.json` 被读 **33 次**（`_IN.json` 20 次），同一区间最多重复 3 遍；`read_file` 总量 **63% 是重复读** | 构造 `str_replace` 要看目标文本，行区间猜不准就再读一遍 | 新增 `check_track_structure.py --show <条件ID>`，按 ID 直取。实测 `--show` 1,530 字符 vs `read_file(350,470)` 3,984 字符（**省 62%**），且**不会读错位置**——那才是 63% 重复读的来源 |
| ③ | 28 页 OCR 派了 **16 个子任务（1.75 页/个）** | `ocr-delegation.md` 把「`parse_document` **每轮** ≤2-3 个」误读成「每**子任务** ≤2-3 张」——轮与子任务混淆 | 改为每子任务 **6-9 页**（3 张/轮 × 3 轮），并在技能与 SOUL 三处写明这是子代理**内部单轮**上限 |
| ④ | 93 轮里 **21 轮只发 1 个工具、7 轮只发 2 个**，58 轮恰好 3 个 | 模型把「`task` 每批 3 个」当成每轮工具总数上限；SOUL 另有「同类工具批量并行」但两条挨在一起，3 盖过了它 | 原则 1 澄清「每批 3 个」**只约束 `task`**，只读/脚本类工具应一轮打满 ≥4-6 个，并给出「每轮 ≈89K，凑不满就是白买一轮」的理由 |

**附带发现：SOUL 本身是每轮成本。** SOUL 是系统提示的一部分，94 轮每轮重发。
713 行 ≈14,835 tokens × 94 = **1.39M，占 lead 输入 17%**。每压 50 行约省 99K。

加完上述规则后 SOUL 涨到 757 行（超 750 上限）。按「只下沉不删规则」压回：
Phase 1.5 的 86 行里，`ask_clarification` 参数表、三段模式文案、落定映射表下沉到新建的
`pdf-image-extractor/references/mode-selection.md`（该技能本就拥有「路线由用户选择决定」的语义）。
三个故障编号 `ab76d625`/`459951c1`/`03a496cc` 及各自机制说明逐字留在 SOUL。
**结果 713 行，比本轮优化前的 739 少 26 行**；三段模式文案原来每轮都发（≈400 tokens × 94 = 37K），
现在整个会话只读一次。

**SOUL 上下文纪律的补强**（原则 6）。原则 6 **已经写了**「同一文件同一 run 最多 read_file 一次」
并引了 4.4M 那次故障，却被违反 33 次——因为它跟修订工作流直接冲突，工作流赢了；
而且紧挨着的「按段读、先定位」正是产生这 33 次读的那条。新增四条把需求消掉而非把规则说得更重：

- 有专用取值脚本时禁止读行区间（用 `--show`）；
- 修订/改判循环一律委派，主代理不得亲做（本原则最大的破口）；
- 脚本 stdout 即结论，不重读产物确认；状态类小 JSON 一 run 一读
  （实测 `criteria_meta` 6 次、`pdf_classification` 5 次、qc 报告各 4-6 次）；
- 技能文档不得全文重读（占 read 量 26%，`pdf-image-extractor/SKILL.md` 全文读了两次）。

**预期节省**（按实测单价核算，非实测结果）

| 项 | 净省 |
|---|---|
| ① 修订强制委派（54 轮 → ~15 轮，含 3 个修订子代理成本） | 2,588,169 |
| ② OCR 子任务 16 → 4 个（含省下的派发/收口轮次） | 4,260,544 |
| ③ 欠载轮次合并（约 12 轮） | 1,069,260 |
| ④ SOUL 压缩 757→713 行 | 147,110 |
| **合计** | **≈8.07M（基线 16.74M 的 48%）** |

`--show` 的收益主要体现在消除「读错再读」，已计入 ①，剩余部分保守不计。
②的量最大也最确定（子任务数可数）；①依赖模型遵守委派规则；
③依赖模型改变批处理习惯，是三项里最不确定的——它靠说服而非机制强制。

---

## 3. 逐文件变更

### 3.1 新增脚本

| 文件 | 行数 | 职责 |
|---|---|---|
| `criteria-parser/scripts/locate_criteria_sections.py` | 349 | 入排章节定位 + 提取完整性机械自检；统一 `splitlines()` 坐标系、报告断行符偏移、产出**独立**源末条号与 `raw段行号` |
| `pdf-image-extractor/scripts/collect_text_pages.py` | 176 | 把 PDF 文本层页的 `.txt` 逐字归集成 `ocr/{source}/{stem}.md`；幂等、不生成 key-fields |

### 3.2 修改的脚本

| 文件 | 行数 | 变更 |
|---|---|---|
| `criteria-parser/scripts/check_track_structure.py` | 466 | 新增**闸 8**（QC 原地打转）、**闸 9**（原文忠实性）、`write_gate_artifact()`、`--show` 取条目、`_norm_text()` |
| `eligibility-judgment/scripts/check_judgment_structure.py` | 329 | 新增**闸 9**（document 键等于真实 OCR 来源集合）、`write_gate_artifact()` |
| `eligibility-judgment/scripts/judge_pack.py` | 651 | 新增 `check_shard_documents_consistent()`，`merge-judgments` 入口包 `try/except SplitBlocked → return 2` |
| `pdf-image-extractor/scripts/ocr_coverage.py` | 214 | 分母改为全部页；新增 `_text_stems()`、`need_scanned`/`need_text`/`missing_scanned`/`missing_text`；两类缺口分别给补法 |

### 3.3 SOUL

`backend/.deer-flow/agents/eligibility-screener/SOUL.md` — **713 行**（gitignored）

| 位置 | 变更 |
|---|---|
| 原则 1 | 「每批 3 个」只约束 `task` 的澄清 + 欠载轮次的成本说明 |
| 原则 6 | 新增 4 条：专用取值脚本优先、修订一律委派、脚本 stdout 即结论/状态文件一 run 一读、技能文档禁止全文重读 |
| 原则 7 | `upstream_issues` 第三档 |
| 原则 9 | present 三步法 + `present_files` 两个硬约束 + 必交付清单补 P1 产物 |
| Phase 1 | 行号定位与自检改为强制调 `locate_criteria_sections.py`；收尾拆成 cp+ls / present 两轮 |
| Phase 1.5 | 参数表、模式文案、落定映射表下沉至技能；保留三条硬规则与故障编号；轮次顺移至第九轮 |
| Phase 2 | 双轨解析改用 `raw段行号` + 坐标混用警告；OCR 每子任务 6-9 页；覆盖率门禁先跑归集脚本 |
| Phase 2.5 | 交付改用三步法 + 扁平命名 |
| 目录规范 | 登记 `criteria_qc_history_*`、两个闸产物、`outputs/` 新增交付物、`criteria_meta.json` 字段更新 |
| Todolist 模板 | 修掉 2→3 轮改动遗漏的 `QC（≤2轮）` |

### 3.4 技能文档

| 文件 | 行数 | 变更要点 |
|---|---|---|
| `criteria-parser/SKILL.md` | 402 | 8→9 闸 + 闸产物 + `upstream_issues` + 两套坐标系 + `locate_criteria_sections.py` |
| `criteria-parser/references/criteria-extraction.md` | 136 | 删掉 `grep -n`/`awk NR` 片段，改为脚本调用；自检改为 `--verify-raw`；`criteria_meta.json` 结构更新 |
| `criteria-parser/references/criteria-repair.md` | 318 | **一律委派**；`--show` 取条目；每次 `str_replace` 后跑闸；改完不得重读确认 |
| `criteria-parser/references/criteria-qc-checklist.md` | 198 | `upstream_issues` 第三档（判据/schema/处置/故障记录）+ 闸产物前置自检 |
| `criteria-parser/references/parse-delegation.md` | 137 | 改用 `raw段行号` + 开工四条自检 + 禁止凭知识补写 |
| `eligibility-judgment/SKILL.md` | 696 | 9 闸 + 闸产物 + document 键硬规则 + 两类子代理前置自检 |
| `eligibility-judgment/references/judge-delegation.md` | 79 | `{DOC_KEYS}` 主代理填充参数 + 禁止自创 + 落盘后自跑闸 |
| `eligibility-judgment/references/judgment-repair.md` | 196 | **一律委派**（含 token 账） |
| `eligibility-judgment/references/qc-delegation.md` | 120 | 闸产物前置自检（含 sha256 比对） |
| `eligibility-judgment/references/reasons-delegation.md` | 60 | 闸产物前置自检（拒工理由：理由贴着结论写） |
| `pdf-image-extractor/SKILL.md` | 379 | 分母口径 + 文本页归集为必做 + 每子任务 6-9 页 + 索引 `mode-selection.md` |
| `pdf-image-extractor/references/ocr-delegation.md` | 85 | 每子任务 1-2 张 → 6-9 张 + 轮/子任务的区分 |
| `pdf-image-extractor/references/mode-selection.md` | 41 | **新增**：`ask_clarification` 参数、三项选项原文、落定映射表、边界情形 |

> `skills/custom/**` 与 SOUL 均为 gitignored；git 跟踪的只有 `tests/skills/**` 与 `docs/**`。

---

## 4. 测试

| 文件 | 用例 | 说明 |
|---|---|---|
| `tests/skills/test_soul_skill_contract.py` | 86 | 行数上限 710→730→750（三次上调各记来历）；证据串新增 `345f2bf4`/`ec37dc7d`/`ec24d087`/`6e5ac7c1`/`69612125` 与 `upstream_issues`/`原地打转`/`假文档`/`splitlines`/`循环论证`/`raw段行号`/`凭空生成`/`collect_text_pages`/`不需要进证据库` |
| `tests/skills/test_check_track_structure.py` | 68 | 闸8/闸9/闸产物/`--show`（新增 29 例） |
| `tests/skills/test_check_judgment_structure.py` | 43 | 闸9 document 键 + 闸产物（新增 7 例） |
| `tests/skills/test_judge_pack.py` | 48 | 跨分片 document 一致性含 CLI `exit 2`（新增 5 例） |
| `tests/skills/test_locate_criteria_sections.py` | 40 | **新增文件**：坐标系、章节定位、`--verify-raw`、`raw段行号` |
| `tests/skills/test_collect_text_pages.py` | 19 | **新增文件**：分母口径、归集、幂等、端到端 |
| `tests/skills/test_pdf_image_extractor_orchestration.py` | 15 | 改写 1 例（旧分母口径 → 新口径 + 归集后转覆盖），修夹具 filename 扩展名 |

**全量**：`cd backend && CI=true PYTHONPATH=. uv run pytest ../tests/skills/ -q --tb=no -p no:randomly`
→ **400 passed / 8 failed**。8 项失败全部是既存的 `test_image_generation.py`
（内网 SSL：`ai-gateway.fosunpharma.com` `WRONG_VERSION_NUMBER`），与本轮无关。

lint：`uvx ruff check --config backend/ruff.toml tests/skills/ skills/custom/` 全绿；
`ruff format --check` 全部已格式化。

**契约测试的两次拦截**（说明它在起作用）：

- 试图把「Phase 2」写进技能 reference → 被「skill 文档不得引用 SOUL 阶段编号」拦下，改为「本技能」；
- 新建 `mode-selection.md` 未被 SKILL.md 索引 → 被 `test_no_orphan_reference_files` 拦下，补上索引。

---

## 5. 遗留与风险

| # | 项 | 状态 |
|---|---|---|
| 1 | `present_files` 不校验文件存在性 | **未修**。属 backend 共享工具（`present_file_tool.py::_normalize`），改动影响所有 agent。已向用户说明修法（加 `if not actual_path.is_file(): raise`），等决定。当前用 SOUL 的「`ls -l` 确认」绕过 |
| 2 | 闸产物自拒工只是**硬化**、不是**强制** | 子代理若不执行前置自检仍可继续；要做到不可能，需 middleware 层改动 |
| 3 | 闸 8 能发现打转，但**逃出去**仍依赖模型正确走 `upstream_issues` 路径 | 设计如此 |
| 4 | token 优化③（欠载轮次合并）靠说服而非机制 | 三项里最不确定 |
| 5 | 预期节省 8.07M 是**估算** | 需下一个完整会话实测验证 |
| 6 | `middleware:summarize` 触发 7 次（每次 ≈55K in / 32K out） | 未调。压缩本身便宜，但丢信息会诱发重读；建议等 ①②③ 落地后看实测再定 |
| 7 | 受影响会话的产物 | `6e5ac7c1` 的 `criteria_parsed_*` 92% 伪造，须从 P2 双轨解析整轨重做（raw.md 完好）；`69612125` 只需跑归集脚本 + 重汇总 `ocr_records.md` + 从 P3 重做（15 个扫描页 `.md` 与病历 13 页完好） |
| 8 | `git commit` | **未提交**。跟踪的改动为 `tests/skills/*.py` 与本文档；`skills/custom/**` 和 SOUL 为 gitignored |

---

## 6. 追加轮次（2026-08-03）：QC 轮次口径修正与上游歧义中性化

> 来源：会话 `afb85bcd`。上一节的 token 优化已在该会话落地，但 Phase 2 双轨 QC 仍未收敛
> ——IN 轨用满 3 轮（阻断 10→4→1），EX 轨只用了 2 轮（4→2）就被 IN 的触顶暂停带停。

### 6.1 上轮 token 优化的落地确认

| 项 | 上轮基线（`69612125`） | 本次（`afb85bcd`） |
|---|---|---|
| OCR 子任务数 | 16 个（1.75 页/个） | **4 个**（病历1-8 / 病历9-13 / 检查1-8 / 检查9-15） |
| 修订执行者 | 主代理亲做（79 read / 15 str_replace） | **4 个修订子代理，主代理零 `str_replace`** |
| `--show` 取条目 | 新增 | 已在用（seq 668/673/686/687） |
| 文本层页归集 | 新增 | 已跑（seq 663「已归集 11 页」，`need=26`） |
| lead / subagent 占比 | 50% / 49% | **13% / 87%**（2.13M / 13.79M）—— 工作确实搬进了子代理 |

### 6.2 实际轮次账

**机械层（结构闸 4 次调用，全部只写 `--workspace --track`，无一次带 `--qc`）**

| seq | 结果 |
|---|---|
| 651 | ⛔ 白跑——用了不存在的 `--criteria`/`--meta`，脚本只打印 usage |
| 657 | 首检：`[IN] 闸9 命中 2/27`、`[EX] 闸4 EX-9 混用 + 闸9 命中 6/26` → `exit 2` |
| 662 | 因闸 9 **整轨重做解析**（IN+EX 各一个 task） |
| 676 / 691 | 修订后 ✅✅ |

后果：闸 7（QC 点名实体仍存在）、闸 8（打转探测）**全程未运行**；
`criteria_qc_history_IN.json` 只有 round 1（那条是修订子代理内部带 `--qc` 写的），
`criteria_qc_history_EX.json` 根本不存在。

**语义层**

| 轨 | R1 | R2 | R3 | 结果 |
|---|---|---|---|---|
| IN | **10 项阻断 / 7 个 ID**（跨 6 种类型） | 4 项 | 1 项（`IN-5-2`） | 触顶 `passed=false` |
| EX | 4 项 | 2 项（`EX-17-1`、`EX-16`） | — | **还有 1 轮预算却被带停** |

收敛轨迹本身健康（IN 10→4→1、EX 4→2），按斜率需 4–5 轮。

### 6.3 六个不收敛原因

1. **首轮问题量超出「复检」预算的设计前提**（主因）：IN R1 的 10 项跨拆分不足/转化条件语义错误/
   分类错误/日期维度缺失/逻辑关系错误/转化条件不完整——这是解析质量整体不达标，
   等于把解析工作推迟到 QC 循环里做。
2. **`upstream_issues` 处置缺一步，白烧 R2 一半配额**：`IN-10-4`/`IN-10-6`（`345f2bf4` 那句丢否定词的
   血小板标准）R1 归 `upstream_issues` → 主代理指示「先不要硬改成确定结论；保持忠实原文，
   不要伪造修复」→ 子代理**正确地**完全不动（result 明确回报「未做伪造性『确定修复』」）→
   但「不动」保留的恰是解析时写下的「两项均无」确定性表达，超出原文 → R2 QC 正确判为
   「上游歧义未隔离/转化条件不可执行」并升级为 blocking，占掉 4 项中的 2 项 → R3 中性化后才回落。
   **缺口在设计里**：三步处置没写「核实完成前已写入的超范围表达该怎么办」。
   另外**第 1 步（回原文核实）一次都没执行**——R1 之后没有任何 `grep`/`read_file`/`view_image`
   碰过 `uploads/试验方案.md`。
3. **闸 8 全程未运行**（见 6.2），振荡在机制上隐形。
4. **修订只做局部修复**：`IN-4` 的 action 要求按可获取性拆成两个实体，子代理只把「适用臂」
   写进转化条件字段，R2 的 finding 明说「实体层面仍未按可获取性拆开」。`IN-11-1` 同样跨 R1/R2。
5. **QC 每轮只报子集**：`EX-16` 自解析起就存在，R1 完全没提，R2 才出现。模板要求第 2/3 轮
   「对比去重，只报未修正或新发现」，但**没有任何要求保证 R1 是穷尽的**。
6. **两轨共享暂停**：「每轨最多 3 轮」按轨计，但暂停是全局的。

### 6.4 本轮改动

**① 结构闸 10 —— `upstream_issues` 点名条目必须已中性化**（`check_track_structure.py`，512 行）

新增模块级常量 `UPSTREAM_PENDING_MARKS = ("待核实","原文歧义","缺否定词","原文缺词","不可判定","待人工确认")`。
判定放宽，满足任一即通过：条目已不存在 / `可从病例获取 is False` / 条目 JSON 任意位置含标记词。
仅「条目仍在 + `可从病例获取` 不为 false + 通篇无标记」判未中性化——精确对应振荡状态。
报错给出三选一动作，并强调**中性化 ≠ 放弃**（仍要回原文核实）。

真实现场验证：

```
① R3 已中性化状态（可从病例获取=False + 备注说明缺否定词）→ [IN] ✅ 结构闸全过  exit=0
② 还原为 R1「不动」状态（可获取 + 确定阈值 + 无标记）    → exit=2
   闸10 `upstream_issues` 点名的条目仍以确定性可执行条件存在（未中性化）：['IN-10-4', 'IN-10-6']
```

**② 中性化写进流程**：`criteria-qc-checklist.md` 第三档由三步改**四步**，新增第 0 步「本轮立即中性化」
（与第 1 步回原文核实**并行**，不是二选一），并写明「放着不动」为什么错 + `afb85bcd` 证据 +
「给修订方的指令必须写**要做什么**，不要只写**不许做什么**」。`criteria-repair.md` 新增
「`upstream_issues` 的中性化改法」专节（三种 `str_replace` 写法）+ 处置手册加一行 +
修订委派模板加明确指令并要求 result 回报中性化方式。

**③ 轮次口径**：`criteria-qc-checklist.md` 顶部与 SOUL 原则 7 都写明——
**「轮次」只计语义 QC**（`task(quality-control)` 返回一次 = 一轮）；结构闸的校验与修复、
闸 9 命中触发的整轨重做，**都不占轮次**，可反复跑到 `exit 0`。
理由：结构问题客观、有机械判据、修复不消耗语义判断力，计入轮次等于用语义预算买结构收敛。

**④ 复检必须带 `--qc`**：`criteria-repair.md` 第 6 步给出完整命令并写明「`--qc` 不是可选项」——
不带则闸 7/8/10 一个都不运行；附 `afb85bcd` 的 4 次漏传证据，并列出全部合法参数
（`--workspace`/`--track`/`--qc`/`--snapshot`/`--json`/`--show`）以防再自创 `--criteria`/`--meta`。

**⑤ 解析子代理前置感知结构规则 + 落盘后自跑结构闸**（本轮预期收益最大）：
`parse-delegation.md` 新增**结构规则速查 5 条**（与机械判据一一对应：条件ID 唯一+前缀一致 /
子序号不混用且连续 / `描述索引` 与实体双向对应 / 原条号全覆盖 / `原文` 在 raw.md 中逐字可查）
与**落盘后自跑结构闸到 `exit 0` 才返回**（`exit 2` 自行 `str_replace` 修，修不过如实回传 problems）。
两个模板的输出段各加一行提醒。SKILL.md 与 SOUL Phase 2 派发处同步。
理由写入：结构问题在解析子代理的上下文里最好修（刚写完、知道每条来历），
交回主代理发现的代价是整轨重做一个子任务。

**⑥ IN/EX 轮次独立**：SOUL 原则 7 的触顶处置由「**全局暂停**（不论是哪一轨）」改为
「**冻结该轨**，另一轨继续用完自己的 3 轮，**两轨都定局后**才发**一次** `ask_clarification`」；
`phase2_summary.json` 按轨写 `criteria_qc_status_{IN|EX}`。
**Phase 3 屏障不放松**：仍须两轨都 `passed=true` 才可推进（标准是全流程唯一判定依据）。
Phase 2 循环的终止条件与 SKILL.md 并行性说明同步。

**⑦ 修掉四处过期/矛盾表述**：SOUL 原则 7「语义修订只能由主代理 LLM 逐条完成」→「一律委派」；
SOUL Phase 2「执行者二选一——主代理亲做」→「一律派修订子代理」；SOUL 槽位说明与
`criteria-repair.md` 并行性段里的「主代理亲做修订时…」；`criteria-parser/SKILL.md` 同类表述；
QC 委派模板「修订由主代理做」→「由专门的修订子代理做」；第一层「8 个闸」→「10 个闸」。
全库检索 `语义修订只能由主代理|主代理亲做修订|修订由主代理做|主代理逐条完成` 已无残留。

### 6.5 文件与测试

| 文件 | 行数 | 变更 |
|---|---|---|
| `criteria-parser/scripts/check_track_structure.py` | 512 | 闸 10 + `UPSTREAM_PENDING_MARKS` + docstring |
| `criteria-parser/references/criteria-qc-checklist.md` | 236 | 第三档四步 + 轮次口径 + 按轨独立 + 10 个闸 + 模板措辞 |
| `criteria-parser/references/criteria-repair.md` | 374 | 中性化专节 + 处置手册 + `--qc` 硬要求 + 模板 upstream 指令 |
| `criteria-parser/references/parse-delegation.md` | 163 | 结构规则速查 5 条 + 落盘后自跑结构闸 |
| `criteria-parser/SKILL.md` | 410 | 一律委派 + 轮次按轨独立 + 解析自检前移 |
| SOUL | 733 | 原则 7 轮次口径/`--qc`/按轨触顶处置；Phase 2 循环与派发；四处过期表述 |

测试：`test_check_track_structure.py` **77 passed**（新增 9 例覆盖闸 10：未中性化命中 /
降级通过 / 备注标记通过 / 标记在任意字段通过 / 条目已删通过 / 部分中性化只报未处理的 /
`upstream_issues` 为空跳过 / 无 `--qc` 跳过 / CLI `exit 2`）；`test_soul_skill_contract.py` **90 passed**
（证据串新增 `afb85bcd` 与 `中性化`/`只计语义 QC`/`按轨独立`）。
全量 `tests/skills/` **409 passed / 8 failed**（8 项为既存 `test_image_generation` 内网 SSL）。
SOUL 733 行，**未上调 750 上限**。

### 6.6 本轮未解决

| # | 项 | 说明 |
|---|---|---|
| 1 | 闸 10 只止损，不解决根本 | 那个丢失的否定词仍需 `view_image` 看 PDF 原页才能定案。`afb85bcd` 里第三档第 1 步一次都没执行；本轮把「回原文核实」与「中性化」写成并行两件事并加了「第 1 步是必做」的强调，但**能否执行仍依赖模型遵守**——没有机械闸能强制「你去看过原页了」 |
| 2 | 原因 ①（首轮问题量过大） | 靠 ⑤ 间接缓解（结构问题前移、不占轮次），但**语义**质量仍取决于解析子代理。计划里的「R1 要求穷尽 + 回报已检 N/N 条」本轮未做 |
| 3 | 原因 ④（局部修复） | 未加机制。可考虑让 QC 在复检时对上一轮 action 做「是否照做」的二元判定 |
| 4 | 原因 ⑤（QC 每轮只报子集） | 未加机制，是「N 轮无法保证归零」的数学根源 |
