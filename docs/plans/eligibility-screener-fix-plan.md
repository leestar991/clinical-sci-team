# Eligibility-Screener 修复计划

> 基于 2026-07-09 会话 `aca54c56` 的卡死事故复盘，整合图片处理、覆盖率校验、判定容错、**上下文清理**、前端与状态同步五大任务组。
>
> 关联文档：[context-optimization-plan.md](./context-optimization-plan.md)（上下文清理专项的前身，本计划第 4 章将其纳入并扩展）。

---

## 1. 背景与目标

### 1.1 事故概述

eligibility-screener agent 在处理「试验方案 + 筛选期病历 + 筛选期检查」三份 PDF 的入排筛选任务时，最终**卡死未交付报告**：

| 指标 | 值 |
|------|-----|
| run_id | `61d8bda3`（lead_agent）|
| 结局 | 卡死（running 但 13+ 分钟无 checkpoint 推进）|
| 卡死点 | Phase 5 报告生成阶段，ECOG 证据 grep 3 次空结果后无 ai 回复 |
| token 消耗 | 2,710,630（目标 < 35K，超标约 77 倍）|
| 已产出 | criteria_parsed.json / criteria_qc.json / patient_index.json / extraction.json |
| 缺失产出 | judgments_*.json / screening_report.html / criteria_report.html |

### 1.2 修复目标

1. **根治图片格式不一致导致的 OCR 覆盖率缺陷**
2. **消除判定阶段的决策循环卡死**
3. **将主代理上下文 token 控制回设计目标（< 35K 量级）**
4. **恢复前端实时进度可见性**
5. 所有改动遵循 TDD，配套测试，不引入回归

---

## 2. 事故复盘：问题清单与根因链

### 2.1 问题清单（按严重度）

| ID | 严重度 | 问题 | 定位 |
|----|--------|------|------|
| Q1 | P0 | `pdf_to_image.py` 输出混合 .png/.jpg 格式 | `skills/custom/pdf-image-extractor/scripts/pdf_to_image.py` `_save_within_limit` |
| Q2 | P0 | 统计命令 `glob('*.png')` 漏 .jpg，total_pages 错误 | SOUL.md 原则5 脚本 + 覆盖率检查复用 |
| Q3 | P0 | 11 张 .jpg 页 OCR 永久缺失，覆盖率误判 100% | i=48 覆盖率检查（同源错误分母）|
| Q4 | P0 | 任务卡死（ECOG grep 空结果决策循环）| SOUL.md 原则7 未覆盖判定阶段 |
| Q5 | P1 | 上下文暴涨：token 271 万 vs 目标 35K | summarization/tool_output/token_budget 配置 + 阶段间压缩缺失 |
| Q6 | P1 | 前端 SSE 断连，全程不显示实时进度 | 前端 streaming 数据流层 |
| Q7 | P2 | todos 状态滞后于实际执行（P3 实际已结束仍标 in_progress）| SOUL.md todos 状态管理规则执行 |

### 2.2 根因链（Q1→Q4 因果传导）

```
Q1: pdf_to_image.py 的 _save_within_limit 为控制单图大小（max_size_kb=1024），
    在 PNG 超 1MB 时自动降级为 JPEG → 输出混合格式（病历 6png+7jpg；检查 22png+4jpg）
        │
        ▼
Q2: SOUL.md 原则5 的统计脚本用 glob('*.png')，漏掉 .jpg
    → total_pages 记录错误（病历 6/实13；检查 22/实26）
        │
        ▼  （i=19 统计、i=48 覆盖率检查复用同一错误逻辑）
Q3: 覆盖率检查 ocr_md_count(6/22) == total_pages(6/22) → 误判 covered=true
    → 11 张 .jpg 页从未分配 OCR 子任务，且缺陷固化进 phase2_summary.json、patient_index.json
        │
        ▼
Q4: Phase 5 生成报告时，ECOG 评分证据在缺失的 .jpg 病历页中
    → agent 在不完整 OCR 文本找不到 → grep uploads 原始文件（scan 型 .md 为空）→ 3 次空
    → 证据缺失无法判定 + 违反原则7「禁止无效探索」→ 决策循环卡死 13 分钟
```

### 2.3 上下文暴涨根因（Q5）

| 机制 | 现状 | 缺口 |
|------|------|------|
| ViewImageMiddleware base64 清理 | **已实施**（`wrap_model_call` 剥离历史轮 base64）| 已生效，无需改 |
| SummarizationMiddleware | trigger = 120K tokens | context-optimization-plan 建议降至 80K，**未落地** |
| ToolOutputBudgetMiddleware | externalize_min_chars = 12000 | 建议降至 8000，**未落地** |
| TokenBudgetMiddleware | `enabled: false` | 硬停机制**完全未启用**，故 271 万仍无拦截 |
| SOUL.md 阶段间上下文压缩 | 仅 Phase 2 末有 `phase2_summary.json` | P3/P4/P5 缺失阶段间压缩，主代理把 OCR 文本、grep 结果、extraction、报告模板全量读入上下文 |

---

## 3. 修复任务总览

| 任务组 | 目标 | 任务项 | 优先级 |
|--------|------|--------|--------|
| **A. 图片格式与统计** | 根治 Q1/Q2 | A1 manifest 输出 / A2 统计匹配全格式 / A3 SOUL.md 更新 | P0 |
| **B. 覆盖率交叉核验** | 根治 Q3 | B1 实际文件数为分母 / B2 缺失页强制补漏 | P0 |
| **C. 防卡死与判定容错** | 根治 Q4 | C1 判定禁 grep uploads / C2 run 无活动超时 | P0 |
| **D. 上下文清理** | 根治 Q5 | D1-D6（见第 4 章）| P0-P1 |
| **E. 前端与状态同步** | 治理 Q6/Q7 | E1 SSE 重连 / E2 todos 同步 | P1-P2 |

---

## 4. 上下文清理专项（任务组 D）

> 本章是本计划的核心新增项，整合并扩展 [context-optimization-plan.md](./context-optimization-plan.md)。

### D1. 启用并配置 TokenBudget 硬停（P0，最高优先）

**现状**：`config.yaml` 中 `token_budget.enabled: false`，导致本次 271 万 token 无任何拦截。

**方案**：启用 token_budget，配置合理的硬停阈值，防止无限增长卡死。

```yaml
# config.yaml
token_budget:
  enabled: true
  max_tokens: 600000          # 单 run 总 token 上限（含子代理）
  max_input_tokens: 400000    # 输入 token 单独限制（防上下文堆积）
  max_output_tokens: null
  warn_threshold: 0.8         # 80% 软警告，提示 agent 收尾
  hard_stop_threshold: 0.95   # 95% 硬停，剥离 tool_calls 强制产出最终答案
```

**效果**：本次卡死场景下，硬停会在约 57 万 token 时触发，agent 被强制产出当前已有结果的最终答案，而非无限 grep 循环。

**验证**：单测覆盖 `token_budget_middleware.py` 的 hard_stop 剥离 tool_calls 逻辑。

### D2. 降低 Summarization 触发阈值（P0）

**现状**：trigger = 120K tokens，单轮可能塞入大量 OCR 文本后才触发压缩。

**方案**：

```yaml
# config.yaml
summarization:
  enabled: true
  model_name: deepseek-v4-flash
  trigger:
  - type: tokens
    value: 80000            # 120000 → 80000，更早清理含图片/大文本的消息
  keep:
    type: messages
    value: 20
```

**效果**：含 base64 图片的 HumanMessage 更早被 summarization 清除（配合 ViewImageMiddleware 的 in-flight 清理，双保险）。

### D3. 降低 ToolOutput 外部化阈值（P1）

**现状**：`externalize_min_chars = 12000`，grep/read 大输出在累积到 12K 字符后才外部化。

**方案**：

```yaml
# config.yaml
tool_output:
  enabled: true
  externalize_min_chars: 8000    # 12000 → 8000，更积极外部化大输出
  preview_head_chars: 2000
  preview_tail_chars: 1000
  fallback_max_chars: 30000
  fallback_head_chars: 8000
  fallback_tail_chars: 3000
  storage_subdir: .tool-results
```

### D4. 扩展 SOUL.md 阶段间上下文压缩（P0）

**现状**：SOUL.md 仅在 Phase 2 末设计 `phase2_summary.json`，P3/P4/P5 主代理全量读入 OCR 文本、extraction、报告模板，导致 token 暴涨。

**方案**：在每个 Phase 结束时写 summary 文件，后续 Phase 从文件读取关键路径，不依赖前序执行上下文。

| Phase | 新增 summary 文件 | 内容 | 后续读取方式 |
|-------|-------------------|------|-------------|
| P2 | `phase2_summary.json`（已有）| 标准解析路径 + 计数 + OCR 路径 | P2.5/P3 read_file |
| P3 | `phase3_summary.json`（新增）| 患者列表 + extraction/judgments_draft 路径 + 判定统计 | P4 read_file |
| P4 | `phase4_summary.json`（新增）| judgments 合并路径 + QC 结论 | P5 read_file |

**SOUL.md 改动示例（Phase 3 末）**：

```markdown
**全部返回后主代理检查 + 上下文压缩**：
- 确认每位患者的 extraction.json + judgments_draft.json 就绪
- write_file workspace/phase3_summary.json，只保留关键结果：
  ```json
  {
    "patients": [
      {"id": "S042002", "extraction": "workspace/patients/S042002/extraction.json",
       "judgments_draft": "workspace/patients/S042002/judgments_draft.json",
       "judgment_count": {"符合": N, "不符合": N, "存疑": N, "无法判断": N}}
    ]
  }
  ```
- Phase 4+ 从文件读取数据，不依赖 Phase 3 执行上下文
```

### D5. 判定阶段证据来源约束（P0，与 C1 联动）

**现状**：Phase 3-5 主代理为补证据 grep uploads、全量 read_file OCR 汇总，是 token 暴涨和卡死的直接行为来源。

**方案**：SOUL.md 补充判定阶段证据约束规则（见 C1）。

### D6. ViewImageMiddleware 清理验证（P1，验证项）

**现状**：`view_image_middleware.py` 的 `wrap_model_call` / `_strip_historical_image_base64` 已实施（剥离历史轮 base64，保留当前轮）。

**方案**：无需改动，但需补单测验证生效（context-optimization-plan 第 6 章的测试用例尚未落地）。

**测试文件**：`backend/tests/test_view_image_middleware_context_cleanup.py`

```python
def test_wrap_model_call_strips_historical_base64():
    """历史轮次的图片 base64 应被替换为路径引用"""

def test_wrap_model_call_preserves_current_turn_images():
    """当前轮次的图片 base64 应保留完整"""

def test_wrap_model_call_no_change_without_images():
    """没有图片消息时不修改 request"""
```

---

## 5. 各任务组详细方案

### 任务组 A：图片格式与统计（P0）

#### A1. pdf_to_image.py 输出 manifest 清单

**根因**：`_save_within_limit`（第 125-201 行）为控制单图大小，在 PNG 超 `max_size_kb`(默认 1024) 时降级为 JPEG。这是有意的大小控制策略，副作用是混合格式。**不应强行禁用降级**（会导致超大图片），而应让下游不依赖固定扩展名。

**方案**：`convert_pdf` 返回时额外写一份 manifest，记录每页实际输出的文件名与格式，供下游读取。

```python
# pdf_to_image.py - convert_pdf 末尾新增
manifest = {
    "source": pdf_path.name,
    "stem": stem,
    "total_pages": total_pages,
    "pages": [
        {"page": i + 1, "filename": p.name, "format": p.suffix.lstrip(".")}
        for i, p in enumerate(outputs)
    ],
}
manifest_path = output_dir / f"{stem}_manifest.json"
manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
```

#### A2. SOUL.md 统计脚本匹配全格式

**方案**：原则5 的 PDF 类型判定脚本 + 第三轮统计，改为匹配所有图片格式或读 manifest。

```python
# 原：files = [p.name for p in base.glob('*.png')]
# 改：优先读 manifest，回退到全格式 glob
import json
manifest = base / f'{source}_manifest.json'
if manifest.exists():
    files = [p['filename'] for p in json.loads(manifest.read_text())['pages']]
else:
    files = sorted([p.name for p in base.iterdir()
                    if p.suffix.lower() in ('.png', '.jpg', '.jpeg')])
```

#### A3. SOUL.md 文档更新

原则5 表格补注：「图片提取可能输出混合 .png/.jpg（受单图大小控制策略影响），统计与覆盖率校验必须匹配全格式或读 `_manifest.json`，禁止仅 glob `*.png`。」

### 任务组 B：覆盖率交叉核验（P0）

#### B1. 覆盖率以实际文件数为分母

**方案**：Phase 2 收尾覆盖率检查改用图片目录实际文件数（ground truth），不以 total_pages 为准。

```python
img_count = len([p for p in img_dir.iterdir() if p.suffix.lower() in ('.png','.jpg','.jpeg')])
ocr_count = len([p for p in ocr_dir.glob('*.md') if p.name != 'ocr_records.md'])
covered = (ocr_count == img_count)
```

#### B2. 缺失页强制补漏

```python
if ocr_count != img_count:
    missing = set(img_stems) - set(ocr_stems)
    # 发起补漏 OCR 子任务，不标记 covered，不进入下一 Phase
```

SOUL.md 补规则：「覆盖率以图片目录实际文件数为准，不以 total_pages 为准。缺失页必须补漏后方可越过 Phase 2 屏障。」

### 任务组 C：防卡死与判定容错（P0）

#### C1. 判定阶段禁 grep uploads，证据缺失直接判「无法判断」

**方案**：SOUL.md 在原则7 后补充判定阶段约束。

```markdown
### 7. 禁止无效探索（补充判定阶段约束）
- Phase 1->2 转换时禁止 glob/find/ls 搜索历史文件
- **Phase 3-5 判定时，证据仅来自 OCR 文本与 extraction.json；
  禁止 grep uploads/原始 PDF 补救。证据缺失直接判「无法判断」
  并在判定理由中记录「缺失页：xxx」，不阻塞流程、不重复搜索。**
- 每次新会话从零构建
```

**效果**：本次 ECOG 找不到 → 直接判「无法判断（缺失页含证据）」→ 继续生成报告，不卡死。

#### C2. run 级无活动超时（backend harness）

**方案**：在 run 生命周期管理中增加无活动超时检测。

- 位置：`backend/packages/harness/deerflow` 的 run 执行层（需定位具体 run loop）
- 逻辑：run 状态 running 但 checkpoint 超过 N 分钟（建议 5 分钟）无 updated_at 推进，自动标记 `timeout`/`failed`，释放 thread
- 避免「僵尸 run」长期占用

### 任务组 E：前端与状态同步（P1-P2）

#### E1. 前端 SSE 重连

**方案**：前端在 run 状态为 running 但 SSE 断连时，自动重连或显示「运行中（连接中断，重连中）」，不静默呈现 idle。

- 位置：`frontend/` 的 thread/streaming 数据流层（参考 `frontend/AGENTS.md`）

#### E2. todos 状态同步

**方案**：SOUL.md todos 规则明确「Phase 完成判定（非进入）时立即标记 completed」。当前 P3 实际已结束但未标记，应确保每个 Phase 产出物就绪后立即更新对应 todo 为 `[✓]`。

---

## 6. 实施优先级与排期

| 优先级 | 任务 | 预计耗时 | 依赖 |
|--------|------|----------|------|
| **P0 - 立即** | D1 启用 token_budget 硬停 | 10 min | 无 |
| **P0 - 立即** | C1 判定阶段禁 grep uploads | 15 min（SOUL.md）| 无 |
| **P0 - 立即** | A1+A2+A3 图片 manifest + 统计全格式 | 1 h | 无 |
| **P0 - 立即** | B1+B2 覆盖率交叉核验 | 1 h | A1 |
| **P0 - 立即** | D2 summarization 80K | 5 min（config）| 无 |
| **P0** | D4 阶段间上下文压缩（P3/P4 summary）| 1 h（SOUL.md）| 无 |
| **P1** | D3 tool_output 8000 | 5 min（config）| 无 |
| **P1** | D6 ViewImageMiddleware 单测 | 30 min | 无 |
| **P1** | C2 run 无活动超时 | 2 h（backend）| 需定位 run loop |
| **P2** | E1 前端 SSE 重连 | 3 h（frontend）| 无 |
| **P2** | E2 todos 同步 | 15 min（SOUL.md）| 无 |

**建议实施顺序**：D1 → C1 → A1+A2+A3 → B1+B2 → D2 → D4 → D3 → D6 → C2 → E1 → E2

前 6 项（D1/C1/A/B/D2/D4）改动小、收益最大，能直接避免本次卡死再次发生，建议优先合入。

---

## 7. 验证计划

### 7.1 单元测试

| 测试文件 | 覆盖任务 |
|----------|----------|
| `backend/tests/test_view_image_middleware_context_cleanup.py` | D6 |
| `backend/tests/test_pdf_to_image_manifest.py`（新增）| A1 |
| `backend/tests/test_token_budget_hard_stop.py`（新增/扩展）| D1 |

运行：`cd backend && make test`

### 7.2 集成验证

1. 启动新 eligibility-screener 会话，上传同样 3 份 PDF
2. **观察不再卡死**：报告正常产出 `screening_report.html` + `criteria_report.html`
3. **观察覆盖率正确**：日志显示 img_count=13/26，ocr_count=13/26，covered=true（真实）
4. **观察 token 达标**：run 总 token 在 token_budget 硬停阈值内（< 60 万），不触发 hard_stop
5. **观察无 .jpg 遗漏**：所有 39 张图片均有对应 OCR .md
6. 前端实时进度可见（SSE 不断连）

### 7.3 回归测试

```bash
cd backend && make test      # 后端全量
cd frontend && pnpm check    # 前端 lint + type
```

---

## 8. 风险评估

| 风险 | 影响 | 缓解 |
|------|------|------|
| token_budget 硬停阈值过低，误杀正常长任务 | agent 被迫提前收尾 | max_tokens 设 60 万（本次 271 万的约 1/4 余量），观察后微调 |
| manifest 新增字段，旧会话无 manifest | A2 统计回退到全格式 glob | A2 已设计 manifest 缺失回退逻辑 |
| summarization 80K 触发过频，压缩损失上下文 | 判定遗漏历史信息 | 配合 DurableContextMiddleware 保持关键信息；keep=20 messages |
| C2 run 超时误判慢 LLM 响应 | 正常运行被中断 | 阈值取 5 分钟（LLM 单次响应通常 < 1 分钟），且仅在 checkpoint 无推进时触发 |
| 判定禁 grep uploads 导致本可找到的证据被判「无法判断」| 报告「无法判断」偏多 | 优先靠 B1/B2 保证 OCR 全覆盖；缺失页补漏后证据齐全，grep 需求消失 |

---

## 9. 附录：涉及文件清单

### 9.1 代码改动

| 文件 | 任务 | 改动类型 |
|------|------|----------|
| `skills/custom/pdf-image-extractor/scripts/pdf_to_image.py` | A1 | 新增 manifest 输出 |
| `backend/.deer-flow/agents/eligibility-screener/SOUL.md` | A3/B2/C1/D4/D5/E2 | 文档更新 |
| `config.yaml` | D1/D2/D3 | 配置调整 |
| `backend/packages/harness/deerflow/` run 执行层 | C2 | 新增无活动超时（需定位）|
| `frontend/` streaming 层 | E1 | SSE 重连（需定位）|

### 9.2 测试新增

| 文件 | 任务 |
|------|------|
| `backend/tests/test_view_image_middleware_context_cleanup.py` | D6 |
| `backend/tests/test_pdf_to_image_manifest.py` | A1 |
| `backend/tests/test_token_budget_hard_stop.py` | D1 |

### 9.3 关联文档

- [context-optimization-plan.md](./context-optimization-plan.md) - 上下文清理前身方案（ViewImageMiddleware base64 清理，已实施）
- [backend/AGENTS.md](../../backend/AGENTS.md) - middleware 链、config 系统深度
