# Eligibility-Screener 监控发现的问题清单

> 来源：会话 `b729d95e-8628-4dcb-9dcc-b68fdfa96828` 实时监控分析 (2026-07-13)
> 状态：待优化

---

## P0 — 已修复

### 1. Summarize 触发失败（跨 provider 场景）

- **根因**: 对话模型 (openai/gpt-5-4) 与 summarization 模型 (deepseek-v4-flash) provider 不一致，导致 `_should_summarize_based_on_reported_tokens()` 中的 provider 校验永远返回 False；同时 `count_tokens_approximately()` 的 1.25x scaling cap 无法弥补实际 2.29x 的差距
- **修复**: Override `_should_summarize_based_on_reported_tokens()`，去掉 provider 校验，改用 `input_tokens` 判断
- **文件**: `backend/packages/harness/deerflow/agents/middlewares/summarization_middleware.py`
- **验证**: 已通过测试，线上会话已正常触发 summarize

---

## P1 — 高优先级

### 2. Summarize 震荡：高频低效压缩循环

- **现象**: 当保留窗口（keep=30 messages）包含大文件 read_file 结果时，保留内容本身就接近 50k 阈值，每次 model call 都触发 summarize，但每次仅压缩 3-4 条消息
- **影响**: 浪费 summarization 模型调用（观测到连续 5 次每 3 steps 触发一次）
- **数据**: 震荡期 token 在 49k-57k 之间反复
- **建议方案**:
  - A) 降低 `keep.value` 到 20（减少保留窗口大小）
  - B) 改 keep 为 token-based（`type: tokens, value: 30000`），更精确控制保留量
  - C) 提高 `trigger.value` 到 70k（给更多缓冲，但会增加模型输入成本）
  - D) 添加 cooldown 机制：summarize 后 N 步内不再触发

### 3. 第一次 Summarize 生成空摘要

- **现象**: 首次触发时 summary 内容为 `"## SESSION INTENT\nNone\n\n## SUMMARY\nNone\n\n## ARTIFACTS\nNone\n\n## NEXT STEPS\nNone"`（78 chars，全是 None）
- **原因**: 早期消息主要是 task 子代理调度（结构化 JSON 状态），deepseek-v4-flash 无法从中提取有意义的摘要
- **影响**: 第一次 summarize 相当于丢失了早期对话上下文
- **建议方案**:
  - A) 添加 summary 质量检查：如果生成内容过短或全为 None，跳过压缩（保留原始消息）
  - B) 在 summary prompt 中增加对 tool-call/task 类消息的处理指导
  - C) 考虑为首次 summarize 使用更强的模型

---

## P2 — 中优先级

### 4. `present_files` 使用问题：重复 present + 子代理产出未 present

- **现象 A — 重复 present 同一文件**:
  - `criteria_parsed.json` 被 present 了 3 次（msg[7]、msg[11]、msg[29]）
  - 前两次是独立的 present 调用，第三次是与其他文件一起批量 present
  - 每次重复调用虽然被 `merge_artifacts` reducer 去重，但仍浪费一次工具调用和 token

- **现象 B — 子代理产出的中间文件未 present 给用户**:
  - 子代理执行 task 后产出了以下文件，均未被 present：
    ```
    workspace/patients/S042002/extraction.json    (27,552 bytes) — 证据提取结果
    workspace/patients/S042002/judgments_draft.json (65,424 bytes) — 判定草稿
    workspace/patients/S042002/reasons.json        (14,307 bytes) — 推断理由
    workspace/patients/S042002/qc_report.json      (11,403 bytes) — QC 核查报告
    ```
  - 主 agent 仅将最终合并后的 `judgments_S042002.json` 和 HTML 报告 present 到了 `/mnt/user-data/outputs/`
  - 但 QC 报告和推断理由等中间过程文件对用户也有参考价值（尤其 `qc_report.json` 包含 12 项质控问题明细）

  **文件交付对照表**：

  | 文件 | 大小 | 用途 | 是否 present |
  |------|------|------|:------------:|
  | `extraction.json` | 27KB | 证据提取结果 | ❌ |
  | `judgments_draft.json` | 65KB | 判定草稿 | ❌ |
  | `reasons.json` | 14KB | 推断理由 | ❌ |
  | `qc_report.json` | 11KB | QC 核查报告（含12项问题明细） | ❌ |
  | `judgments_S042002.json` | 74KB | 最终合并判定 | ✅ |
  | `screening_report.html` | 33KB | 筛选报告 | ✅ |
  | `criteria_report.html` | 22KB | 标准解析报告 | ✅ |
  | `criteria_parsed.json` | 61KB | 结构化标准 | ✅ (×3 次) |

- **影响**:
  - 重复 present 浪费 token（轻微）
  - 子代理产出的过程文件不可见，用户无法查看判定逻辑的中间推导过程

- **建议方案**:
  - A) 在 skill prompt 中明确文件交付规范：
    ```
    交付文件清单（所有以下文件完成后必须 present_files 给用户）：
    1. /mnt/user-data/outputs/criteria_parsed.json — 入排标准解析
    2. /mnt/user-data/outputs/judgments_{patient_id}.json — 最终判定
    3. /mnt/user-data/outputs/screening_report.html — 筛选报告
    4. /mnt/user-data/outputs/criteria_report.html — 标准解析报告
    5. /mnt/user-data/outputs/qc_report_{patient_id}.json — QC 报告（过程文件）
    6. /mnt/user-data/outputs/reasons_{patient_id}.json — 推断理由（过程文件）
    ```
  - B) 要求子代理完成后将产出文件移动到 `/mnt/user-data/outputs/` 并在 task result 中声明文件路径
  - C) 主 agent 在收到子代理 task result 后，检查是否有新产出文件需要 present（可在 skill prompt 中添加 checklist）
  - D) 对已 present 过的文件，仅在内容更新后再次 present（避免重复）

### 5. 子代理 Recursion Limit 耗尽

- **现象**: 一个 `task` 子代理执行了 100 步仍未完成，触发 `GraphRecursionError`
- **影响**: 浪费时间（该子代理执行了数分钟但无产出）和 token
- **建议方案**:
  - A) 为 eligibility-screener 的子代理配置更合适的 `max_turns`（当前默认 150，但 recursion_limit 仍为 100）
  - B) 在子代理 prompt 中强调分步执行、避免深层嵌套
  - C) 添加子代理超时后的任务降级/简化重试策略

### 6. QC 质控任务不应使用 bash 编写脚本执行

- **现象**:
  - 主 agent 在 P2/P2.5 阶段通过 `bash` 编写了多个 Python 脚本来执行 QC 校验：
    - `run_qc.py` (16KB) — 对 `criteria_parsed.json` 做结构/语义校验
    - `rebuild_qc.py` (12KB) — 硬编码 QC 结果重建
    - `build_criteria_json.py` (23KB) — 解析原始标准生成 JSON
    - `make_judgments_s042002.py` (14KB) — 生成患者判定
    - `build_s042002_outputs.py` (10KB) — 构建最终输出
  - 这些脚本通过 `bash: python3 /mnt/user-data/workspace/xxx.py` 执行
  - 其中 QC 校验脚本 (`run_qc.py`) 做的是规则型检查（JSON 结构、字段完整性、转化条件存在性），本质上是 agent 用代码"代替"了应由 LLM 推理完成的质控工作

- **问题**:
  - **QC 的核心价值是医学语义层面的校验**（如：判定逻辑是否正确、证据是否充分、数值比较是否合理），而非结构性 JSON 校验
  - 脚本只能做表面校验（字段是否存在、JSON 是否合法），无法做深层语义 QC（如：排除标准 EX-5-2 的豁免条件拆分是否导致语义偏差）
  - Agent 编写脚本执行 QC 绕过了 LLM 推理能力，退化为了程序员模式
  - 脚本中硬编码了宿主机绝对路径（如 `/Users/louli/Documents/...`），暴露了安全风险
  - 脚本产出的 `criteria_qc.json` 仅检测到 `missing_transform` 类结构性问题，而 task(quality-control) 子代理产出的 `qc_report.json` 发现了 12 项语义级问题（高 5/中 5/低 2），质量差距明显

- **影响**:
  - QC 质量低：脚本式 QC 遗漏了语义级错误
  - Token 浪费：编写 16KB+ 的脚本消耗大量输出 token
  - 上下文膨胀：脚本输出（stdout）进入消息流，加速 summarize 触发
  - 安全风险：脚本硬编码宿主路径

- **建议方案**:
  - A) 在 skill prompt 中明确禁止：**QC 任务禁止使用 bash 编写脚本执行，必须委派给 `task(quality-control)` 子代理以 LLM 推理方式完成**
  - B) 在 eligibility-screener 的 agent config 中限制 QC 阶段可用的 tool_groups（移除 bash），或通过 guardrail 拦截 QC 阶段的 bash 调用
  - C) 在 quality-control 子代理的 prompt 中明确 QC 范围：
    ```
    QC 校验范围（必须用 LLM 推理完成，禁止编写脚本）：
    1. 判定结论正确性 — 数值比较、逻辑关系是否成立
    2. 证据充分性 — 是否有遗漏证据或过度推断
    3. 跨文档一致性 — 同一信息在不同文档中的描述是否矛盾
    4. 时间窗口正确性 — 日期计算、参考日期选择是否合理
    5. 条件拆分合理性 — AND/OR/除外拆分是否改变语义
    ```
  - D) 保留结构性校验（JSON 合法性、字段存在性）作为前置自动化步骤，但不计入 QC 质控流程

### 7. `.tool-results` 文件对 Agent 可见性问题

- **现象**: `ToolOutputBudgetMiddleware` 将大于 12k chars 的工具输出外部化到 `.tool-results/` 目录。该会话生成了 28 个文件（~700KB），都在 `/mnt/user-data/outputs/.tool-results/` 下
- **问题**:
  - Agent 执行 `ls /mnt/user-data/outputs/` 时会看到 `.tool-results/` 目录
  - Summarize 后原始预览消息被压缩，agent 丢失文件引用但文件仍存在
  - Summary text 中保留了对这些路径的引用（如 `read_file-3c21d2a2fe2b.txt`），可能误导 agent 将中间产物当作工作成果
- **建议方案**:
  - A) 在 `ls` 工具中默认隐藏 `.tool-results/` 目录（类似 dotfile 约定）
  - B) 在 summarize 时将活跃的 externalized 文件引用保留为 durable context
  - C) 定期清理已被 summarize 的旧 tool-results 文件

### 8. 入排匹配阶段缺乏明确的输入边界定义

- **现象**: workspace 中混杂了输入文件、中间脚本、中间产物、QC 结果等，agent 在 glob/ls 时需要从中辨别哪些是"输入资料"
- **影响**: 可能导致 agent 将中间产物误作为匹配依据，或在搜索时被不相关文件分散注意力
- **建议方案**:
  - A) 在 skill prompt（eligibility-judgment）中明确声明输入文件清单：
    ```
    输入资料（仅以下文件作为判定依据）：
    - /mnt/user-data/uploads/试验方案.md — 入排标准来源
    - /mnt/user-data/uploads/筛选期病历.md — 患者病历
    - /mnt/user-data/uploads/筛选期检查.md — 患者检查报告
    - /mnt/user-data/workspace/criteria_parsed.json — 结构化入排标准
    ```
  - B) 工作区采用分层目录结构：`workspace/inputs/`、`workspace/scripts/`、`workspace/intermediate/`
  - C) 在 task 子代理 prompt 中传入明确的输入文件路径，而非让子代理自行搜索

---

## P3 — 低优先级

### 9. 路径使用错误（轻微）

- **现象**:
  - `bash` 工具使用了不安全绝对路径 `/patient_id`（应为 `/mnt/user-data/workspace/patients/{id}`）
  - `grep` 工具对文件而非目录调用（`grep` 的 path 参数应为目录）
- **影响**: 极低，agent 自行从错误中恢复
- **建议方案**: 在 system prompt 中强化路径使用规范，或在工具 description 中补充示例

### 10. `write_todos` 重复写入相同内容

- **现象**: 相同 todo 列表内容被写入两次
- **影响**: 极低，仅浪费少量 token
- **建议方案**: 可在 `TodoMiddleware` 中添加内容去重（如果新内容与当前完全一致则跳过）

---

## 监控统计摘要

| 指标 | 值 |
|------|-----|
| 总执行步数 | 341 steps (72→413) |
| 监控时间 | ~38 分钟 |
| 工具调用总数 | ~80+ |
| 工具错误数 | 5 (错误率 ~6%) |
| Summarize 触发次数 | 7 次 |
| 其中有效压缩 | 3 次（第 2、3 次为主要压缩） |
| 其中震荡压缩 | 4 次（每次仅 3-4 条，低效） |
| 最终 Summary 质量 | 良好 (3,092 chars，覆盖 intent/summary/artifacts/next-steps) |
