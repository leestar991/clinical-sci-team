# Plan: 将 Phase 2.5 QC 并入 Phase 2 与 OCR 并行

## Context

当前 eligibility-screener 的 Phase 2 和 Phase 2.5 是**串行**关系：

```
Phase 2:  入排标准解析 + OCR 分片（所有批次）
    ↓ (等待全部 OCR 完成)
Phase 2.5: QC + 患者拆分 + 按患者聚合 OCR
```

**问题**：Phase 2.5 的 QC 任务（bash 收敛 + `task(quality-control)` 语义核验）完全可以在 `criteria_parsed.json` 产出后立即启动，与后续 OCR 批次并行执行。当前串行设计浪费了 OCR 等待时间。

**目标**：将 QC（bash 收敛 + 语义核验 + 修正）提前到 Phase 2 内部，与 OCR 并行；仅保留患者拆分和 OCR 聚合作为 OCR 完成后的收尾步骤。删除独立的 Phase 2.5，将其剩余工作并入 Phase 2 末尾。

## 依赖分析

```
criteria_parsed.json      ← criteria-parser task (Phase 2 Batch 1)
criteria_qc.json           ← 依赖 criteria_parsed.json（可与 OCR 并行）
OCR page *.md              ← OCR 分片 tasks（Phase 2 各批次）
patient_index.json         ← 依赖 OCR page *.md 全部完成
ocr_records.md             ← 依赖 patient_index.json + OCR page *.md
```

**结论**：QC 与 OCR **完全无依赖**，可并行。

## 修改方案

### 改动的唯一文件

`backend/.deer-flow/agents/eligibility-screener/SOUL.md`

### 改动内容

#### 1. 合并 Phase 2 和 Phase 2.5，删除 Phase 2.5

**阶段总览表** 从 6 个 Phase 缩减为 5 个：

| Phase | 核心动作 | 关键产出 |
|-------|---------|---------|
| 1 | 预处理 | pdf_classification.json, eligibility_criteria_raw.md |
| **2** | **入排解析 + OCR 分片 + QC 收敛 + 患者拆分 + OCR 聚合** | criteria_parsed.json, criteria_qc.json, patient_index.json, ocr_records.md |
| 3 | 匹配分析 | judgments_draft.json |
| 4 | QC + 推断理由 | judgments_{id}.json, qc_report, reasons |
| 5 | 报告交付 | HTML reports |

#### 2. 重构 Phase 2 执行流程

**新流程（关键变化用 `>>` 标记）**：

```
Phase 2 执行步骤：

第 1 批（3 并发）：
  task(解析入排标准) || task(OCR分片1) || task(OCR分片2)

>> 第 2 批（入排解析返回后立即启动，不等 OCR 全部完成）：
>>   bash: QC 客观格式收敛（对 criteria_parsed.json 做结构校验+修复）
>>   || task(OCR分片3) || task(OCR分片4)

第 3 批：
>>   task(quality-control): 语义 QC（基于已修复的 criteria_parsed.json）
>>   || task(OCR分片5) || task(OCR分片6)
>>   （如 QC 语义任务提前返回、仍有 OCR 在进行，可再派下一批 OCR）

>> QC 阻断问题修正 + OCR 剩余批次（并行）：
>>   - 若有阻断级问题 → 主代理 LLM 逐条修正 criteria_parsed.json
>>     （用 write_file/str_replace，与 OCR 批次并行）
>>   - 继续 OCR 分片直到全部完成
>>   - 阻断修正后 → 第 2 轮 task(quality-control) 复核（与 OCR 并行）

全部 OCR 完成 + QC 通过（或达 2 轮上限）后：

>> 同一轮并行（OCR 全部就绪后）：
>>   task(patient-separator): 患者拆分 → patient_index.json
>>   （patient-separator 完成后立即 bash cat 拼接 OCR → ocr_records.md）

>> present_files: criteria_parsed.json, criteria_qc.json, patient_index.json + ocr_records.md

✅ write_todos: [✓] P2
```

**Todolist 模板** 同步更新——去掉 `P2.5` 行。

#### 3. 关键约束保持不变

- **QC 收敛机制**（原则 12）不变：bash 收敛格式 → task 语义核验 → 阻断/建议分级 → 最多 2 轮
- **患者拆分**仍在所有 OCR 完成后执行（依赖 OCR 全量数据）
- **覆盖率检查**仍在 OCR 全部完成后执行
- **present_files 去重规则**（原则 15）不变

## 历史教训（来自监控数据）

多轮监控会话验证了此设计的必要性：

| 问题 | 发生阶段 | 关联 |
|------|----------|------|
| QC 5 轮不收敛，全为主观语义问题 | P2.5 | P2.5 独立成 Phase 易诱导过度迭代 |
| Budget 硬停 (1.5M input tokens) 在 P2.5，P3-P5 未执行 | P2.5 | 串行等待浪费上下文 token |
| Agent 在 P2.5 后提前退出，从未到达 P3 | P2.5→P3 | Phase 过多加剧遗漏风险 |
| Subagent 慢 (GPT-5-4 3-6 min/batch) + Watchdog 600s 误杀 | P2 | 并行可缓解 subagent 慢的问题 |

**核心教训**：P2.5 作为独立 Phase 使得 agent 倾向在 QC 上过度投入（多轮循环），串行阻塞增加了 token 消耗和提前退出风险。将 QC 并入 P2 与 OCR 并行，既可以减少 Phase 跳转次数，也能利用并行降低总耗时。

## 预期收益

| 指标 | 当前 | 调整后 |
|------|------|--------|
| Phase 数量 | 6（含 P2.5） | 5 |
| QC 启动时机 | Phase 2 全部 OCR 完成后 | Phase 2 第一批返回后立即 |
| 串行等待 | P2 全量 OCR → P2.5 QC | OCR 与 QC 完全并行 |
| 预计节省 | - | 1-2 轮 LLM 调用周期（约 2-5 分钟） |
| 预期缓解 | QC 过度循环、Budget 硬停 | QC 在 OCR 背景下执行，减少孤立 QC Phase 的"完美主义"倾向 |

## 验证方式

1. 文件修改后，通过 `read_file` 确认 SOUL.md 内容完整、阶段编号连贯
2. 检查 todolist 初始化模板与新 Phase 结构一致
3. 通过新会话运行 eligibility-screener，观察：
   - Phase 2 第一批后 QC 是否与 OCR 并行启动
   - write_todos 正确反映 Phase 2 进度（不需要 P2.5 行）
   - present_files 在 Phase 2 结束时正确触发
