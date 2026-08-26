# 解析域 render 模式 + SOUL P2 下沉 设计

日期:2026-08-26 · 状态:待确认

## 背景与问题

881e7ba8 实测(19 个 task 全程还原):

1. **主代理手写 19 份委派 prompt,共 ~19k 字符**(解析 1.2-1.4k / QC 0.9-1.0k / 修订 1.2-1.4k),
   每份都占 lead 输出 token;QC/修订里的**轮次号、闸 total 数字、点名条目清单**全靠主代理手填,
   填错即误导子代理(R2 QC prompt 里写「total=28」vs「total=27」,两轨数字不同步就出错)。
2. **每个子代理被要求自读 34KB parsing-rules.md + schema_example + repair/checklist**——
   EX 重做任务前 4 步花在学规则(~200k token),到写产物时上下文耗尽 → 27 实体只精拆 2 个、
   17 条「待人工判定」占位符(拆分倒退的直接放大因素)。
3. **手写变体即质量漂移点**:重做任务的 prompt(903 字符)没继承 parse-delegation 的
   「结构闸自修/禁重写」纪律 → rm 重建降级版。
4. **SOUL Phase 2 段 ~80 行**,其中委派细节/QC↔修订循环机制/bundle 装配命令/
   收尾合并+phase2_summary 字段清单(约 50 行)是域逻辑,常驻主代理 system prompt。

判定域已有同构解法(render_judge_prompt + prompt_file,会话 9a23a→247a 实证:模板逐字到达、
派发调用 143.6s/15k 输出 token 归零)。本设计把该模式复制到解析域,同时按
soul-skill-split 既有原则把 P2 域逻辑下沉进 skill。

## 方案

### 1. render_parse_prompt.py(criteria-parser/scripts/)

一个脚本,三类委派,一次渲染产出全部 prompt 文件 + 主代理照抄的派发行:

```bash
python3 .../render_parse_prompt.py --workspace <ws> --meta <criteria_meta.json> \
    [--redo-track IN|EX]  # 初解析:两轨 prompt;--redo-track:单轨重做(吃结构闸点名)
```

- **解析/重做模板**(占位符 `{TRACK}/{TRACK_CN}/{RAW_START}/{RAW_END}/{TOP_NUMBERS}/
  {GATE_CMD}`):从 meta 的 `raw段行号`/`末条号` 机械注入;重做模板内嵌「重做=修订,
  禁 rm 重建」与结构闸点名条目(从 structure_gate 产物读)。
- **QC 模板**(`{TRACK}/{ROUND}`):固定读取证包/结构闸拒工条件/全量纪律。
- **修订模板**(`{TRACK}/{ROUND}`):指向 criteria-repair.md 唯一权威 + 中性化要求。
- **规则内嵌**:模板占位符 `{PARSING_RULES}` 由脚本从 parsing-rules.md **按节标题机械抽取**
  (拆分原则/或组/编号规则/转化规则/常见拆分错误)嵌入——规则单一权威不变(仍住
  parsing-rules.md),模板零手抄,子代理不再自读全文。
- stdout 给出每份 prompt 的路径 + `task(prompt_file=...)` 一行照抄派发 + expected_outputs 建议。

### 2. write_phase2_summary.py(收尾机械写盘)

phase2_summary.json 字段全部机械可算(路径拼接/QC 状态合取/四分类计数/ocr_results 从
pdf_classification+覆盖率产物读)→ 脚本落盘,消灭「先写占位 stub」「字段手写错」两类故障,
SOUL 的 20 行字段清单不再需要常驻。

### 3. skill 文档重构(域逻辑下沉)

- parse-delegation.md:委派模板段改为 render 占位符形态 + 「派发=渲染,禁止手写 prompt」铁律
- criteria-qc-checklist.md / criteria-repair.md:委派段同步占位符形态
- **新增 references/phase2-orchestration.md**:QC↔修订循环机制(单轮五步、四件套把守、
  bundle 前置与每轮重装、结构闸与 QC 不同轮、修订子代理类型、slim 时序)、
  收尾合并(slim×2→assemble→自检→交付→summary)——内容从 SOUL P2 迁入,SOUL 只留指针

### 4. SOUL P2 瘦身(80 行 → ~30 行)

保留(编排骨架职责):入口条件(ocr_route 检查)、并发预算与三轨调度、A→B 降级决策、
屏障(结构闸→QC 时序、QC 通过才 slim、两轨过+OCR 全覆盖才收尾、blocked_round_limit 停摆)、
OCR 覆盖率门禁、todos 状态。
下沉(→ skill):委派模板细节与派发纪律(→render+parse-delegation)、QC↔修订循环机制与
四件套(→phase2-orchestration)、bundle 装配命令(→phase2-orchestration)、
收尾命令与 phase2_summary 字段清单(→phase2-orchestration+summary 脚本)。
契约防回流:test_soul_skill_contract 的 SUNK_FROM_SOUL 增补 needle(bundle 命令、
summary 字段名、QC 循环措辞)。

## 不做(范围外)

- 不动判定域 render_judge_prompt(已稳定)
- 不改 parsing-rules.md 规则内容(只被抽取嵌入)
- OCR 委派(pdf-image-extractor)暂不 render 化——其模板短(0.5k)且稳定,另案
- 不迁移 P2.5/P3+(本次只动 P2)

## 验收标准

- 881e7ba8 workspace 副本端到端:render 产出 IN/EX 解析、QC×3、修订×3 全部 prompt 文件,
  变量注入正确(行号/编号/轮次/闸点名),规则节嵌入完整,leftover 占位符为零
- 子代理 prompt 不再要求自读 parsing-rules.md 全文(嵌入节覆盖依赖面)
- SOUL.md 行数下降 ≥40 行;test_soul_skill_contract 全绿(含新增防回流 needle)
- tests/skills 全量回归无新增失败;render/summary 脚本 TDD 测试覆盖
