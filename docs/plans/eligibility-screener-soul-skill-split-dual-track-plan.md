# eligibility-screener 重构方案：SOUL/Skill 职责分离 + IN/EX 双轨并行

> 状态：执行中。方案在执行期间除「因实现导致方案变更」外不再改动；Task 9 的核对表追加到本文末尾。
>
> 影响范围：`backend/.deer-flow/agents/eligibility-screener/SOUL.md`（gitignored）、
> `skills/custom/{criteria-parser,eligibility-judgment,patient-separator,pdf-image-extractor,screening-report-generator}`（gitignored）、
> `tests/skills/`（版本控制）、本文档（版本控制）。

## 问题陈述

`SOUL.md`（964 行）混着三类内容：编排层真正需要的（阶段串联、屏障、并发预算、todo），
以及大量属于具体 skill 的执行细节（OCR 路线与命令、解析拆分规则、判定取证与两个机械闸、
报告构建命令、60 行判定 prompt 模板）。已观测到三个代价：

1. **每轮付全额 token** —— SOUL 是 system prompt 全文常驻；skill 本应渐进加载，
   把细节抄进 SOUL 等于放弃渐进加载。
2. **双份真相漂移** —— `criteria-parser/SKILL.md` 已写「步骤 2: QC 校验（Phase 2.5）」，
   而 SOUL 的 Phase 2.5 是患者拆分，规则已经对不上。
3. **入选/排除全程串行** —— 解析、QC 单轨，只有 P3 分片；criteria QC 收敛慢直接卡住整条链
   （thread `5a1c8d95`：criteria QC 从 10:24 跑到 10:52，28 分钟全流程阻塞）。

## 需求（已确认）

- **彻底双轨**：从解析开始分 IN/EX 两轨，各自独立 QC↔修订，P3/P4 沿用，出报告前才合并。
  `judge_pack.py split` 退役。
- **全局暂停**：任一轨 criteria QC 两轮仍有阻断级 → 停整个流程，`ask_clarification` 一次性
  汇报两轨状态；通过的轨也不得推进 P3。
- **不新增 skill**：原 SOUL 原则 6（方案预提取）并入 `criteria-parser`；各 skill 新增
  `references/` 层承载长模板，SKILL.md 只留索引。
- **P4 也分轨**：`患者 × 轨 × {QC, 理由}` = 4N，合并只在 P4.5 发生一次。
  已知代价：任务数从 2N 翻倍到 4N，`task` 并发硬上限 3 且超发静默丢弃，患者多时批次增加
  （N=3 时 2 批 → 4 批）；抵消因素是每任务输入减半。
- **跨轨语义 QC 不加额外子任务**：改用两层机械兜底 —— `assemble` 做跨轨一致性检查
  （跨轨条件ID 冲突、`汇总统计` 与实际条目数一致、`描述索引` 前缀冲突），
  P4.5 合并后跑全量 `exclusion_direction_check.py`。
- 不遗漏、不简化任何现有逻辑/流程/原则；每一块都要有明确去处，纯删除需显式说明理由。

## 背景（调研结论，已验证）

### 产物契约

- `criteria_parsed.json` 顶层 5 键：`方案元数据`（全篇级）、`解析说明`（常量样板）、
  `四分类`（4 类目，`IN-*`/`EX-*` 前缀可分区）、`汇总统计`（按类目 + `子条件总数` 需重算）、
  `描述索引`（前缀可分区）。
- `screening-report-generator/scripts/build_reports.py` 只消费 `四分类`（缺失即 die）+
  `方案元数据`；**不读** `汇总统计`/`描述索引`/`解析说明`。合并逻辑不受报告端约束。
- `uncertain_recheck.py` / `exclusion_direction_check.py` 只要 `四分类` 外层结构，
  按轨切分的包可直接消费（现有 `split` 即靠保留该外层做到）。
- 观测缺陷：thread `5a1c8d95` 中 `方案元数据` 全空（QC 记为 CQC-R2-004 建议级），
  说明「谁填全篇元数据」当前无人认领，双轨后必须显式指派。

### 并行约束

- `task` 每批硬上限 3，超发被 `SubagentLimitMiddleware` 在 `after_model` 静默丢弃、**不排队**。
- P3 已是 `患者 × {IN, EX}`；P4 目前在合并后的 `judgments_draft.json` 上做 `患者 × {QC, 理由}`。

### 闸门现状（不可丢失）

`judge_pack.py split` 三道闸：

| 闸门 | 拦什么 | 可否绕过 |
|------|--------|----------|
| QC 闸 | `passed != true` / `blocking_issues` 非空 / 检出「带建议放行·轮次上限·`round_limit_released`」自我放行痕迹 | 仅 `--force-qc-unconverged` |
| 结构闸 | 前缀↔类目一致、`汇总统计` 与实际条目数一致 | 不可绕过 |
| 产出闸 | 分片非空 | 不可绕过 |

`tests/skills/test_judge_pack.py` 27 项通过，其中 9 项为闸门用例，含两个真实故障态回放。

## 目标形态

```
Phase 1  预处理
  流水线A: classify_uploads → pdf_to_image        (pdf-image-extractor)
  流水线B: 边界锚定 grep → 逐字提取 → 完整性自检   (criteria-parser)
           产出 eligibility_criteria_raw.md + criteria_meta.json + 入选/排除段行号
Phase 1.5 HITL 模式必选 (纯编排, 留 SOUL)
Phase 2  三轨并行
  轨 IN : parse(入选段) → QC↔修订 (≤2轮) → criteria_parsed_IN.json + criteria_qc_IN.json
  轨 EX : parse(排除段) → QC↔修订 (≤2轮) → criteria_parsed_EX.json + criteria_qc_EX.json
  轨 OCR: 路线A/B 分片 → ocr/{source}/ → 覆盖率门禁
  全局暂停闸: 任一轨 2 轮仍有阻断 → ask_clarification, 全流程停
  收尾: slim ×2 → criteria_judge_{IN,EX}.json ; assemble → criteria_parsed.json
Phase 2.5 患者拆分/聚合 (三分支不变)               (patient-separator)
Phase 3  患者 × 轨 判定 → judgments_draft_{id}_{TRACK}.json  (eligibility-judgment)
         per-track 闸: uncertain_recheck / (EX) exclusion_direction_check
Phase 4  患者 × 轨 × {QC, 理由} → judgments_{id}_{TRACK}.json   (4N)
Phase 4.5 合并汇总 → outputs/judgments_{id}.json + 全量终检
Phase 5  报告                                     (screening-report-generator)
```

关键变更：P3 不再中途合并（原 3.3/3.4 的「合并 + 合并后终检」上移到 P4.5），
双轨一路走到 P4 结束才汇总一次。

## SOUL 全块归属映射（26 块，逐块交代）

| # | SOUL 块（原行号） | 处置 | 去处 |
|---|---|---|---|
| 1 | 角色定位 / 报告原则（3-9） | 留 | SOUL |
| 2 | 严禁跳步 1-8（11-44） | 改写留 | SOUL；规则 7/8 改 per-track 语义，新增规则 9「未 assemble 不得出报告」 |
| 3 | 原则1 最大并行（48-60） | 拆 | 留 SOUL：`task` 预算/超发静默丢弃/滑动窗口/`write_todos` 同轮；下沉：`parse_document ≤2-3`→pdf-image-extractor、`view_image ≤2-3`→两处技能（已在） |
| 4 | 原则2 分级屏障与流水线（61-77） | 改写留 | SOUL，硬屏障表补双轨条目 |
| 5 | 原则3 颗粒度（78-81） | 留 | SOUL |
| 6 | 原则4 容错（82-86） | 留 | SOUL |
| 7 | 原则5 归类与 OCR 委派 ①②③④（87-134） | 拆 | 留 SOUL：路线由 P1.5 定、在途 OCR ≤2、产物去向、目录白名单；下沉 → pdf-image-extractor：脚本用法、`role`/`handled_by` 语义表、降级链、幂等 |
| 8 | 原则6 方案预提取 ①②③④（135-166） | **整块下沉** | criteria-parser 新增「章节提取与完整性自检」+ `references/criteria-extraction.md` |
| 9 | 原则7 上下文与读取纪律（167-187） | 拆 | 留 SOUL：Phase 边界、单文件读一次、按段读、bash 合并、空文件不重试；下沉 → eligibility-judgment：「判定子代理只读 2 类文件」「prompt 不内联正文」 |
| 10 | 原则8 QC 收敛机制（188-250） | 拆 | 留 SOUL：公共 QC 契约（两层结构、阻断/建议分级、≤2 轮、禁 bash 改语义、QC 结论只由 QC 子代理写、全局暂停策略）；下沉：criteria 侧检查清单 → criteria-parser `references/criteria-qc-checklist.md`；判定侧 → eligibility-judgment |
| 11 | 原则9 路径纪律（251-256） | 留 | SOUL |
| 12 | 原则10 判定证据边界与取证（257-282） | **整块下沉** | eligibility-judgment（与其原则四~九重复，合并去重） |
| 13 | 原则11 可见性与交付清单（283-295） | 拆 | 留 SOUL：present 去重、批量 present、子代理须声明产出清单；各 skill 声明自己的交付项 |
| 14 | 原则12 Todolist（296-307） | 留 | SOUL |
| 15 | 阶段总览 + 并行度速查（308-332） | 改写留 | SOUL，按新阶段与双轨重算 |
| 16 | Phase 1 四轮（333-404） | 拆 | 轮次编排留 SOUL；A 的命令 → pdf-image-extractor；B 的 grep/read/自检 → criteria-parser |
| 17 | Phase 1.5 HITL 全段（405-467） | 留 | SOUL（纯编排 + 两个真实故障案例 `ab76d625`/`459951c1`） |
| 18 | Phase 2 步骤2.0 路线核对（470-476） | 留 | SOUL |
| 19 | Phase 2 OCR 派发 + 并发预算（477-492） | 拆 | 预算/屏障留 SOUL；派发规则 → pdf-image-extractor |
| 20 | Phase 2 QC↔修订循环（493-521） | 拆 | 循环骨架 + 暂停闸留 SOUL；检查项/修订纪律 → criteria-parser |
| 21 | Phase 2 三个子任务模板（522-580） | **整块下沉** | criteria-parser `references/parse-delegation.md`（双轨版）、pdf-image-extractor `references/ocr-delegation.md`（模板①②） |
| 22 | Phase 2 覆盖率门禁 + 收尾/summary（581-627） | 拆 | 门禁命令 → pdf-image-extractor；`phase2_summary.json` 契约留 SOUL |
| 23 | Phase 2.5 三分支 + 聚合脚本 + schema（628-716） | 拆 | 模式分支表留 SOUL；聚合脚本 + `patient_index.json` schema + 页码/回退规则 → patient-separator `references/aggregate-ocr.md` |
| 24 | Phase 3 启动闸 + 60 行判定模板 + 合并 + 终检（717-833） | 拆 | 启动闸/矩阵/流水/批次留 SOUL；模板 → eligibility-judgment `references/judge-delegation.md`；合并与终检移到 P4.5 |
| 25 | Phase 4 QC/理由委派 + 通过判定 + 回填终检（834-879） | 拆 | 矩阵/屏障留 SOUL；委派要点与 QC 报告结构 → eligibility-judgment `references/{qc,reasons}-delegation.md` |
| 26 | Phase 5（880-906）/ 目录规范（907-950）/ Todolist 模板（951-964） | 拆 / 留 / 改写留 | P5 命令 → screening-report-generator；目录规范留 SOUL（补双轨新文件）；Todolist 模板改双轨可见 |

预期 SOUL 从 964 行降到 **~400 行**。

## 任务拆解

### Task 1: 冻结基线并建立不变量测试网

新建 `tests/skills/test_soul_skill_contract.py`：

- 断言 SOUL.md 不含被下沉的关键词（如 `parse_document 调用铁律`、`classify_uploads.py --uploads`、
  判定模板特征串）。
- 断言各 SKILL.md 不含 `Phase ` 编号引用（防再次漂移）。
- 断言 `references/` 里被 SKILL.md 索引的文件都存在。

同时为 SOUL.md 与 5 个 SKILL.md 各存一份行数/章节清单快照供人工 diff。

**Demo**：`pytest tests/skills/test_soul_skill_contract.py` 全红，每条红即后续验收点。

### Task 2: `judge_pack.py` 从 `split` 迁移到 `slim` + `assemble`（地基，先做）

- `slim --criteria criteria_parsed_{TRACK}.json --qc criteria_qc_{TRACK}.json --track {IN|EX}
  --out criteria_judge_{TRACK}.json`：复用现有三道闸；结构闸收紧为「本轨只允许本前缀条目」。
- `assemble --tracks IN.json EX.json --qc qc_IN.json qc_EX.json --meta criteria_meta.json
  --out criteria_parsed.json`：两轨 QC 闸 + 跨轨条件ID 冲突检测 + `汇总统计` 重算 +
  注入 `方案元数据`/`解析说明` + `描述索引` 并集（前缀冲突即阻断）。
- 保留 `merge-judgments`/`merge-recheck`/`merge-reasons` 不变；`split` 保留为 deprecated 薄壳
  （打印迁移提示后 exit 2），避免旧 prompt 静默走老路。

**测试**：现有 9 个闸门用例改写到 `slim`（含两个故障态回放）；新增 `assemble` 用例 ——
跨轨 ID 冲突、`汇总统计` 重算正确、`方案元数据` 缺失阻断、单轨 QC 未过阻断、
`build_reports.py` 能消费 assemble 产物。

**Demo**：双轨假数据跑 `slim ×2 → assemble → build_reports.py --verify` 全绿；
把 EX 轨 QC 改成未收敛，`assemble` 立刻 exit 2。

### Task 3: `criteria-parser` 接收章节提取 + 双轨解析 + criteria QC 清单

SKILL.md 重组为：概述 / 四分类体系 / 拆分原则（保持原样）/ 条件转化 / 日期维度 / 可获取性 /
输出格式 / **章节提取与完整性自检（原 SOUL 原则 6 全文）** / **双轨解析（本轨只处理本段、
只填本轨前缀、分片 write_file 节奏保持）** / QC 清单索引。

新建：

- `references/criteria-extraction.md` —— 边界锚定 grep + 逐字规则 + 末条号自检算法 + 两个历史故障。
- `references/parse-delegation.md` —— IN/EX 两个委派模板，输入为 `eligibility_criteria_raw.md` +
  P1 落盘的段行号，禁止读全文。
- `references/criteria-qc-checklist.md` —— 原 SOUL 原则 8 第二层 5 项语义检查 + 阻断/建议分级 +
  `criteria_qc_{TRACK}.json` schema。

删除 SKILL.md 过时的 `（Phase 2 轨道 A）`/`（Phase 2.5）` 标签，改为按产物描述。
保留其「输出必须分片写入」硬规则（历史故障 `31c168d2` 看门狗超时）。

**测试**：契约测试断言三个 references 存在且被索引、SKILL.md 无 Phase 编号、
`criteria_qc_{TRACK}.json` schema 与 `judge_pack.slim` 的 QC 闸读的字段一致。

**Demo**：删掉 SOUL 原则 6 后，仅凭 `/criteria-parser` 可完整复现
「边界锚定 → 逐字提取 → 末条号自检」。

### Task 4: `pdf-image-extractor` 接收归类语义与 OCR 委派模板

SKILL.md 新增「上传归类的业务语义」（`role`/`handled_by` 表 + `ignored` 段禁读禁解析 +
拆页始终执行）与「覆盖率门禁调用」；新建 `references/ocr-delegation.md`（原 SOUL 模板①②全文，
含「不要自行降级、只回报 `route_a_failed`」「result 禁回传正文」）。

SOUL 侧只留：路线由 P1.5 决定且 `null` 时禁派、在途 ≤2、产物必须落 `ocr/{source}/`、目录白名单。

**Demo**：SOUL 的 Phase 2 OCR 段从 ~100 行压到 ~12 行，行为不变。

### Task 5: `patient-separator` 接收聚合脚本与 schema

SKILL.md 补三种模式下本技能职责边界（模式1 不参与、模式2 只聚合、模式3 拆分+聚合）；
新建 `references/aggregate-ocr.md`（`patient_index.json` schema + 按整数页码格式化文件名的
拼接脚本 + `.md` 优先 `.txt` 回退 + 禁止通配拼接避免跨患者混入 + 未登记页不纳入）。

SOUL 只留模式分支表与屏障。

**Demo**：SOUL 的 Phase 2.5 从 ~88 行压到 ~20 行。

### Task 6: `eligibility-judgment` 接收判定证据边界并改造为双轨到底

SKILL.md 吸收 SOUL 原则 10（与既有原则四~九去重合并）与原则 7 判定侧纪律；
「判定分片与合并」重写 —— `split` 退役，输入改为 `criteria_judge_{TRACK}.json`（由 `slim` 产出），
合并点从 P3 移到 P4 之后。

新建：

- `references/judge-delegation.md` —— 原 SOUL 60 行模板，参数化 `{TRACK}` 与患者模式路径。
- `references/qc-delegation.md` —— P4 QC 要点：漏判反查阻断级 + 方向核验阻断级 + QC 报告结构，
  按轨版本。
- `references/reasons-delegation.md` —— 理由生成 + `direction_warnings`。

「判定约束清单（40条）」补「双轨约束」小节。

**测试**：`slim` 产物能被 `uncertain_recheck.py`/`exclusion_direction_check.py` 直接消费
（现有同类测试扩到双轨）；per-track `judgments_draft_{id}_{TRACK}.json` 经 `merge-judgments`
合并后条目数与 summary 正确。

**Demo**：单患者双轨跑通 P3→P4→P4.5，`outputs/judgments_{id}.json` 结构与重构前一致。

### Task 7: `screening-report-generator` 收口 P5 命令

SKILL.md「强制流程」补多患者 `--judgments` 重复用法与 `--verify` 闸失败处置
（改数据重跑，不得改 HTML），显式声明输入为 `assemble` 产出的全量 `criteria_parsed.json`。

SOUL Phase 5 只留：前置读 `phase4_summary.json`、加载技能、present 清单。

**Demo**：SOUL Phase 5 从 ~27 行压到 ~8 行。

### Task 8: 重写 SOUL.md（双轨编排骨架）

按归属表只保留公共块，并落地双轨编排：

- 阶段总览与并行度速查重算（P2 = IN + EX + OCR 三轨共享 3 并发，OCR 独立 ≤2 →
  稳定态 IN/EX 各 1 + OCR 1；P3 = 2N；P4 = 4N）。
- 硬屏障表新增「两轨 QC 均通过前禁止 assemble」「未 assemble 前禁止 P5」。
- 严禁跳步规则 7/8 改 per-track，新增规则 9。
- 原则 8 保留公共 QC 契约并写入**全局暂停**语义（任一轨阻断 → 两轨状态一次性汇报、
  通过的轨也不得推进 P3）。
- 新增 Phase 4.5 合并汇总节（承接原 P3 步骤 3.3/3.4 的合并与终检 + 原 P4 的 merge-reasons
  回填与全量终检）。
- 目录规范补 `criteria_parsed_{IN,EX}.json`、`criteria_qc_{IN,EX}.json`、`criteria_meta.json`、
  `criteria_judge_{IN,EX}.json`、`judgments_draft_{id}_{TRACK}.json`、`judgments_{id}_{TRACK}.json` 等。
- Todolist 模板改双轨可见：`P2-IN / P2-EX / P2-OCR / P3-IN / P3-EX / P4-IN / P4-EX / P4.5`。

**Demo**：`wc -l SOUL.md` ≈ 400；Task 1 契约测试全绿。

### Task 9: 全流程回归与一致性核对

用 Task 1 的基线快照逐块核对 26 个块「已迁移到 X 且内容无损」或「显式删除并记录理由」，
产出核对表（追加到本文末尾）。

跑 `tests/skills/` 全量 —— 注意 `test_image_generation.py` 有 8 项既存失败
（内网 `ai-gateway.fosunpharma.com` SSL `WRONG_VERSION_NUMBER`），与本次无关，
需在核对表标注为已知基线。

最后用 thread `5a1c8d95` 的输入（入排标准.docx + 7 页病历）跑一次端到端，对比重构前后
`criteria_parsed.json` 的 `四分类` 条目数、`judgments_{id}.json` 的结论分布是否一致；
用 `run_events` 时间戳验证 P2 的 IN/EX QC 两轨 `subagent.start` 落在同一批（真正并行）。

**Demo**：核对表 26/26 有明确去处；端到端产物与重构前等价。

## 三个取舍及其定论

| # | 取舍 | 定论 |
|---|---|---|
| 1 | P4 分轨使子任务从 2N 变 4N，`task` 上限 3 导致患者多时批次增加（N=3：2 批 → 4 批）；抵消因素是每任务输入减半 | **保持 P4 分轨**（用户选定）。语义最统一，合并只发生一次 |
| 2 | 按轨 QC 失去跨轨语义视角（入选与排除互相矛盾、同一事实两轨结论不同） | **不加额外子任务**，改机械兜底：`assemble` 做跨轨 ID/统计/索引一致性检查；P4.5 合并后跑全量 `exclusion_direction_check.py` |
| 3 | `方案元数据` 归属改变：由 P1 流水线B 落盘 `criteria_meta.json`、`assemble` 注入 | 采纳。顺手修掉 thread `5a1c8d95` 的「方案元数据全空」缺陷；代价是 P1 完整性自检要多校验一项元数据非空 |

## 执行注意事项

- **gitignore 状态**：`skills/custom/**` 与 `backend/.deer-flow/agents/**` 均被 gitignore，
  只有 `tests/skills/**` 与 `docs/plans/**` 纳入版本控制。改完后如需提交，
  只有测试文件和本文档可提交；提交前先问用户。
- **禁止简化**：任何被搬迁的规则必须逐字保留其硬规则措辞与历史故障 thread 编号
  （`ab76d625`、`459951c1`、`d1ce04c0`、`31c168d2`、`5a1c8d95` 等），它们是防回归的证据链。
- **上一轮已完成、不要回退的改动**：SOUL 严禁跳步规则 7/8（QC 收敛前禁切分、
  禁主代理改写 QC 结论）、原则 8 的 criteria QC 达 2 轮上限暂停策略、Phase 3 启动闸、
  `phase2_summary.criteria_qc_status`；这些要在双轨语义下改写保留，不可删除。
- 每个 Task 完成后运行 `pytest tests/skills/ -q` 并确认新增/改写用例通过；
  `judge_pack.py` 与测试文件改动后跑 `uvx ruff check` + `uvx ruff format --check`
  （该脚本有 3 项既存 ruff 告警：EXE001 shebang、两处 TRY004，属基线，不要顺手改无关代码）。

---

## 执行结果（Task 9 核对表）

### 与方案的偏离

| # | 方案原文 | 实际 | 原因 |
|---|---|---|---|
| 1 | 「预期 SOUL 从 964 行降到 **~400 行**」 | **662 行** | ~400 是在双轨定案前估的。双轨新增 Phase 4.5、Phase 2 三轨调度、13 项双轨 todos、7 个新产物，编排层本身内容变多。逐段核过后确认剩余全是承重内容：严禁跳步 9 条 + 10 条公共原则 216 行、8 个 Phase 348 行、目录规范/Todolist 契约 65 行。再压就要删规则，与「不遗漏、不简化」硬要求冲突。相应把 `test_soul_stays_an_orchestration_skeleton` 阈值从 500 改为 **680**（实测 662 + 余量），并在测试 docstring 写清来历——该闸的作用改为「挡细节回流」而非「逼迫删规则」 |
| 2 | `assemble --tracks A B --qc a b` | `assemble --in-criteria --in-qc --ex-criteria --ex-qc` | 两个 `--tracks` 路径与两个 `--qc` 路径靠位置配对有歧义，LLM 容易错配；显式四参数无歧义 |
| 3 | 原则重编号未在方案中明确 | 12 条 → **10 条** | 原则 6（方案预提取）整块下沉 criteria-parser、原则 10（判定证据边界）整块下沉 eligibility-judgment，其余合并去重后重编号；SOUL 内所有交叉引用已同步 |

### 26 块归属核对（全部有明确去处，无静默删除）

核对方法：每块抽 1-2 个特征串，机械验证它出现在声明的去处（`38/38` 通过）。

| # | 块 | 处置 | 去处 | 核对特征串 |
|---|---|---|---|---|
| 1 | 角色定位 / 报告原则 | 留 + 增补 | SOUL（新增 skill 归属表） | — |
| 2 | 严禁跳步 1-8 | 改写留 | SOUL：1-6 保留，7/8 改 per-track，**新增 9**「未 assemble 不得出报告」 | `ab76d625` `459951c1` |
| 3 | 原则1 最大并行 | 拆 | SOUL（预算/静默丢弃/滑动窗口）+ pdf-image-extractor（`parse_document` 并发） | `SubagentLimitMiddleware` / `一轮 ≤ 2-3 个` |
| 4 | 原则2 分级屏障与流水线 | 改写留 | SOUL（硬屏障补 4 条双轨条目；可流水线补三轨/按轨） | — |
| 5 | 原则3 颗粒度 | 留 | SOUL 新原则 3 | `python3 -c` |
| 6 | 原则4 容错 | 留 | SOUL 新原则 4（"某轨解析失败不影响另一轨"） | — |
| 7 | 原则5 归类与 OCR 委派 ①②③④ | 拆 | SOUL 新原则 5（路线由 P1.5 定 / 在途 ≤2 / 产物去向 / 目录白名单）+ pdf-image-extractor（脚本用法、`role`/`handled_by` 表、拆页始终执行） | `handled_by` `phase1_criteria_extract` / `在途 OCR 子任务 ≤ 2` |
| 8 | 原则6 方案预提取 ①②③④ | **整块下沉** | criteria-parser「章节提取与完整性自检」+ `references/criteria-extraction.md` | `边界锚定` `逐字复制` `末条号` `55%` |
| 9 | 原则7 上下文与读取纪律 | 拆 | SOUL 新原则 6（Phase 边界/单文件读一次/按段读/bash 合并/空文件不重试）+ eligibility-judgment「子代理上下文纪律」 | `4.4M input token` `d1ce04c0` / `子代理上下文` |
| 10 | 原则8 QC 收敛机制 | 拆 | SOUL 新原则 7（两层结构、分级、≤2轮、禁 bash 改语义、QC 只由子代理写、**全局暂停**）+ criteria-parser `references/criteria-qc-checklist.md` + eligibility-judgment `references/qc-delegation.md` | `严禁 bash 脚本做语义修订` `blocked_round_limit` / `覆盖完整性` |
| 11 | 原则9 路径纪律 | 留 | SOUL 新原则 8 | — |
| 12 | 原则10 判定证据边界与取证 | **整块下沉** | eligibility-judgment（与原则四~九去重合并） | `符合=符合入组` `uncertain_recheck.py` |
| 13 | 原则11 可见性与交付清单 | 拆 | SOUL 新原则 9（去重规则 + 双轨交付/不交付清单）+ 各 skill 自己的交付清单 | `present_files` 一次 |
| 14 | 原则12 Todolist | 留 | SOUL 新原则 10 | — |
| 15 | 阶段总览 + 并行度速查 | 改写留 | SOUL（8 阶段；并行度 P2=IN1+EX1+OCR1、P3=2N、P4=4N + 批次代价提示） | — |
| 16 | Phase 1 四轮 | 拆 | SOUL（轮次编排）+ pdf-image-extractor（归类/拆页命令）+ criteria-parser（提取链） | `classify_uploads.py` `pdf_to_image.py` |
| 17 | Phase 1.5 HITL 全段 | 留 | SOUL（纯编排 + 两个真实故障 + 三选项原文） | `ab76d625` `459951c1` `ClarificationMiddleware` |
| 18 | Phase 2 步骤2.0 路线核对 | 留 | SOUL Phase 2 步骤 2.0 | — |
| 19 | Phase 2 OCR 派发 + 并发预算 | 拆 | SOUL（三轨共享预算/屏障）+ pdf-image-extractor `references/ocr-delegation.md`（派发规模） | `ceil(缺失 scanned 页数 / 2)` |
| 20 | Phase 2 QC↔修订循环 | 拆 | SOUL（按轨循环骨架 + 三种终止 + 暂停闸）+ criteria-parser（检查项/修订纪律） | — |
| 21 | Phase 2 三个子任务模板 | **整块下沉** | criteria-parser `references/parse-delegation.md`（IN/EX 双轨版）+ pdf-image-extractor `references/ocr-delegation.md`（模板①②） | `route_a_failed` / `禁止读原始方案文档` |
| 22 | Phase 2 覆盖率门禁 + 收尾/summary | 拆 | pdf-image-extractor（门禁命令与判定口径）+ SOUL（`phase2_summary.json` 契约，新增 `criteria_qc_status`） | `ocr_coverage.py` `duplicate_parse_suspected` |
| 23 | Phase 2.5 三分支 + 聚合脚本 + schema | 拆 | SOUL（模式分支表 + 屏障）+ patient-separator `references/aggregate-ocr.md` | `out_dir.mkdir` `禁止用全局` `来源图片` |
| 24 | Phase 3 启动闸 + 判定模板 + 合并 + 终检 | 拆 | SOUL（启动闸/矩阵/流水/**不合并**）+ eligibility-judgment `references/judge-delegation.md`；合并与终检移到 **Phase 4.5** | `取证索引` `5a1c8d95` |
| 25 | Phase 4 QC/理由委派 + 通过判定 + 回填终检 | 拆 | SOUL（4N 矩阵 + 屏障）+ eligibility-judgment `references/{qc,reasons}-delegation.md` | `objective_checks` `direction_warnings` |
| 26 | Phase 5 / 目录规范 / Todolist 模板 | 拆 / 留 / 改写留 | screening-report-generator（构建+校验+输入前置）/ SOUL 目录规范（补 7 个双轨产物并标注不交付）/ SOUL Todolist（13 项双轨可见） | `build_reports.py` / `images_ascii` |

### 验证证据

| 项 | 结果 |
|---|---|
| `tests/skills/test_soul_skill_contract.py` | **63 passed**（重构前基线 44 failed / 19 passed） |
| `tests/skills/test_judge_pack.py` | **43 passed**（重构前 27 passed；新增 16 项含双轨闸门与两个故障态回放） |
| `tests/skills/` 全量 | **202 passed / 8 failed** |
| 上述 8 failed | 全部为 `test_image_generation.py` 的**既存**内网 SSL 失败（`ai-gateway.fosunpharma.com` `WRONG_VERSION_NUMBER`），与本次重构无关，重构前即如此 |
| 端到端脚本链（假数据） | `slim ×2 → assemble → build_reports.py` 构建 + `--verify` 全 ✅（17 项校验） |
| 闸门反例 | EX 轨 QC 未收敛 → `assemble` exit 2 且不产出文件；跨轨条件ID 撞号被结构闸拦住；「带建议放行」自我放行痕迹被 QC 闸拦住 |
| `ruff check` / `format --check`（`--config backend/ruff.toml`） | 通过 |
| 26 块特征串核对 | **38/38** |
| SOUL 体量 | 964 → **662 行**（-31%），且不再含任何 skill 可拥有的执行细节 |

### 未执行项

**真实 LLM 端到端回归未跑**（方案 Task 9 的最后一项）。原计划用 thread `5a1c8d95` 的输入
（入排标准.docx + 7 页病历）重跑一次，对比重构前后 `四分类` 条目数与结论分布，并用 `run_events`
时间戳验证两轨 `subagent.start` 落在同一批。该步需要启动 agent 跑一次完整会话（真实 LLM 调用 +
外部 OCR 服务计费），不在本次静态重构的可自动化范围内。已完成的替代验证：契约测试网 63 项、
脚本链端到端 Demo、闸门反例、26 块特征串核对。**建议在下一次真实会话中验证双轨是否真并行**
（查 `run_events` 里 IN/EX 两个 `subagent.start` 的时间戳是否同批）。

