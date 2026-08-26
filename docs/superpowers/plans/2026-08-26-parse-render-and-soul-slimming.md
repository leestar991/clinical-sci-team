# 解析域 render 模式 + SOUL P2 下沉 实现计划

Spec: `docs/superpowers/specs/2026-08-26-parse-render-and-soul-slimming-design.md`
用户已确认(2026-08-26):write_phase2_summary.py 纳入;顺序按本计划。

全局约束:
- 运行测试:`python3 -m pytest tests/skills/<file>.py -q`(repo 根);harness 脚本无
- skills/custom/ 与 backend/.deer-flow/ gitignored:脚本/文档改动不入库,只提交 tests/skills/ 与 docs/superpowers/
- 提交信息 `feat(criteria-parser): ...`/`test(...)`,尾行 Co-Authored-By: Claude <noreply@anthropic.com>
- 契约防回流:test_soul_skill_contract.SUNK_FROM_SOUL 增补 needle

### Task 1: render_parse_prompt.py — 解析/重做模板(TDD)

Files: skills/custom/criteria-parser/scripts/render_parse_prompt.py(新)、parse-delegation.md(模板段占位符化)、tests/skills/test_render_parse_prompt.py(新)

- [ ] Step 1 测试先行:kind=parse 渲染两轨(行号/编号区间/{PARSING_RULES} 节嵌入/无 leftover 占位符/派发行含 prompt_file);kind=redo 吃 structure_gate 点名+内嵌禁 rm 铁律;meta 缺 raw段行号 → exit 2;规则节缺失 → exit 2
- [ ] Step 2 跑 RED
- [ ] Step 3 实现:模板从 parse-delegation.md `<!-- template:parse/redo -->` 标记段抽取;{PARSING_RULES} 从 parsing-rules.md 按节标题抽(拆分原则/条件转化规则/可获取性判定标准);变量 {TRACK}/{TRACK_CN}/{RAW_START}/{RAW_END}/{TOP_NUMBERS}/{GATE_CMD}/重做 {GATE_PROBLEMS}
- [ ] Step 4 GREEN + parse-delegation.md 模板段落盘

### Task 2: QC/修订模板 render 化(TDD)

Files: 同上脚本 + parse-delegation.md 模板段 + tests 同文件追加

- [ ] Step 1 测试:kind=qc(--track --round)含拒工条件/全量纪律/取证包入口;kind=repair 含 repair 唯一权威指针/中性化;轮次注入;structure_gate total 注入
- [ ] Step 2 RED → Step 3 实现 → Step 4 GREEN

### Task 3: phase2-orchestration.md 新建 + 文档重构

Files: references/phase2-orchestration.md(新)、parse-delegation.md、criteria-qc-checklist.md、criteria-repair.md、SKILL.md(索引)

- [ ] 从 SOUL P2 迁入:QC↔修订循环机制(单轮五步/四件套把守/bundle 前置与每轮重装/结构闸与 QC 不同轮/修订子代理类型/slim 时序)+ 收尾合并(slim×2→assemble→自检→交付→summary 脚本调用)
- [ ] 三文档委派段改「派发=渲染,禁手写 prompt」指针;SKILL.md 索引新文件

### Task 4: write_phase2_summary.py(TDD)

Files: skills/custom/criteria-parser/scripts/write_phase2_summary.py(新)、tests/skills/test_write_phase2_summary.py(新)

- [ ] Step 1 测试(f9231297 字段实证为基准):路径类/criteria_qc_passed(合取)/criteria_qc_status 三态/blocked_round_limit→passed=false/criteria_count 四类目计数/patient_mode(--patient-mode 枚举或 pdf_classification 已落盘)/ocr_results(route A→ocr_file,B→ocr_dir+计数);QC 未收敛→exit 2 拒写
- [ ] Step 2 RED → Step 3 实现 → Step 4 GREEN

### Task 5: SOUL P2 瘦身 + 契约防回流

Files: SOUL.md、tests/skills/test_soul_skill_contract.py

- [ ] P2 段 80→~30 行(留:入口/预算/调度/屏障/降级/覆盖率门禁;域细节→指针 phase2-orchestration.md + render 脚本)
- [ ] SUNK_FROM_SOUL 增 needle:bundle 装配命令、phase2_summary 字段名(criteria_qc_passed 等)、QC 循环措辞
- [ ] 全量 tests/skills 回归

### Task 6: 端到端验证(881e7ba8 副本)

- [ ] workspace 副本上:render 产出 IN/EX 解析、重做(用真实 structure_gate)、QC R2、修订 R2 全部 prompt;变量核对(行号 276-350/350-468、编号 1..11/1..20、点名条目)
- [ ] 收尾提交(docs/superpowers/ spec+plan)
