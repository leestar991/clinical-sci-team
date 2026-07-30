# eligibility-screener ECOG 检索失败修复 + SOUL 重构变更汇总

> 实施日期：2026-07-15
>
> 触发问题：入排判定中 `ECOG：1分` 明确存在于 page 5 的 OCR 文本，但被埋在一个
> ~1200 字符、无换行的密集段落中间，判定模型"注意力滑过"未检索到，误判为「无法判断」；
> Phase 4 QC 也未发现（QC 委派模板写着"不需要读文件"，只拿到 judgments，结构上无法反查 OCR）。
>
> 根因定性：**这不是"缺规则"，而是"规则全是依赖模型注意力的软提示"+ 一处结构性死角**。
> 现有 SOUL/skill 里"按概念取证"（原则四）、"禁止伪无法判断"（原则五）、"穷尽取证"（原则七）、
> QC"漏判反查"都已存在却全部被绕过——对注意力失败再加 prose 无效。修复方向：**引入不依赖
> 注意力的确定性机械检索（grep 兜底闸），并修复 QC 拿不到 OCR 的死角**。
>
> 本次含两批改动：
> - **第一批（O1–O6）**：确定性 grep 兜底闸 + QC 死角修复 + 强制同义词 + OCR 结构化。
> - **第二批（SOUL 重构）**：合并语义重复约束（15→13 条）+ 把 criteria QC 从 P2.5 提前到 P2 与 OCR 并行。

---

## 1. 变更概览

| 层级 | 文件 | 新增测试 | 覆盖项 |
|------|------|----------|--------|
| 技能脚本（新增） | `skills/custom/eligibility-judgment/scripts/uncertain_recheck.py` | — | O1 |
| 技能 SKILL.md | `skills/custom/eligibility-judgment/SKILL.md` | — | O1/O2 |
| 技能 SKILL.md | `skills/custom/criteria-parser/SKILL.md` | — | O3 |
| Agent SOUL.md | `backend/.deer-flow/agents/eligibility-screener/SOUL.md` | — | O1/O2/O4/O5/O6 + 重构 |
| 测试（新增） | `backend/tests/test_eligibility_uncertain_recheck.py` | 12 | O1/O2/O3/O4/O5/O6 |

**统计**：4 个源文件改动，1 个新脚本（223 行），1 个新测试文件（12 用例），全部通过；`ruff format`/`ruff check` 干净。

---

## 2. 第一批：ECOG 检索失败修复（O1–O6）

### O1 — 确定性 grep 兜底闸（核心，机械步骤取代软提示）

**新增** `skills/custom/eligibility-judgment/scripts/uncertain_recheck.py`：

- 对每条判定为「无法判断」且 `可从病例获取=true` 的条件，用
  `转化条件.同义词` + `匹配字段` + **内置量表同义词表**（ECOG/KPS/TNM/CrCl/G-CSF/ANC/MSI/HBsAg…）
  对该患者 OCR 汇总做**大小写不敏感 grep**。命中即产出 `suspected_missed`。
- 命中长/密集段落时截取命中词周边 ±40 字符（阈值 200），避免噪音淹没证据。
- **只查该患者 `ocr_records.md`，绝不查 uploads/原始 PDF**（与 fix-plan C1 的"禁 grep uploads"互补：uploads 禁检索、本患者 OCR 强制检索）。
- 始终 `exit 0`；是否漏判以产物 JSON 的 `suspected_missed` 为准，便于子代理/QC 机械核验。

**`SKILL.md` 新增「原则八」**：判「无法判断」前必须跑该脚本；命中即 `read_file` 命中行 ±5 行上下文，据实改判为 符合/不符合/存疑，把命中原文写入 evidence；仅全空才允许保留「无法判断」。约束清单补 31/32 条。

### O2 — 修复 QC 结构性死角

`eligibility-judgment/SKILL.md` 的 QC 委派模板：

- 从"**数据直接附在下方，不需要读文件**"改为：传入 `judgments_draft.json` + `uncertain_recheck.json` + 该患者各 source `ocr_records.md` 绝对路径。
- 检查项 7「漏判反查」从"靠 LLM 再读一遍 OCR"（不可靠）改为"**先核对 `uncertain_recheck.json.suspected_missed`**（机械命中表）"，脚本未覆盖项再 grep/read OCR 复核。
- 交付清单新增 `outputs/uncertain_recheck_{id}.json`。

### O3 — criteria-parser 强制同义词

`criteria-parser/SKILL.md`：

- `同义词` / `证据位置` 由「可选」升级为对"可从病例获取"条件**强制**（空缺由 QC 标阻断级）。
- 新增**内置高频量表/指标同义词对照表**（ECOG/KPS/TNM/CrCl/G-CSF/ANC/MSI-MMR/HBsAg），与脚本内置表对齐。
- 执行流程步骤 1 + QC 校验步骤 2 同步强调该硬规则。

### O4 / O6 — OCR 阶段结构化（源头降噪）

SOUL.md Phase 2 OCR 子任务模板新增输出格式要求：

1. 首行标注来源；
2. **页首"关键字段速览"块**：`<!-- key-fields: ECOG=1分; 身高=145cm; ... -->`，把离散临床事实抽成一行，供 LLM 与 grep 快速命中；
3. **正文无损重排**：并列临床事实（分号/句号/顿号分隔）逐条断行，**只断行不改写、不概括**，原始长段落保留。

### O5 — QC 漏判反查列为阻断级

SOUL.md 原则（QC 收敛机制）新增：`uncertain_recheck.json.suspected_missed` 非空**一律计阻断级**，必须改判方可通过，**不得**以"带建议放行"绕过；纯主观拆分粒度仍为建议级。

### 效果（对原案）

`ECOG：1分` 无论埋多深都会被 grep 命中 → 禁止判「无法判断」、强制读上下文改判「符合」；即便判定阶段漏改，Phase 4 QC 也会据 `suspected_missed` 阻断级挡下。

---

## 3. 第二批：SOUL.md 重构

### 3.1 合并语义重复约束（15 条 → 13 条，无删减独立规则/检查项）

| 合并前 | 合并后 | 说明 |
|--------|--------|------|
| 原则 7（判定证据）+ 11（判定证据读取）+ 14（输入资料边界） | **原则 11「判定阶段证据边界与取证」** | 判定证据边界原来重复三遍，合一 |
| 原则 8（上下文管理）+ 11（上下文读取纪律） | **原则 8「上下文管理与读取纪律」** | 上下文规则散在两处，合一 |
| 原则 9（文件可见性）+ 15（交付清单与去重） | **原则 12「文件可见性与交付清单」** | 可见性/交付合一 |
| 原则 7（禁止无效探索） | **原则 7「禁止无效探索与 Phase 边界」** | 精简，判定约束指向原则 11 |

**编号重排**：QC 12→**9**、路径 13→**10**、Todolist 10→**13**；**原则 1–6 编号保持不变**（Phase 里对原则 5/6①②④ 的引用不受影响）。所有 `原则N` 交叉引用已同步（Phase 4「遵循原则12」→「原则9」）。

**顺带修正**：原则 14 曾把"试验方案.md"误列为判定证据源，与 `eligibility-judgment` 技能"原文已内嵌 `criteria_parsed.json`、无需再读方案"矛盾——合并时移除该源，保持一致并防上下文膨胀。

### 3.2 criteria QC 从 P2.5 提前到 P2，与 OCR 并行

**动机**：criteria QC 只依赖 `criteria_parsed.json`（P2 首批即返回），无需等 OCR 完成。原 P2.5 串行"等 OCR 全部完成后才做标准 QC"存在不必要等待。

| 阶段 | 变更 |
|------|------|
| **P2** | 入排解析 task 一返回即**立即启动 criteria QC 收敛（原则9，最多2轮），与后续 OCR 分片同批并发**；明确 QC 与 OCR 共享每批 3 并发预算。P2 完成条件 = criteria QC 通过（或带建议放行）**且** OCR 全覆盖。`phase2_summary.json` 增加 `criteria_qc`/`criteria_qc_passed`；criteria QC 相关 `present_files` 与 `cp outputs/` 移到 P2。 |
| **P2.5** | 收窄为「患者拆分 + 按患者聚合 OCR」，不再做标准 QC；新增 `phase2_5_summary.json`（患者列表 + ocr_records 路径）供 P3 读取。 |
| 其他 | 阶段总览表、Todolist 初始化模板、简化流程（仅方案无病历：P1 → P2 含 criteria QC → P5）、目录规范、Phase3 前置、Phase5 present 去重引用均已同步。 |

**收益**：criteria QC 两轮延迟与 OCR 分片延迟重叠，去掉串行等待，缩短端到端时长；判定阶段三处重复约束合一，降低 SOUL 自身 token 占用与维护歧义。

---

## 4. 测试

**新增** `backend/tests/test_eligibility_uncertain_recheck.py`（12 用例，全通过）：

| 用例 | 覆盖 |
|------|------|
| `test_build_keywords_includes_builtin_ecog_synonyms` | O1 内置同义词表 |
| `test_grep_finds_ecog_in_dense_paragraph` | O1 密集段落 grep 命中 + 截断 |
| `test_recheck_flags_buried_ecog_as_suspected_missed` | O1 核心：埋藏 ECOG 被标漏判 |
| `test_recheck_passes_when_evidence_truly_absent` | O1 真缺失时放行 |
| `test_recheck_skips_non_obtainable_conditions` | O1 跳过不可获取条件（如知情同意） |
| `test_recheck_ignores_non_uncertain_conclusions` | O1 仅反查「无法判断」 |
| `test_cli_writes_product_and_reports_missed` | O1 CLI 端到端 |
| `test_criteria_parser_mandates_synonyms` | O3 强制同义词内容断言 |
| `test_judgment_skill_declares_grep_gate` | O1 原则八内容断言 |
| `test_qc_template_receives_ocr_and_recheck` | O2 QC 死角修复断言 |
| `test_soul_phase2_ocr_key_fields_block` | O4/O6 key-fields 块 |
| `test_soul_phase3_runs_gate_and_phase4_blocks_on_missed` | O1/O5 SOUL 兜底闸接线 |

运行：`cd backend && .venv/bin/python -m pytest tests/test_eligibility_uncertain_recheck.py`

---

## 5. 与既有方案的关系

- 本次处理**"页在、证据在、但没检索到"**（注意力/检索失败）；
  [eligibility-screener-fix-plan.md](./plans/eligibility-screener-fix-plan.md) 的 C1 处理**"页真缺失"**（禁 grep uploads 兜底）。
- 二者互补且不冲突：**uploads 原始 PDF 禁检索，本患者已 OCR 的 `ocr_records.md` 强制机械检索**。

## 6. 涉及文件清单

### 代码/内容改动
| 文件 | 变更 |
|------|------|
| `skills/custom/eligibility-judgment/scripts/uncertain_recheck.py` | 新增（O1 兜底闸脚本，223 行） |
| `skills/custom/eligibility-judgment/SKILL.md` | O1 原则八 + 约束31/32；O2 QC 模板；交付清单 |
| `skills/custom/criteria-parser/SKILL.md` | O3 强制同义词 + 内置量表表 + QC 校验 |
| `backend/.deer-flow/agents/eligibility-screener/SOUL.md` | O1/O4/O5/O6 接线 + 原则 15→13 合并重排 + P2/P2.5 重构 |

### 测试新增
| 文件 | 用例数 |
|------|--------|
| `backend/tests/test_eligibility_uncertain_recheck.py` | 12 |

> 注意：`backend/.deer-flow/` 为运行时目录，通常 gitignored；若需随部署分发/纳入版本控制，需确认该 agent 定义的持久化方式。
