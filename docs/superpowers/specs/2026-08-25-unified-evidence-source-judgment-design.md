# 统一证据源判定设计（取消 documents 维度）

日期：2026-08-25 · 状态：待审阅

## 背景与目标

当前 `/eligibility-judgment` 按**物料**（来源文档）独立判定：判定产物
`judgments_{id}.json` 为 `documents:{<物料名>:{judgments:{条件ID:{...}}}}`，
同一患者有 N 份物料就产 N 套判定，同一条条件可能出现「病历判符合、检查判无法判断」等矛盾。

用户决策：**同一患者的所有 OCR 材料是共享证据材料，作为统一入排判定证据源**——
每条条件只判定一次，evidence 跨物料合并（`source` 字段标注物料）；多物料证据冲突时结论按
**不符合 > 符合 > 存疑 > 无法判断** 折叠，不允许矛盾结论共存。

产物 schema 形态已与用户确认：**方案 A——彻底取消 documents 维度**（顶层直接 `judgments`）。

## 新判定产物 schema

```jsonc
// judgments_draft_{id}_{TRACK}_b{N}.json 与 outputs/judgments_{id}.json 同形态
{
  "patient_id": "S042002",
  "judgment_date": "2026-08-25",
  "judgments": {                  // 顶层，条件ID → 条目
    "IN-1": {
      "conclusion": "符合",        // 唯一结论：全部物料证据综合 + 冲突折叠
      "reason": "知情同意书签署=2026-04-15…",
      "evidence": [                // 跨物料合并；source 标注物料名
        {"source": "筛选期病历", "page": 5, "screenshot_ref": "…", "quote": "…"},
        {"source": "筛选期检查", "page": 12, "screenshot_ref": "…", "quote": "…"}
      ]
      // "exclusion_triggered" 仅 EX-* 且 conclusion∈{符合,不符合} 时必填（不变）
    }
  },
  "summary": {"符合": 30, "不符合": 1, "存疑": 5, "无法判断": 38},   // 顶层，条件口径
  "criteria_rollup": {             // 终稿：merge-judgments 机械重算（全量一次）
    "IN-1": {"conclusion": "符合", "rule": "单条", "members": ["IN-1"], "decided_by": ["IN-1"], "counts": {…}}
  },
  "rollup_summary": {"符合": 17, …},
  "warnings": [],                  // 原 cross_doc_warnings 合并于此（跨物料一致性标注）
  "rollup_warnings": []
}
```

关键语义变化：

- `evidence[].source` = 物料来源名（逐字等于 `phase2_summary.ocr_results[].source`），
  仍是必填——它现在是**物料维度在产物里唯一的存活点**。
- 「无法判断」语义：从「该文档中无对应内容」→「**全部** OCR 材料中无对应内容」
  （穷尽全部物料取证后仍缺失才判）。
- `summary` 计数口径 = 条件数（不再 × 物料数）；判定子代理 result 计数同口径。

## 判定流程变化（委派模板）

- 删除 `{DOC_KEYS}` 注入与「每份来源独立判定全部条目」「documents 键必须取给定值」硬规则。
- 新增注入：**统一证据源判定指令**——
  1. 该患者全部 OCR 材料（`{OCR_PATHS}`）为共享证据，每条条件只判一次；
  2. 证据来自任意物料，`evidence[].source` 逐字取给定来源名列表；
  3. **多物料证据冲突时结论按 不符合 > 符合 > 存疑 > 无法判断 折叠**（取最高优先级结论，
     reason 中说明各物料证据及折叠依据）；
  4. 物料间一致性矛盾（姓名/性别/年龄>2岁/ECOG 不一致等，沿用现「跨文档一致性检查」表）
     → 记入 reason 并写入 `warnings`；
  5. result 计数口径 = 条件数（四类之和必须等于本批条件ID 数）。
- 分批（12 条/批）、双轨、QC 轮次机制不变——分批是条件维度，与物料无关。

## 判定原则（judgment-principles.md）

- **原则一「逐文档独立」改写为「统一证据源」**：同一患者全部 OCR 材料为共享证据源，
  每条条件一次判定；多物料冲突折叠规则（不符合>符合>存疑>无法判断）；
  原「跨文档矛盾单独标记为警告」保留（改述为物料间矛盾标注）。
- 42 条判定约束中 #7「逐文档独立：每份文档独立判定，不合并信息」→ 改为
  「统一证据源：全部物料合并判定，冲突按优先级折叠」。

## 逐文件改动清单

### 1. 判定 skill 脚本（skills/custom/eligibility-judgment/scripts/）

| 文件 | 改动 |
|---|---|
| `check_judgment_structure.py` | 闸1 顶层结构：`documents` → 顶层 `judgments`+`summary`；闸2 条件ID 恒等：顶层 judgments 键集合；闸3/4/5/12 迭代顶层 judgments（`flatten()` 简化）；**闸9 删除 document 键校验，改为 evidence source 白名单校验**（`evidence[].source` ⊆ phase2_summary 的 OCR 来源集合，phase2_summary 不可读时跳过出声）；闸6/8 口径随 flatten 变化 |
| `judge_pack.py` | `merge-judgments`：删除 document 合并层（`documents.setdefault` 循环），两轨顶层 judgments 直接合并；summary 顶层重算；`rollup_document(合并后 judgments, groups)` 一次全量；`check_shard_documents_consistent` 删除；`merge-recheck` 同改；fix-summary 顶层化 |
| `rollup.py` | `rollup_document` 本身吃 judgments dict，签名不变；docstring 更新（不再写 `documents[]`） |
| `render_judge_prompt.py` | 删除 `{DOC_KEYS}` 渲染；新增 `{EVIDENCE_SOURCES}`（来源名列表，供 evidence.source 取值与 source 白名单）；`{OCR_PATHS}` 保留；leftover 占位符校验同步 |
| `uncertain_recheck.py` | L277/343 documents 迭代 → 顶层 judgments |
| `exclusion_direction_check.py` | L232 documents 迭代 → 顶层 judgments |
| `check_reason_alignment.py` | L458 documents 迭代 → 顶层 judgments；docstring 措辞 |
| `evidence_bundle.py` | L153 documents 迭代 → 顶层 judgments |
| `run_judgment_gates.py` | 调用链参数不变，wrapper 顺序不变 |

### 2. 判定 skill 文档（skills/custom/eligibility-judgment/）

| 文件 | 改动 |
|---|---|
| `SKILL.md` | 概述「逐文档独立性」→「统一证据源」；判定输出格式节（documents 三条易错 → 顶层 judgments 规则）；合并与终检节（按 document 合并 → 顶层合并+一次全量 rollup）；跨文档一致性检查表保留并改述为「物料间一致性标注」；交付清单不变 |
| `references/judgment-schema.md` | 顶层字段表重写；evidence.source 语义（物料来源名）；闸表更新（闸9 新语义） |
| `references/judge-delegation.md` | 委派模板：删 DOC_KEYS 硬规则，加统一证据源指令+冲突折叠+result 计数口径；schema 指引更新 |
| `references/qc-delegation.md` | 删除孪生条目/双文档核验点 |
| `references/judgment-repair.md` | 删除「孪生条目同批复核」节；pointer 示例 `/documents/{source}/judgments/…` → `/judgments/…` |
| `references/judgment-principles.md` | 原则一改写；约束 #7 改写；「无法判断」语义更新（全部材料） |
| `references/schema_example.json` | 顶层结构示例重写 |
| `references/failure-archive.md` | 历史故障记录**保留原样**（历史档案不改写） |

### 3. 编排层（backend/.deer-flow/agents/eligibility-screener/SOUL.md）

- L580「判定委派须显式给定 documents 键」→ 删除（无此维度；改为提醒 evidence.source 取真实来源名）。
- L320 阶段表「患者 × 轨 并行判定（子代理只读本轨包 + 该患者 OCR）」→「…统一证据源判定」。
- 其余（P3 调度、派发核对、expected_outputs、phase3_summary 口径）不变。

### 4. 报告生成（skills/custom/screening-report-generator/scripts/build_reports.py）

- 新产物为顶层 `judgments`（无 documents）：`normalize_documents` 的扁平 fallback 已是正确路径，
  将其提升为**第一公民路径**（doc_key 用 `patient_id`，标签=患者名）；
  多 documents 兼容保留（历史产物）+ `merged` 跨物料折叠保留为防御层（新产物下退化为 no-op）。
- 报告模板不变；SKILL.md 补一行「统一判定产物为顶层 judgments 形态」。

### 5. 测试（tests/skills/）

- 受影响的既有测试（fixture 改 documents → 顶层 judgments）：`test_check_judgment_structure.py`、
  `test_judge_pack.py`、`test_judgment_rollup.py`、`test_uncertain_recheck.py`、
  `test_exclusion_direction_check.py`、`test_check_reason_alignment.py`、`test_evidence_bundle.py`、
  `test_render_judge_prompt.py`、`test_expected_outputs_contract.py`、
  `test_judgment_authority_single_source.py`、`test_or_group_split_gate.py`、
  `test_soul_skill_contract.py`、`test_skill_slimming_contract.py`、
  `test_screening_report_generator.py`。
- 新增测试：顶层 judgments 结构闸；闸9'（evidence source 白名单：编造 source 被拦）；
  merge-judgments 无 documents 合并 + 全量 rollup；render 注入统一证据源指令且不再注入 DOC_KEYS；
  报告构建器对顶层 judgments 的第一公民路径。

## 不做的（范围外）

- 不迁移历史会话产物（f9231297 等旧 outputs 保持原样）。
- 不改判定子代理的 OCR 读取方式（现在已读全部 OCR，只是输出按物料分）。
- 不改 criteria-parser / patient-separator / pdf-image-extractor / criteria_report.html。
- 报告层跨物料折叠（merged）保留为防御，不删除。

## 实施顺序建议（供 writing-plans）

1. schema 与判定原则（judgment-schema.md / judgment-principles.md / schema_example.json）
2. 判定产物脚本（judge_pack → check_judgment_structure → 其余四闸 → render_judge_prompt）
3. 委派/QC/改判文档（judge-delegation / qc-delegation / judgment-repair / SKILL.md）
4. SOUL.md
5. 报告构建器第一公民路径
6. 测试适配 + 新增（TDD：每步先改测试）
7. 全量回归（tests/skills + backend 相关）与真实数据验证

## 验收标准

- `tests/skills/` 全绿（含既有失败修复回归）。
- 用真实数据模拟新产物形态跑通：结构闸、四机械闸、merge-judgments、报告构建+verify。
- 报告打开后每条条件一枚结论徽章，evidence 跨物料合并且 source 标注物料。
