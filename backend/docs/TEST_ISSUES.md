# Subagent 协作测试问题记录

> 测试文件：`tests/test_lead_subagent_collaboration.py` + `tests/test_collaboration_scenarios.py`
> 测试分支：`feature/virtual-clinical-dev-team`
> 记录日期：2026-04-08

---

## 概述

针对以下五个协作场景对 Lead Agent + Subagent 进行静态测试（无 LLM 调用，纯配置 + 逻辑层）：

| 场景 | 主要 Agent |
|------|-----------|
| clinical-medicine | parkinson-clinical |
| biostats | trial-statistics |
| regulatory | drug-registration |
| ops-quality | clinical-ops + quality-control |
| sci-ppt-generation | literature-analyzer + data-extractor + report-writing + ppt-generation skill |

共发现 **11 个问题**，分为「基础设施层」和「场景层」两类。每个问题均有对应的回归测试用例固定当前行为。

---

## 一、基础设施层问题

### Issue-1：`MAX_CONCURRENT_SUBAGENTS` 双重定义

**文件**：`backend/packages/harness/deerflow/subagents/executor.py`

**现象**

```python
# Line ~71 — 用于初始化线程池
MAX_CONCURRENT_SUBAGENTS = 5
_scheduler_pool = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_SUBAGENTS, ...)
_execution_pool  = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_SUBAGENTS, ...)

# Line ~470 — 线程池已创建后再次赋值
MAX_CONCURRENT_SUBAGENTS = 3
```

**发生原因**

开发者在文件底部补加了「可见常量」= 3，但线程池已用值 5 创建，两者不一致。

**影响**

- 线程池容量为 5，但可见常量显示 3，造成代码阅读歧义。
- `get_max_concurrent()` 运行时从 config 读值，`SubagentLimitMiddleware` 同样从 config 读值，因此功能上没有实际 bug，但易引发误解。

**改进建议**

删除第一处 `MAX_CONCURRENT_SUBAGENTS = 5`，线程池改用 `get_max_concurrent()` 动态值或显式常量（如 `_POOL_SIZE = 5`）与并发限制分开命名。

**回归测试**：`TestKnownIssues::test_issue1_max_concurrent_constant_defined_twice_in_executor`

---

### Issue-2：`parent_model` 从 `metadata` 而非 `configurable` 读取

**文件**：`backend/packages/harness/deerflow/tools/builtins/task_tool.py`

**现象**

```python
metadata = runtime.config.get("metadata", {})
parent_model = metadata.get("model_name")   # 通常为 None
```

LangGraph 实际将 `model_name` 放在 `configurable` 而非 `metadata`，导致 `parent_model` 始终为 `None`。

**影响**

所有设置了 `model = "inherit"` 的 subagent（如 `general-purpose`、`bash`）实际上无法继承父 Agent 的模型，回退到系统默认值。设置了具体模型名的 clinical agent 不受影响（它们不使用 `inherit`）。

**改进建议**

```python
parent_model = (
    runtime.config.get("configurable", {}).get("model_name")
    or runtime.config.get("metadata", {}).get("model_name")
)
```

**回归测试**：`TestKnownIssues::test_issue2_task_tool_reads_parent_model_from_metadata_not_configurable`

---

### Issue-3：`SubagentLimitMiddleware` 截断无通知

**文件**：`backend/packages/harness/deerflow/agents/middlewares/subagent_limit_middleware.py`

**现象**

当 Lead Agent 在单次响应中生成超过 `max_concurrent` 个 `task()` 调用时，中间件静默地将超出部分从 `tool_calls` 列表中删除。被丢弃的任务：

- 不产生任何 `ToolMessage` 错误响应
- AIMessage 的 `content` 文本不变（Lead Agent 仍认为自己说了"我会做这 4 件事"）
- Lead Agent 只在最终聚合结果时才发现缺少输出

**影响**

在 4 个或以上 agent 的并行场景（如 IND package：toxicology + pharmacology + chemistry + drug-registration）中，第 4 个任务被静默丢弃，Lead Agent 无法得知，可能产生不完整的输出而不报错。

**改进建议**

为每个被截断的 `tool_call` 注入一个合成 `ToolMessage`，内容说明该任务因并发限制被延迟，建议 Lead Agent 在下一轮重新调度：

```python
ToolMessage(
    content=f"Task '{tc['args'].get('description', tc['id'])}' was deferred: "
            f"concurrent subagent limit ({self.max_concurrent}) reached. Re-schedule in next turn.",
    tool_call_id=tc["id"],
)
```

**回归测试**：`TestSilentTruncationGap`、`TestKnownIssues::test_issue3_truncation_does_not_inform_lead_agent`

---

### Issue-4：`MAX_CONCURRENT_SUBAGENTS` 未从 `subagent_limit_middleware.py` 导出

**文件**：`backend/packages/harness/deerflow/agents/middlewares/subagent_limit_middleware.py`
**影响文件**：`backend/tests/test_subagent_limit_middleware.py`

**现象**

现有测试文件导入了一个不存在的常量：

```python
# test_subagent_limit_middleware.py line 8
from deerflow.agents.middlewares.subagent_limit_middleware import (
    MAX_CONCURRENT_SUBAGENTS,  # ← 此常量在 middleware 文件中未定义
    ...
)
```

`subagent_limit_middleware.py` 只定义了 `MIN_SUBAGENT_LIMIT = 2` 和 `MAX_SUBAGENT_LIMIT = 10`，没有 `MAX_CONCURRENT_SUBAGENTS`。

**影响**

`test_subagent_limit_middleware.py` 在运行时会因 `ImportError` 立即失败，整个文件的测试均无法执行。

**改进建议**

方案 A（推荐）：在 `subagent_limit_middleware.py` 末尾添加：
```python
MAX_CONCURRENT_SUBAGENTS = get_subagents_app_config().max_concurrent
```

方案 B：修改 `test_subagent_limit_middleware.py`，改从 `deerflow.config.subagents_config` 导入。

**回归测试**：`TestKnownIssues::test_issue4_max_concurrent_subagents_missing_from_middleware_module`、`test_issue4_importing_missing_constant_raises_import_error`

---

## 二、场景层问题

### CM-1：`parkinson-clinical` 只读，无法直接输出报告

**场景**：clinical-medicine

**现象**

`parkinson-clinical` 的 `tools` 列表中不包含 `write_file`（设计为只读咨询角色）。当 Lead Agent 要求 PD 专家"出具一份 PD 终点评估报告"时，该 Agent 无法直接写文件。

**发生原因**

设计决策：`parkinson-clinical` 定位为"顾问"，不应直接产出文件，避免产出格式与其他 Agent 不一致。

**影响**

Lead Agent 必须额外调度一次 `report-writing` Agent 才能将 PD 专家的分析转化为书面输出，增加一个轮次。若 Lead Agent 不了解此约束，可能尝试反复调用 `parkinson-clinical` 期望文件输出，导致无效循环。

**改进建议**

在 `parkinson-clinical` 的 `description` 中明确说明：

```
Do NOT expect written output — results are returned as text only.
For written clinical reports, combine with report-writing agent.
```

**回归测试**：`TestClinicalMedicineAgentConfig::test_parkinson_clinical_is_read_only_no_write_file`、`TestClinicalMedicineCollaborationDesign::test_cm1_parkinson_clinical_read_only_forces_report_writing_collaboration`

---

### CM-2：`parkinson-clinical` 描述未交叉引用 `bioinformatics`

**场景**：clinical-medicine

**现象**

`parkinson-clinical` 的 `system_prompt` 引用了 GBA、LRRK2、SNCA 等基因亚型，但其 `description` 没有说明"遗传生物标志物分析请使用 bioinformatics"。

**影响**

Lead Agent 在处理 GBA1 携带者亚组分析时，可能错误地仅调用 `parkinson-clinical` 而遗漏 `bioinformatics`，导致基因组学分析不完整。

**改进建议**

在 `parkinson-clinical` 的 `description` 的 "Do NOT use for" 部分添加：

```
- Genomic data analysis or NGS pipeline (use bioinformatics)
```

**回归测试**：`TestClinicalMedicineCollaborationDesign::test_cm2_parkinson_clinical_description_lacks_bioinformatics_crossref`

---

### BS-1：多 Agent 共享 `/mnt/user-data/workspace`，无路径隔离

**场景**：biostats（及所有场景）

**现象**

所有 Agent 的 `system_prompt` 中 workspace 路径均为 `/mnt/user-data/workspace`，没有 Agent 级子目录。并行运行的两个 Agent 可能对同一路径下的文件进行读写冲突（如均写 `sap_draft.md`）。

**发生原因**

Sandbox 路径设计为 per-thread 隔离（`/mnt/user-data/workspace` → 物理路径 `.deer-flow/threads/{thread_id}/user-data/workspace/`），但 subagent 与 lead agent 共享同一个 thread_id，因此共享同一 workspace 目录。

**影响**

两个并行 Agent（如 `trial-statistics` + `parkinson-clinical`）同时写入 workspace 时可能产生竞态条件，导致文件内容损坏或被覆盖。

**改进建议**

为每个 subagent 分配独立子目录（如 `workspace/trial-statistics/`、`workspace/parkinson-clinical/`），并在 `_build_initial_state()` 中注入 Agent 专属的工作目录前缀。

**回归测试**：`TestBiostatsCollaborationDesign::test_bs1_workspace_path_not_isolated_between_agents`

---

### BS-2：`trial-statistics` 与 `parkinson-clinical` 无审核回路

**场景**：biostats

**现象**

`trial-statistics` 生成 SAP 后，没有设计机制要求 `parkinson-clinical` 对统计终点的临床合理性进行审核，反之亦然。

**影响**

对于 PD 试验，统计方法（MMRM on MDS-UPDRS）的临床合理性未被验证，Lead Agent 需要主动编排审核步骤，但没有配置层面的提示。

**改进建议**

在 `trial-statistics` 的 `description` 末尾添加推荐工作流说明，提示 Lead Agent 在 SAP 完成后调用 `parkinson-clinical` 做临床验证。

**回归测试**：`TestBiostatsCollaborationDesign::test_bs2_no_review_loop_between_stats_and_clinical`

---

### RA-1：`drug-registration` 900 秒超时在复杂多辖区场景下可能不足

**场景**：regulatory

**现象**

`drug-registration` 超时设置为 900 秒。在需要同时处理 FDA pre-IND + EMA Scientific Advice + NMPA IND 三条路径时，网络搜索 + 文件写入可能超时。

**影响**

任务以 `TIMED_OUT` 状态结束，Lead Agent 收到超时错误，需要重试。

**改进建议**

将 `drug-registration` 超时提高至 1200 秒，或在任务设计上拆分：单辖区单任务（如 `FDA 路径分析` + `EMA 路径分析` 并行），而不是一个任务覆盖全部辖区。

**回归测试**：`TestRegulatoryCollaborationDesign::test_ra1_drug_registration_900s_timeout_may_be_tight_for_complex_scenarios`

---

### RA-2：`drug-registration` 描述缺少对 `cmo-gpl` 的获益-风险边界说明

**场景**：regulatory

**现象**

`drug-registration` 的 description 没有说明"整体获益-风险评估应由 `cmo-gpl` 负责"，而 `drug-registration` 的 system_prompt 中包含与获益-风险相关的内容（EMA CHMP 方法论等）。

**影响**

Lead Agent 可能将获益-风险评估错误地路由给 `drug-registration`，而实际应由 `cmo-gpl` 处理。

**改进建议**

在 `drug-registration` 的 "Do NOT use for" 中添加：

```
- Overall benefit-risk assessment or safety strategy (use cmo-gpl)
```

**回归测试**：`TestRegulatoryCollaborationDesign::test_ra2_drug_registration_description_lacks_cmo_gpl_safety_boundary`

---

### OQ-1：`clinical-ops` 使用 `claude-haiku-4-5`，复杂合规场景能力不足

**场景**：ops-quality

**现象**

`clinical-ops` 使用轻量级模型 `claude-haiku-4-5` 以降低高频调用成本。但对于复杂的 IRB/IEC 谈判方案、CRO 合同条款分析，Haiku 的推理深度可能不足。

**影响**

在需要细致判断的运营合规场景下（如 CRO 合同纠纷分析、国家级监管差异对比），输出质量可能低于预期。

**改进建议**

对 `clinical-ops` 区分任务复杂度：模板化任务（站点可行性调查、IMP 供应计划）保持 Haiku；合规分析任务（IRB 审批策略、CRO QTL 设计）升级为 Sonnet。可通过 `max_turns` 或任务描述触发不同配置。

**回归测试**：`TestOpsQualityAgentConfig::test_oq1_clinical_ops_haiku_model_cost_efficiency`

---

### OQ-2：PV 质量边界在 `quality-control` 和 `cmo-gpl` 之间模糊

**场景**：ops-quality

**现象**

`quality-control` 的 `system_prompt` 包含"Pharmacovigilance quality: SAE reconciliation; quality review of pharmacovigilance systems"，而 `quality-control` 的 description 中说明 "pharmacovigilance case processing (use cmo-gpl for safety strategy)"。两者边界描述不一致：

- SAE 质量审核 → quality-control
- PV 安全策略 → cmo-gpl
- 但"质量审核"和"安全策略"在实际操作中高度重叠

**影响**

Lead Agent 在处理 SAE 报告质量问题时，无法准确判断应调用 `quality-control` 还是 `cmo-gpl`，可能导致双重调用或遗漏。

**改进建议**

明确边界定义：`quality-control` 只负责 GCP 合规质量审核（系统性、程序性），不负责单案例安全判断；单案例安全判断属于 `cmo-gpl` 的安全策略范畴。在两个 agent 的 description 中对应说明。

**回归测试**：`TestOpsQualityCollaborationDesign::test_oq2_pharmacovigilance_quality_boundary_ambiguous`

---

### PPT-1：无专用 `sci-ppt-generation` Subagent，Lead 须手工编排

**场景**：sci-ppt-generation

**现象**

`sci-ppt-generation` 不是一个注册的 subagent，Lead Agent 需要手工编排以下 4 个阶段：

```
Phase 1: literature-analyzer  → 文献收集（可并行）
Phase 2: data-extractor       → 数据提取（可并行）
Phase 3: report-writing       → 幻灯片内容撰写
Phase 4: ppt-generation skill → 图像生成 + PPTX 组装（必须串行）
```

**影响**

复杂的编排逻辑由 Lead Agent 的 prompt 推理负责，没有结构化的约束。Lead Agent 可能打乱阶段顺序（如先组装 PPTX 再写内容），导致流程失败。

**改进建议**

考虑添加专用 `sci-ppt-generation` subagent，其 `system_prompt` 内嵌完整的四阶段工作流说明，并通过 `task()` 调用各阶段 agent（需允许 task 工具，与其他 subagent 的"禁止 task"设计相反，需特殊处理）。

**回归测试**：`TestSciPptPipelineAgents::test_ppt1_no_dedicated_sci_ppt_subagent_exists`

---

### PPT-2 & PPT-3：幻灯片图像生成须串行，基础设施层无法强制执行

**场景**：sci-ppt-generation

**现象**

`ppt-generation` SKILL.md 明确要求：

> "Generate slides **strictly one by one, in order**. Do NOT parallelize or batch image generation."

但此约束仅存在于自然语言指令中，基础设施层（`SubagentLimitMiddleware`）无法强制执行：

- `SubagentLimitMiddleware` 只限制 `task()` 工具调用的并发数
- 图像生成通过 `bash python generate.py` 完成，不经过 `task()` 工具
- 因此，Agent 在一次响应中生成 5 个 bash 命令（5 张幻灯片并行）不会被任何机制阻止

**影响**

若 Agent 并行生成幻灯片图像，后续幻灯片无法以前一张作为参考图，导致视觉风格不一致，幻灯片看起来像不同演示文稿的拼接。

**改进建议**

在 `ppt-generation` SKILL.md 中用更强烈的语言强调串行约束（已部分改进）。长期方案：在 `generate.py` 脚本层面检测是否有前置幻灯片参考图，如无参考图则拒绝执行（除第 1 张外）。

**回归测试**：`TestSciPptSequentialConstraint::test_ppt2_subagent_limit_middleware_cannot_enforce_sequential_slides`、`test_ppt3_image_generation_bash_commands_not_governed_by_subagent_limit`

---

## 三、测试覆盖摘要

| 测试文件 | 测试类数 | 测试用例数 | 覆盖的 Issue |
|----------|---------|-----------|------------|
| `test_lead_subagent_collaboration.py` | 12 | ~75 | Issue-1,2,3,4 + 基础注册/配置/执行 |
| `test_collaboration_scenarios.py` | 14 | ~65 | CM-1,2 / BS-1,2 / RA-1,2 / OQ-1,2 / PPT-1,2,3 |
| **合计** | **26** | **~140** | **11 个 Issue** |

### 测试设计原则

1. **无 LLM 调用**：所有测试为纯静态/单元测试，不依赖真实 API。
2. **回归锚点**：Issue 测试断言的是**当前行为**（即使是错误行为），修复后需要更新断言。
3. **范围边界验证**：每个 agent 的 `description` 中的 "Do NOT use for" 约束均有对应测试。
4. **并发截断验证**：`SubagentLimitMiddleware` 在所有五个场景下的截断行为均有对应测试。

---

## 四、优先级建议

| 优先级 | Issue | 原因 |
|--------|-------|------|
| P0 | Issue-4 | 现有 `test_subagent_limit_middleware.py` 无法运行（ImportError） |
| P1 | Issue-3 | 静默截断影响所有 4+ agent 并行场景的可靠性 |
| P1 | CM-2 | GBA/LRRK2 分析路由错误会产生不完整的遗传学结论 |
| P2 | Issue-1 | 代码可读性问题，无功能影响 |
| P2 | Issue-2 | model 继承失效，仅影响使用 `inherit` 的通用 agent |
| P2 | RA-2 | 获益-风险路由错误影响关键监管决策 |
| P2 | OQ-2 | PV 质量边界模糊影响合规审核准确性 |
| P3 | BS-1 | 并行写入冲突概率低（实际文件名通常不同），但存在风险 |
| P3 | OQ-1 | Haiku 质量问题在实际使用中才能验证 |
| P3 | PPT-1 | 功能可用，只是编排复杂度偏高 |
| P3 | PPT-2/3 | 串行约束靠指令保证，有改进空间但非阻断性 |
| P3 | RA-1 | 超时问题需真实运行验证 |
| P3 | BS-2 | 流程优化，不影响基础功能 |
